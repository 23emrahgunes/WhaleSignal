"""Persistent state and audit trail for DUAL40 maker recovery.

The ladder is persisted separately for PAPER and LIVE so research results can never
change real-money sizing.  LIVE hard-stop state survives process restarts; restarting
the daemon is deliberately not a way to unlock a capped 30-share loss.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from p3_dual40_core import DEFAULT_LADDER
from p3_schema import connect_p3, ensure_p3_schema


DUAL40_ASSETS = ("BTC", "ETH", "SOL", "XRP")

ACTIVE_STATUSES = {
    "PAPER_RESTING",
    "LIVE_SUBMITTING",
    "LIVE_RESTING",
    "CANCELLING",
    "WAIT_RESOLUTION",
    "STARTUP_RECOVERY",
}


DUAL40_DDL = """
CREATE TABLE IF NOT EXISTS p3_dual40_state (
    scope               TEXT NOT NULL CHECK(scope IN ('PAPER','LIVE')),
    asset               TEXT NOT NULL,
    level_index         INTEGER NOT NULL DEFAULT 0,
    loss_pool_usdc      REAL NOT NULL DEFAULT 0,
    hard_stopped        INTEGER NOT NULL DEFAULT 0,
    hard_stop_reason    TEXT,
    updated_at_ms       INTEGER NOT NULL,
    last_cycle_id       INTEGER,
    migration_note      TEXT,
    PRIMARY KEY(scope, asset)
);

CREATE TABLE IF NOT EXISTS p3_dual40_cycles (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    scope                   TEXT NOT NULL CHECK(scope IN ('PAPER','LIVE')),
    asset                   TEXT NOT NULL,
    session_id              TEXT,
    condition_id            TEXT NOT NULL,
    combo_key               TEXT NOT NULL,
    market_end_ts_ms        INTEGER NOT NULL,
    level_index             INTEGER NOT NULL,
    target_shares           REAL NOT NULL CHECK(target_shares > 0),
    maker_price             REAL NOT NULL CHECK(maker_price > 0 AND maker_price < 1),
    status                  TEXT NOT NULL,
    gate_json               TEXT NOT NULL DEFAULT '{}',
    up_token_id             TEXT NOT NULL,
    down_token_id           TEXT NOT NULL,
    up_order_id             TEXT,
    down_order_id           TEXT,
    before_up_shares        REAL NOT NULL DEFAULT 0,
    before_down_shares      REAL NOT NULL DEFAULT 0,
    up_filled_shares        REAL NOT NULL DEFAULT 0,
    down_filled_shares      REAL NOT NULL DEFAULT 0,
    up_fill_price           REAL CHECK(up_fill_price IS NULL OR (up_fill_price > 0 AND up_fill_price < 1)),
    down_fill_price         REAL CHECK(down_fill_price IS NULL OR (down_fill_price > 0 AND down_fill_price < 1)),
    matched_shares          REAL NOT NULL DEFAULT 0,
    residual_side           TEXT,
    residual_shares         REAL NOT NULL DEFAULT 0,
    official_result         TEXT,
    realized_pnl_usdc       REAL,
    loss_pool_before_usdc   REAL NOT NULL DEFAULT 0,
    loss_pool_after_usdc    REAL,
    merge_tx_hash           TEXT,
    heartbeat_id            TEXT,
    last_heartbeat_ms       INTEGER,
    near_touch_up_41        INTEGER NOT NULL DEFAULT 0,
    near_touch_down_41      INTEGER NOT NULL DEFAULT 0,
    error_code              TEXT,
    details_json            TEXT NOT NULL DEFAULT '{}',
    created_at_ms           INTEGER NOT NULL,
    orders_posted_at_ms     INTEGER,
    orders_cancelled_at_ms  INTEGER,
    resolved_at_ms          INTEGER,
    updated_at_ms           INTEGER NOT NULL,
    UNIQUE(scope,condition_id)
);
CREATE INDEX IF NOT EXISTS idx_p3_dual40_cycles_time
ON p3_dual40_cycles(created_at_ms DESC);

