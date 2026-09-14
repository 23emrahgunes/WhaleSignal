"""Read-only HFT shadow audit over persisted P2.6 CLOB book history.

The goal is not to claim fills.  It identifies why an HFT-style maker/taker idea
is unlikely to work on the observed feed:

- source-to-receive latency from exchange timestamp to VPS observation,
- update gaps and stale-book rate,
- whether a 40c post-only BUY would have crossed the spread and been rejected,
- whether a taker buy at or below 40c had visible executable depth.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


def _connect_ro(path: str, *, timeout_sec: float) -> sqlite3.Connection:
    resolved = Path(path).resolve()
    conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True, timeout=timeout_sec)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute(f"PRAGMA busy_timeout={int(timeout_sec * 1000)}")
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _levels(raw: str | None) -> list[tuple[float, float]]:
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    levels: list[tuple[float, float]] = []
    if not isinstance(payload, list):
        return levels
    for item in payload:
        if not isinstance(item, list | tuple) or len(item) < 2:
            continue
        try:
            price = float(item[0])
            size = float(item[1])
        except (TypeError, ValueError):
            continue
        if 0.0 < price < 1.0 and size > 0.0:
            levels.append((price, size))
    return levels


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * pct)
    return round(ordered[min(len(ordered) - 1, max(0, index))], 3)


def _stats(values: Iterable[float]) -> dict[str, float | None]:
    data = list(values)
    if not data:
        return {"min": None, "p50": None, "p90": None, "p99": None, "max": None, "avg": None}
    return {
        "min": round(min(data), 3),
        "p50": _percentile(data, 0.50),
        "p90": _percentile(data, 0.90),
        "p99": _percentile(data, 0.99),
        "max": round(max(data), 3),
        "avg": round(sum(data) / len(data), 3),
    }


@dataclass
class GroupAudit:
    rows: int = 0
    source_to_recv: list[float] = field(default_factory=list)
    recv_gaps: list[float] = field(default_factory=list)
    stale_rows: int = 0
    empty_asks: int = 0
    post_only_40_cross_reject: int = 0
    maker_40_rest_possible: int = 0
    taker_40_visible_depth_rows: int = 0
    taker_40_visible_depth_shares: float = 0.0
    taker_40_visible_notional: float = 0.0
    best_ask_values: list[float] = field(default_factory=list)
    previous_recv_ms: int | None = None

    def add(
        self,
        *,
        recv_ms: int,
        source_ms: int,
        asks: list[tuple[float, float]],
        maker_price: float,
        stale_after_ms: int,
    ) -> None:
        self.rows += 1
        latency = max(0, recv_ms - source_ms)
        self.source_to_recv.append(float(latency))
        if self.previous_recv_ms is not None:
            self.recv_gaps.append(float(max(0, recv_ms - self.previous_recv_ms)))
        self.previous_recv_ms = recv_ms
        if latency > stale_after_ms:
            self.stale_rows += 1
        if not asks:
            self.empty_asks += 1
            return
        best_ask = min(price for price, _ in asks)
        self.best_ask_values.append(best_ask)
        if best_ask <= maker_price + 1e-12:
            self.post_only_40_cross_reject += 1
        else:
            self.maker_40_rest_possible += 1
        executable = [(price, size) for price, size in asks if price <= maker_price + 1e-12]
        if executable:
            shares = sum(size for _, size in executable)
            notional = sum(price * size for price, size in executable)
            self.taker_40_visible_depth_rows += 1
            self.taker_40_visible_depth_shares += shares
            self.taker_40_visible_notional += notional

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "source_to_recv_ms": _stats(self.source_to_recv),
            "recv_gap_ms": _stats(self.recv_gaps),
            "best_ask": _stats(self.best_ask_values),
            "stale_rows": self.stale_rows,
            "stale_rate": round(self.stale_rows / self.rows, 4) if self.rows else None,
            "empty_ask_rate": round(self.empty_asks / self.rows, 4) if self.rows else None,
            "post_only_40_cross_reject_rate": (
                round(self.post_only_40_cross_reject / self.rows, 4) if self.rows else None
            ),
            "maker_40_rest_possible_rate": (
                round(self.maker_40_rest_possible / self.rows, 4) if self.rows else None
            ),
            "taker_40_visible_depth_rate": (
                round(self.taker_40_visible_depth_rows / self.rows, 4) if self.rows else None
            ),
            "taker_40_visible_depth_shares_avg_when_seen": (
                round(self.taker_40_visible_depth_shares / self.taker_40_visible_depth_rows, 6)
                if self.taker_40_visible_depth_rows else None
            ),
            "taker_40_visible_notional_avg_when_seen": (
                round(self.taker_40_visible_notional / self.taker_40_visible_depth_rows, 6)
                if self.taker_40_visible_depth_rows else None
            ),
        }


def _load_recent_rows(
    conn: sqlite3.Connection,
    *,
    since_ms: int,
    limit: int,
    combo_like: str | None,
) -> list[sqlite3.Row]:
    params: list[Any] = [int(since_ms), int(limit)]
    combo_clause = ""
    if combo_like:
        combo_clause = "AND combo_key LIKE ?"
        params = [int(since_ms), str(combo_like), int(limit)]
    return conn.execute(
        f"""
        SELECT condition_id,combo_key,side,token_id,recv_ts_ms,source_ts_ms,
               sequence,bids_json,asks_json
        FROM p26_clob_books
        WHERE recv_ts_ms>=?
          {combo_clause}
        ORDER BY recv_ts_ms ASC,id ASC
        LIMIT ?
        """,
        params,
    ).fetchall()


def _ms_age(now_ms: int, value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round((now_ms - int(value)) / 1000, 3)
    except (TypeError, ValueError):
        return None


def _diagnostics(
    conn: sqlite3.Connection,
    *,
    now_ms: int,
    since_ms: int,
    combo_like: str | None,
) -> dict[str, Any]:
    summary = conn.execute(
        """
        SELECT count(*) AS rows,
               min(recv_ts_ms) AS min_recv_ts_ms,
               max(recv_ts_ms) AS max_recv_ts_ms,
               count(DISTINCT combo_key) AS combos,
               count(DISTINCT condition_id) AS conditions
        FROM p26_clob_books
        """
    ).fetchone()
    recent_count = conn.execute(
        "SELECT count(*) FROM p26_clob_books WHERE recv_ts_ms>=?",
        (int(since_ms),),
    ).fetchone()[0]
    filtered_count = None
    if combo_like:
        filtered_count = conn.execute(
            """
            SELECT count(*)
            FROM p26_clob_books
            WHERE recv_ts_ms>=? AND combo_key LIKE ?
            """,
            (int(since_ms), str(combo_like)),
        ).fetchone()[0]
    combos = conn.execute(
        """
        SELECT combo_key,
               count(*) AS rows,
               max(recv_ts_ms) AS max_recv_ts_ms
        FROM p26_clob_books
        GROUP BY combo_key
        ORDER BY max_recv_ts_ms DESC
        LIMIT 20
        """
    ).fetchall()
    return {
        "table_rows": int(summary["rows"] or 0),
        "table_conditions": int(summary["conditions"] or 0),
        "table_combos": int(summary["combos"] or 0),
        "min_recv_ts_ms": summary["min_recv_ts_ms"],
        "max_recv_ts_ms": summary["max_recv_ts_ms"],
        "last_book_age_sec": _ms_age(now_ms, summary["max_recv_ts_ms"]),
        "recent_rows_before_combo_filter": int(recent_count or 0),
        "recent_rows_after_combo_filter": (
            int(filtered_count) if filtered_count is not None else None
        ),
        "sample_recent_combos": [
            {
                "combo_key": row["combo_key"],
                "rows": int(row["rows"] or 0),
                "last_age_sec": _ms_age(now_ms, row["max_recv_ts_ms"]),
            }
            for row in combos
        ],
    }


def build_report(
    conn: sqlite3.Connection,
    *,
    lookback_minutes: float,
    limit: int,
    maker_price: float,
    stale_after_ms: int,
    combo_like: str | None,
) -> dict[str, Any]:
    if "p26_clob_books" not in _tables(conn):
        return {"status": "MISSING_P26_CLOB_BOOKS"}
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - int(float(lookback_minutes) * 60_000)
    rows = _load_recent_rows(
        conn,
        since_ms=since_ms,
        limit=limit,
        combo_like=combo_like,
    )
    diagnostics = _diagnostics(
        conn,
        now_ms=now_ms,
        since_ms=since_ms,
        combo_like=combo_like,
    )
    overall = GroupAudit()
    by_combo: dict[str, GroupAudit] = defaultdict(GroupAudit)
    by_combo_side: dict[str, GroupAudit] = defaultdict(GroupAudit)
    conditions = set()
    sides = Counter()
    for row in rows:
        recv_ms = int(row["recv_ts_ms"])
        source_ms = int(row["source_ts_ms"])
        asks = _levels(row["asks_json"])
        combo = str(row["combo_key"] or "UNKNOWN")
        side = str(row["side"] or "UNKNOWN")
        key = f"{combo}:{side}"
        conditions.add(str(row["condition_id"]))
        sides[side] += 1
        overall.add(
            recv_ms=recv_ms,
            source_ms=source_ms,
            asks=asks,
            maker_price=maker_price,
            stale_after_ms=stale_after_ms,
        )
        by_combo[combo].add(
            recv_ms=recv_ms,
            source_ms=source_ms,
            asks=asks,
            maker_price=maker_price,
            stale_after_ms=stale_after_ms,
        )
        by_combo_side[key].add(
            recv_ms=recv_ms,
            source_ms=source_ms,
            asks=asks,
            maker_price=maker_price,
            stale_after_ms=stale_after_ms,
        )
    return {
        "status": "OK",
        "created_at_ms": now_ms,
        "db_rows_scanned": len(rows),
        "diagnostics": diagnostics,
        "lookback_minutes": lookback_minutes,
        "maker_price": maker_price,
        "stale_after_ms": stale_after_ms,
        "condition_count": len(conditions),
        "sides": dict(sides),
        "overall": overall.to_dict(),
        "by_combo": {
            combo: audit.to_dict()
            for combo, audit in sorted(by_combo.items())
        },
        "by_combo_side": {
            key: audit.to_dict()
            for key, audit in sorted(by_combo_side.items())
        },
        "interpretation": {
            "post_only_40_cross_reject": "A BUY post-only at maker_price would be marketable against visible asks and should be rejected, not filled.",
            "maker_40_rest_possible": "A BUY post-only could rest, but this report does not prove queue fill.",
            "taker_40_visible_depth": "A taker buy at maker_price had visible ask depth; this is execution, not maker edge.",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/p26_research.sqlite")
    parser.add_argument("--lookback-minutes", type=float, default=30.0)
    parser.add_argument("--limit", type=int, default=200_000)
    parser.add_argument("--maker-price", type=float, default=0.40)
    parser.add_argument("--stale-after-ms", type=int, default=500)
    parser.add_argument("--sqlite-timeout-sec", type=float, default=3.0)
    parser.add_argument("--combo-like", help="Example: BTC:% or BTC:5m")
    args = parser.parse_args()
    conn = _connect_ro(args.db, timeout_sec=args.sqlite_timeout_sec)
    try:
        report = build_report(
            conn,
            lookback_minutes=args.lookback_minutes,
            limit=max(1, int(args.limit)),
            maker_price=float(args.maker_price),
            stale_after_ms=int(args.stale_after_ms),
            combo_like=args.combo_like,
        )
        print(json.dumps(report, indent=2, sort_keys=True))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
