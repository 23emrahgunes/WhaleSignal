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
    scope               TEXT PRIMARY KEY CHECK(scope IN ('PAPER','LIVE')),
    level_index         INTEGER NOT NULL DEFAULT 0,
    loss_pool_usdc      REAL NOT NULL DEFAULT 0,
    hard_stopped        INTEGER NOT NULL DEFAULT 0,
    hard_stop_reason    TEXT,
    updated_at_ms       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS p3_dual40_cycles (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    scope                   TEXT NOT NULL CHECK(scope IN ('PAPER','LIVE')),
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
CREATE INDEX IF NOT EXISTS idx_p3_dual40_cycles_active
ON p3_dual40_cycles(scope,status,updated_at_ms DESC);
CREATE INDEX IF NOT EXISTS idx_p3_dual40_cycles_time
ON p3_dual40_cycles(created_at_ms DESC);
"""


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _decode(value: object) -> Any:
    try:
        return json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def ensure_dual40_schema(conn: sqlite3.Connection) -> None:
    ensure_p3_schema(conn)
    conn.executescript(DUAL40_DDL)
    now = int(time.time() * 1000)
    for scope in ("PAPER", "LIVE"):
        conn.execute(
            """
            INSERT INTO p3_dual40_state(scope,level_index,loss_pool_usdc,hard_stopped,updated_at_ms)
            VALUES(?,0,0,0,?)
            ON CONFLICT(scope) DO NOTHING
            """,
            (scope, now),
        )
    conn.commit()


def connect_dual40(path: str) -> sqlite3.Connection:
    conn = connect_p3(path)
    ensure_dual40_schema(conn)
    return conn


def ladder_state(conn: sqlite3.Connection, scope: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM p3_dual40_state WHERE scope=?",
        (scope.upper(),),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"DUAL40 state missing for {scope}")
    return dict(row)


def set_ladder_state(
    conn: sqlite3.Connection,
    *,
    scope: str,
    level_index: int,
    loss_pool_usdc: float,
    hard_stopped: bool,
    hard_stop_reason: str | None,
) -> None:
    conn.execute(
        """
        UPDATE p3_dual40_state
        SET level_index=?,loss_pool_usdc=?,hard_stopped=?,hard_stop_reason=?,updated_at_ms=?
        WHERE scope=?
        """,
        (
            int(level_index),
            max(0.0, float(loss_pool_usdc)),
            int(bool(hard_stopped)),
            hard_stop_reason,
            int(time.time() * 1000),
            scope.upper(),
        ),
    )
    conn.commit()


def active_cycle(conn: sqlite3.Connection) -> dict[str, Any] | None:
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    row = conn.execute(
        f"""
        SELECT * FROM p3_dual40_cycles
        WHERE status IN ({placeholders})
        ORDER BY id DESC LIMIT 1
        """,
        tuple(sorted(ACTIVE_STATUSES)),
    ).fetchone()
    return _cycle_dict(row) if row is not None else None


def cycle_for_condition(
    conn: sqlite3.Connection,
    *,
    scope: str,
    condition_id: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM p3_dual40_cycles WHERE scope=? AND condition_id=?",
        (scope.upper(), str(condition_id)),
    ).fetchone()
    return _cycle_dict(row) if row is not None else None


def create_cycle(
    conn: sqlite3.Connection,
    *,
    scope: str,
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
    cur = conn.execute(
        """
        INSERT INTO p3_dual40_cycles(
            scope,session_id,condition_id,combo_key,market_end_ts_ms,
            level_index,target_shares,maker_price,status,gate_json,
            up_token_id,down_token_id,before_up_shares,before_down_shares,
            loss_pool_before_usdc,details_json,created_at_ms,updated_at_ms
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            scope.upper(),
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
    conn.commit()
    return int(cur.lastrowid)


def update_cycle(
    conn: sqlite3.Connection,
    cycle_id: int,
    *,
    status: str | None = None,
    details_merge: dict[str, Any] | None = None,
    **fields: Any,
) -> None:
    allowed = {
        "up_order_id",
        "down_order_id",
        "before_up_shares",
        "before_down_shares",
        "up_filled_shares",
        "down_filled_shares",
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
    clear_cycles: bool = False,
) -> None:
    if active_cycle(conn) is not None:
        raise RuntimeError("cannot reset DUAL40 while a cycle is active")
    set_ladder_state(
        conn,
        scope=scope,
        level_index=0,
        loss_pool_usdc=0.0,
        hard_stopped=False,
        hard_stop_reason=None,
    )
    if clear_cycles:
        conn.execute(
            "DELETE FROM p3_dual40_cycles WHERE scope=?",
            (scope.upper(),),
        )
        conn.commit()


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
            scope: ladder_state(conn, scope)
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
            "scan": read_scan_status(conn),
            "cycles": cycles,
            "settled_cycles_in_view": len(settled),
            "realized_pnl_in_view_usdc": round(pnl, 6),
        }
    finally:
        conn.close()