CREATE TABLE IF NOT EXISTS p3_dual40_market_decisions (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    scope                   TEXT NOT NULL CHECK(scope IN ('PAPER','LIVE')),
    asset                   TEXT NOT NULL,
    combo_key               TEXT NOT NULL,
    condition_id            TEXT NOT NULL,
    market_start_ts_ms      INTEGER,
    market_end_ts_ms        INTEGER,
    first_seen_ms           INTEGER NOT NULL,
    last_seen_ms            INTEGER NOT NULL,
    decision                TEXT NOT NULL,
    reason                  TEXT,
    score                   REAL,
    eligible                INTEGER NOT NULL DEFAULT 0,
    opened_cycle_id         INTEGER,
    final_gate_json         TEXT NOT NULL DEFAULT '{}',
    created_at_ms           INTEGER NOT NULL,
    updated_at_ms           INTEGER NOT NULL,
    UNIQUE(scope,condition_id)
);
CREATE INDEX IF NOT EXISTS idx_p3_dual40_decisions_recent
ON p3_dual40_market_decisions(scope,updated_at_ms DESC);
"""


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _decode(value: object) -> Any:
    try:
        return json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def normalize_asset(asset: str) -> str:
    value = str(asset or "").strip().upper()
    if value not in DUAL40_ASSETS:
        raise ValueError(f"unsupported DUAL40 asset: {asset!r}")
    return value


def asset_from_combo_key(combo_key: str) -> str:
    raw = str(combo_key or "").strip()
    asset, sep, horizon = raw.partition(":")
    if sep != ":" or horizon.strip().lower() != "5m":
        raise ValueError(f"cannot infer DUAL40 asset from combo_key: {combo_key!r}")
    return normalize_asset(asset)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _write_meta(conn: sqlite3.Connection, key: str, payload: dict[str, Any]) -> None:
    now = int(time.time() * 1000)
    conn.execute(
        """
        INSERT INTO p3_meta(key,value,updated_at_ms)
        VALUES(?,?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at_ms=excluded.updated_at_ms
        """,
        (key, _json(payload), now),
    )


def _migrate_legacy_state(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "p3_dual40_state"):
        return
    cols = _columns(conn, "p3_dual40_state")
    if "asset" in cols:
        return
    rows = [dict(row) for row in conn.execute("SELECT * FROM p3_dual40_state").fetchall()]
    review_required = any(
        int(row.get("level_index") or 0) != 0
        or abs(float(row.get("loss_pool_usdc") or 0.0)) > 1e-9
        or bool(row.get("hard_stopped"))
        for row in rows
    )
    conn.execute("ALTER TABLE p3_dual40_state RENAME TO p3_dual40_state_legacy_global")
    conn.execute(
        """
        CREATE TABLE p3_dual40_state (
            scope               TEXT NOT NULL CHECK(scope IN ('PAPER','LIVE')),
            asset               TEXT NOT NULL,
            level_index         INTEGER NOT NULL DEFAULT 0,
            loss_pool_usdc      REAL NOT NULL DEFAULT 0,
            hard_stopped        INTEGER NOT NULL DEFAULT 0,
            hard_stop_reason    TEXT,
            updated_at_ms       INTEGER NOT NULL,
            last_cycle_id       INTEGER,
            migration_note      TEXT,
            PRIMARY KEY(scope, asset)
        )
        """
    )
    if review_required:
        _write_meta(
            conn,
            "dual40_legacy_global_state_review_required",
            {
                "status": "LEGACY_GLOBAL_STATE_REVIEW_REQUIRED",
                "reason": "legacy scope-level ladder/loss pool was not assigned to any asset",
                "legacy_rows": rows,
            },
        )


def _migrate_cycles_asset(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "p3_dual40_cycles"):
        return
    cols = _columns(conn, "p3_dual40_cycles")
    if "asset" not in cols:
        conn.execute("ALTER TABLE p3_dual40_cycles ADD COLUMN asset TEXT")
    rows = conn.execute(
        """
        SELECT id,combo_key FROM p3_dual40_cycles
        WHERE asset IS NULL OR trim(asset)=''
        """
    ).fetchall()
    for row in rows:
        asset = asset_from_combo_key(str(row["combo_key"]))
        conn.execute(
            "UPDATE p3_dual40_cycles SET asset=? WHERE id=?",
            (asset, int(row["id"])),
        )


def _migrate_cycle_fill_prices(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "p3_dual40_cycles"):
        return
    columns = _columns(conn, "p3_dual40_cycles")
    for name in ("up_fill_price", "down_fill_price"):
        if name not in columns:
            conn.execute(f"ALTER TABLE p3_dual40_cycles ADD COLUMN {name} REAL")


def ensure_dual40_schema(conn: sqlite3.Connection) -> None:
    ensure_p3_schema(conn)
    _migrate_legacy_state(conn)
    conn.executescript(DUAL40_DDL)
    _migrate_cycles_asset(conn)
    _migrate_cycle_fill_prices(conn)
    now = int(time.time() * 1000)
    for scope in ("PAPER", "LIVE"):
        for asset in DUAL40_ASSETS:
            conn.execute(
                """
                INSERT INTO p3_dual40_state(
                    scope,asset,level_index,loss_pool_usdc,hard_stopped,updated_at_ms
                )
                VALUES(?,?,0,0,0,?)
                ON CONFLICT(scope,asset) DO NOTHING
                """,
                (scope, asset, now),
            )
    conn.execute("DROP INDEX IF EXISTS idx_p3_dual40_cycles_active")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_p3_dual40_cycles_active_lookup
        ON p3_dual40_cycles(scope,asset,status,updated_at_ms DESC)
        """
    )
    conn.execute(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_p3_dual40_active_scope_asset
        ON p3_dual40_cycles(scope,asset)
        WHERE status IN ({",".join(repr(status) for status in sorted(ACTIVE_STATUSES))})
        """
    )
    conn.commit()


def connect_dual40(path: str) -> sqlite3.Connection:
    conn = connect_p3(path)
    ensure_dual40_schema(conn)
    return conn


def ladder_state(conn: sqlite3.Connection, scope: str, asset: str) -> dict[str, Any]:
    asset_value = normalize_asset(asset)
    row = conn.execute(
        "SELECT * FROM p3_dual40_state WHERE scope=? AND asset=?",
        (scope.upper(), asset_value),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"DUAL40 state missing for {scope}:{asset_value}")
    return dict(row)


def set_ladder_state(
    conn: sqlite3.Connection,
    *,
    scope: str,
    asset: str,
    level_index: int,
    loss_pool_usdc: float,
    hard_stopped: bool,
    hard_stop_reason: str | None,
    last_cycle_id: int | None = None,
    commit: bool = True,
) -> None:
    asset_value = normalize_asset(asset)
    conn.execute(
        """
        UPDATE p3_dual40_state
        SET level_index=?,loss_pool_usdc=?,hard_stopped=?,hard_stop_reason=?,
            updated_at_ms=?,last_cycle_id=COALESCE(?,last_cycle_id)
        WHERE scope=? AND asset=?
        """,
        (
            int(level_index),
            max(0.0, float(loss_pool_usdc)),
            int(bool(hard_stopped)),
            hard_stop_reason,
            int(time.time() * 1000),
            last_cycle_id,
            scope.upper(),
            asset_value,
        ),
    )
    if commit:
        conn.commit()


def active_cycle(
    conn: sqlite3.Connection,
    scope: str | None = None,
    asset: str | None = None,
) -> dict[str, Any] | None:
    cycles = active_cycles(conn, scope=scope, asset=asset, limit=1)
    return cycles[0] if cycles else None


def active_cycles(
    conn: sqlite3.Connection,
    scope: str | None = None,
    asset: str | None = None,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    where = [f"status IN ({placeholders})"]
    params: list[Any] = list(sorted(ACTIVE_STATUSES))
    if scope is not None:
        where.append("scope=?")
        params.append(scope.upper())
    if asset is not None:
        where.append("asset=?")
        params.append(normalize_asset(asset))
    sql = f"""
        SELECT * FROM p3_dual40_cycles
        WHERE {" AND ".join(where)}
        ORDER BY id DESC
    """
    if limit is not None:
        sql += " LIMIT ?"
        params.append(max(1, int(limit)))
    rows = conn.execute(sql, tuple(params)).fetchall()
    return [_cycle_dict(row) for row in rows]


def cycle_for_condition(
    conn: sqlite3.Connection,
    *,
    scope: str,
    asset: str | None = None,
    condition_id: str,
) -> dict[str, Any] | None:
    params: list[Any] = [scope.upper(), str(condition_id)]
    extra = ""
    if asset is not None:
        extra = " AND asset=?"
        params.append(normalize_asset(asset))
    row = conn.execute(
        f"SELECT * FROM p3_dual40_cycles WHERE scope=? AND condition_id=?{extra}",
        tuple(params),
    ).fetchone()
    return _cycle_dict(row) if row is not None else None


def create_cycle(
    conn: sqlite3.Connection,
    *,
    scope: str,
    asset: str,
    session_id: str | None,
    condition_id: str,
    combo_key: str,
    market_end_ts_ms: int,
    level_index: int,
    target_shares: float,
    maker_price: float,
    status: str,
    gate: dict[str, Any],
    up_token_id: str,
    down_token_id: str,
    loss_pool_before_usdc: float,
    before_up_shares: float = 0.0,
    before_down_shares: float = 0.0,
    details: dict[str, Any] | None = None,
) -> int:
    now = int(time.time() * 1000)
    asset_value = normalize_asset(asset)
    if asset_from_combo_key(combo_key) != asset_value:
        raise ValueError("DUAL40 asset does not match combo_key")

    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        if cycle_for_condition(
            conn,
            scope=scope,
            asset=asset_value,
            condition_id=condition_id,
        ):
            raise sqlite3.IntegrityError("duplicate DUAL40 condition for scope/asset")
        if active_cycle(conn, scope=scope, asset=asset_value) is not None:
            raise sqlite3.IntegrityError("active DUAL40 cycle already exists for scope/asset")
        cur = conn.execute(
            """
            INSERT INTO p3_dual40_cycles(
                scope,asset,session_id,condition_id,combo_key,market_end_ts_ms,
                level_index,target_shares,maker_price,status,gate_json,
                up_token_id,down_token_id,before_up_shares,before_down_shares,
                loss_pool_before_usdc,details_json,created_at_ms,updated_at_ms
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                scope.upper(),
                asset_value,
                session_id,
                str(condition_id),
                str(combo_key),
                int(market_end_ts_ms),
                int(level_index),
                float(target_shares),
                float(maker_price),
                str(status),
                _json(gate),
                str(up_token_id),
                str(down_token_id),
                float(before_up_shares),
                float(before_down_shares),
                max(0.0, float(loss_pool_before_usdc)),
                _json(details or {}),
                now,
                now,
            ),
        )
        if owns_transaction:
            conn.commit()
        return int(cur.lastrowid)
    except Exception:
        if owns_transaction:
            conn.rollback()
        raise


