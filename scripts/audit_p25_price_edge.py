"""Read-only P2.5 forecast price-edge audit.

The report asks a narrower question than directional accuracy: if a forecast
probability disagreed with the market-implied UP probability by at least a given
threshold, did buying the cheaper side at the implied market price make money?

This is not an order-book fill simulator.  It is a fast first-pass screen for
whether the model has any price edge worth testing with executable books.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


SOURCES = {
    "validated_primary": ("p_up_calibrated", "p_up_raw"),
    "raw_model": ("p_up_raw",),
    "external": ("p_up_external",),
    "ptb_trained": ("p_up_ptb",),
    "ptb_heuristic": ("p_up_ptb_heuristic",),
    "research_forecast": ("forecast_p_up",),
}


def _connect_ro(path: str, timeout_sec: float) -> sqlite3.Connection:
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


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _source_probability(row: sqlite3.Row, columns: set[str], source: str) -> float | None:
    for column in SOURCES[source]:
        if column in columns and row[column] is not None:
            value = float(row[column])
            if 0.0 < value < 1.0:
                return value
    return None


def _trade(row: sqlite3.Row, p_up: float, min_edge: float) -> dict[str, Any] | None:
    market_up = row["p_up_market"]
    if market_up is None:
        return None
    market_up = float(market_up)
    if not 0.0 < market_up < 1.0:
        return None
    result = str(row["official_result"] or "").upper()
    if result not in {"UP", "DOWN"}:
        return None

    up_edge = p_up - market_up
    down_edge = market_up - p_up
    if up_edge >= min_edge:
        side = "UP"
        price = market_up
        edge = up_edge
    elif down_edge >= min_edge:
        side = "DOWN"
        price = 1.0 - market_up
        edge = down_edge
    else:
        return None
    if not 0.0 < price < 1.0:
        return None

    correct = side == result
    shares = 1.0 / price
    pnl = shares - 1.0 if correct else -1.0
    return {
        "side": side,
        "price": price,
        "edge": edge,
        "correct": correct,
        "pnl": pnl,
        "combo_key": str(row["combo_key"] or "UNKNOWN"),
    }


def _summarize(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {"n": 0}
    wins = sum(1 for item in trades if item["correct"])
    pnl = sum(float(item["pnl"]) for item in trades)
    avg_price = sum(float(item["price"]) for item in trades) / len(trades)
    sides = Counter(str(item["side"]) for item in trades)
    per_combo: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in trades:
        per_combo[str(item["combo_key"])].append(item)
    return {
        "n": len(trades),
        "wins": wins,
        "losses": len(trades) - wins,
        "hit_rate": round(wins / len(trades), 4),
        "pnl_per_1usdc_stake": round(pnl, 6),
        "roi": round(pnl / len(trades), 6),
        "avg_entry_price": round(avg_price, 6),
        "sides": dict(sides),
        "per_combo": {
            combo: {
                "n": len(items),
                "hit_rate": round(
                    sum(1 for item in items if item["correct"]) / len(items),
                    4,
                ),
                "roi": round(sum(float(item["pnl"]) for item in items) / len(items), 6),
            }
            for combo, items in sorted(per_combo.items())
        },
    }


def build_report(conn: sqlite3.Connection, thresholds: list[float]) -> dict[str, Any]:
    if "forecasts" not in _tables(conn):
        return {"status": "MISSING_FORECASTS_TABLE"}
    columns = _columns(conn, "forecasts")
    required = {"combo_key", "p_up_market", "official_result"}
    missing = sorted(required - columns)
    if missing:
        return {"status": "MISSING_COLUMNS", "missing": missing}

    select_columns = sorted(
        (required | {"decision"} | {col for cols in SOURCES.values() for col in cols})
        & columns
    )
    rows = conn.execute(
        f"""
        SELECT {', '.join(select_columns)}
        FROM forecasts
        WHERE official_result IN ('UP','DOWN') AND p_up_market IS NOT NULL
        """
    ).fetchall()

    report: dict[str, Any] = {
        "status": "OK",
        "settled_with_market_price": len(rows),
        "thresholds": thresholds,
        "sources": {},
    }
    for source in SOURCES:
        if not any(column in columns for column in SOURCES[source]):
            continue
        source_rows = [
            row for row in rows if _source_probability(row, columns, source) is not None
        ]
        by_threshold = {}
        for threshold in thresholds:
            trades = [
                trade
                for row in source_rows
                for trade in [_trade(row, _source_probability(row, columns, source), threshold)]
                if trade is not None
            ]
            by_threshold[f"{threshold:.3f}"] = _summarize(trades)
        report["sources"][source] = {
            "usable": len(source_rows),
            "thresholds": by_threshold,
        }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/direction_engine.sqlite")
    parser.add_argument("--sqlite-timeout-sec", type=float, default=3.0)
    parser.add_argument(
        "--thresholds",
        default="0,0.02,0.05,0.08,0.10,0.15",
        help="Comma-separated probability edge thresholds.",
    )
    args = parser.parse_args()
    thresholds = [
        float(item.strip())
        for item in str(args.thresholds).split(",")
        if item.strip()
    ]
    conn = _connect_ro(args.db, args.sqlite_timeout_sec)
    try:
        print(json.dumps(build_report(conn, thresholds), indent=2, sort_keys=True))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