def upsert_market_decision(
    conn: sqlite3.Connection,
    *,
    scope: str,
    asset: str,
    combo_key: str,
    condition_id: str,
    market_start_ts_ms: int | None,
    market_end_ts_ms: int | None,
    decision: str,
    reason: str | None = None,
    score: float | None = None,
    eligible: bool = False,
    opened_cycle_id: int | None = None,
    final_gate: dict[str, Any] | None = None,
    now_ms: int | None = None,
    commit: bool = True,
) -> None:
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    conn.execute(
        """
        INSERT INTO p3_dual40_market_decisions(
            scope,asset,combo_key,condition_id,market_start_ts_ms,market_end_ts_ms,
            first_seen_ms,last_seen_ms,decision,reason,score,eligible,opened_cycle_id,
            final_gate_json,created_at_ms,updated_at_ms
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(scope,condition_id) DO UPDATE SET
            asset=excluded.asset,
            combo_key=excluded.combo_key,
            market_start_ts_ms=excluded.market_start_ts_ms,
            market_end_ts_ms=excluded.market_end_ts_ms,
            last_seen_ms=excluded.last_seen_ms,
            decision=excluded.decision,
            reason=excluded.reason,
            score=excluded.score,
            eligible=excluded.eligible,
            opened_cycle_id=COALESCE(excluded.opened_cycle_id,opened_cycle_id),
            final_gate_json=excluded.final_gate_json,
            updated_at_ms=excluded.updated_at_ms
        """,
        (
            scope.upper(),
            normalize_asset(asset),
            str(combo_key),
            str(condition_id),
            market_start_ts_ms,
            market_end_ts_ms,
            now,
            now,
            str(decision),
            reason,
            score,
            int(bool(eligible)),
            opened_cycle_id,
            _json(final_gate or {}),
            now,
            now,
        ),
    )
    if commit:
        conn.commit()


def market_decisions(
    conn: sqlite3.Connection,
    *,
    scope: str | None = None,
    asset: str | None = None,
    limit: int | None = 100,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if scope is not None:
        where.append("scope=?")
        params.append(scope.upper())
    if asset is not None:
        where.append("asset=?")
        params.append(normalize_asset(asset))
    sql = "SELECT * FROM p3_dual40_market_decisions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at_ms DESC,id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(max(1, min(500, int(limit))))
    rows = conn.execute(sql, tuple(params)).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["final_gate"] = _decode(item.pop("final_gate_json", "{}"))
        out.append(item)
    return out


def update_cycle(
    conn: sqlite3.Connection,
    cycle_id: int,
    *,
    status: str | None = None,
    details_merge: dict[str, Any] | None = None,
    commit: bool = True,
    **fields: Any,
) -> None:
    allowed = {
        "gate_json",
        "up_order_id",
        "down_order_id",
        "before_up_shares",
        "before_down_shares",
        "up_filled_shares",
        "down_filled_shares",
        "up_fill_price",
        "down_fill_price",
        "matched_shares",
        "residual_side",
        "residual_shares",
        "official_result",
        "realized_pnl_usdc",
        "loss_pool_after_usdc",
        "merge_tx_hash",
        "heartbeat_id",
        "last_heartbeat_ms",
        "near_touch_up_41",
        "near_touch_down_41",
        "error_code",
        "orders_posted_at_ms",
        "orders_cancelled_at_ms",
        "resolved_at_ms",
    }
    pairs = ["updated_at_ms=?"]
    values: list[Any] = [int(time.time() * 1000)]
    if status is not None:
        pairs.append("status=?")
        values.append(str(status))
    for key, value in fields.items():
        if key not in allowed:
            continue
        pairs.append(f"{key}=?")
        values.append(value)

    if details_merge is not None:
        current = conn.execute(
            "SELECT details_json FROM p3_dual40_cycles WHERE id=?",
            (int(cycle_id),),
        ).fetchone()
        details = _decode(current[0] if current is not None else "{}")
        if not isinstance(details, dict):
            details = {}
        details.update(details_merge)
        pairs.append("details_json=?")
        values.append(_json(details))

    values.append(int(cycle_id))
    conn.execute(
        f"UPDATE p3_dual40_cycles SET {','.join(pairs)} WHERE id=?",
        values,
    )
    if commit:
        conn.commit()


def write_scan_status(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    now = int(time.time() * 1000)
    conn.execute(
        """
        INSERT INTO p3_meta(key,value,updated_at_ms)
        VALUES('dual40_latest_scan_json',?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at_ms=excluded.updated_at_ms
        """,
        (_json(payload), now),
    )
    conn.commit()


def read_scan_status(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        "SELECT value FROM p3_meta WHERE key='dual40_latest_scan_json'"
    ).fetchone()
    value = _decode(row[0]) if row is not None else {}
    return value if isinstance(value, dict) else {}


def reset_scope(
    conn: sqlite3.Connection,
    *,
    scope: str,
    asset: str | None = None,
    clear_cycles: bool = False,
    clear_decisions: bool = False,
    discard_active_paper: bool = False,
) -> None:
    scope_value = str(scope).upper()
    if discard_active_paper and scope_value != "PAPER":
        raise ValueError("active cycles may only be discarded for PAPER")
    if discard_active_paper and not clear_cycles:
        raise ValueError("discarding active PAPER cycles requires clear_cycles")
    if (
        active_cycle(conn, scope=scope_value, asset=asset) is not None
        and not discard_active_paper
    ):
        raise RuntimeError("cannot reset DUAL40 while a cycle is active")
    assets = (normalize_asset(asset),) if asset is not None else DUAL40_ASSETS
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        if clear_cycles:
            if asset is None:
                conn.execute(
                    "DELETE FROM p3_dual40_cycles WHERE scope=?",
                    (scope_value,),
                )
            else:
                conn.execute(
                    "DELETE FROM p3_dual40_cycles WHERE scope=? AND asset=?",
                    (scope_value, normalize_asset(asset)),
                )
        if clear_decisions:
            if asset is None:
                conn.execute(
                    "DELETE FROM p3_dual40_market_decisions WHERE scope=?",
                    (scope_value,),
                )
            else:
                conn.execute(
                    "DELETE FROM p3_dual40_market_decisions WHERE scope=? AND asset=?",
                    (scope_value, normalize_asset(asset)),
                )
        now_ms = int(time.time() * 1000)
        for asset_value in assets:
            conn.execute(
                """
                UPDATE p3_dual40_state
                SET level_index=0,loss_pool_usdc=0,hard_stopped=0,
                    hard_stop_reason=NULL,last_cycle_id=NULL,updated_at_ms=?
                WHERE scope=? AND asset=?
                """,
                (now_ms, scope_value, asset_value),
            )
        if owns_transaction:
            conn.commit()
    except Exception:
        if owns_transaction:
            conn.rollback()
        raise


def _cycle_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    item = dict(row)
    item["gate"] = _decode(item.pop("gate_json", "{}"))
    item["details"] = _decode(item.pop("details_json", "{}"))
    return item


def summary(path: str, *, limit: int = 50) -> dict[str, Any]:
    conn = connect_dual40(path)
    try:
        states = {
            scope: {asset: ladder_state(conn, scope, asset) for asset in DUAL40_ASSETS}
            for scope in ("PAPER", "LIVE")
        }
        rows = conn.execute(
            "SELECT * FROM p3_dual40_cycles ORDER BY id DESC LIMIT ?",
            (max(1, min(500, int(limit))),),
        ).fetchall()
        cycles = [_cycle_dict(row) for row in rows]
        settled = [
            cycle
            for cycle in cycles
            if cycle.get("realized_pnl_usdc") is not None
        ]
        pnl = sum(float(cycle.get("realized_pnl_usdc") or 0.0) for cycle in settled)
        return {
            "strategy": "DUAL40_MAKER_RECOVERY_V1",
            "ladder": list(DEFAULT_LADDER),
            "state": states,
            "active_cycle": active_cycle(conn),
            "active_cycles": active_cycles(conn),
            "scan": read_scan_status(conn),
            "market_decisions": market_decisions(conn, limit=limit),
            "cycles": cycles,
            "settled_cycles_in_view": len(settled),
            "realized_pnl_in_view_usdc": round(pnl, 6),
        }
    finally:
        conn.close()
