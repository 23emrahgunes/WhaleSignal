"""Read-only audit for P2.5/P2.6 prediction and paper-trade evidence.

This script intentionally does not import project recorders, because those
classes may migrate schemas on construction.  It opens SQLite databases in
read-only mode and reports what evidence is already present.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


def _connect_ro(path: str) -> sqlite3.Connection | None:
    resolved = Path(path).resolve()
    if not resolved.exists():
        return None
    conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=30000")
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


def _avg(rows: Iterable[sqlite3.Row], column: str) -> float | None:
    values = [float(row[column]) for row in rows if row[column] is not None]
    return round(sum(values) / len(values), 6) if values else None


def _prob_metrics(rows: list[sqlite3.Row], p_col: str, label_col: str) -> dict[str, Any]:
    usable = [
        row
        for row in rows
        if row[p_col] is not None and row[label_col] is not None
    ]
    if not usable:
        return {"n": 0}
    briers: list[float] = []
    losses: list[float] = []
    correct = 0
    for row in usable:
        p = max(1e-6, min(1.0 - 1e-6, float(row[p_col])))
        y = int(row[label_col])
        briers.append((p - y) ** 2)
        losses.append(-(y * math.log(p) + (1 - y) * math.log(1 - p)))
        correct += int((p >= 0.5) == bool(y))
    return {
        "n": len(usable),
        "accuracy_at_50": round(correct / len(usable), 4),
        "brier": round(sum(briers) / len(briers), 6),
        "log_loss": round(sum(losses) / len(losses), 6),
    }


def _direction_metrics(rows: list[sqlite3.Row], direction_col: str, result_col: str) -> dict[str, Any]:
    directional = [
        row
        for row in rows
        if str(row[direction_col] or "").upper() in {"UP", "DOWN"}
        and str(row[result_col] or "").upper() in {"UP", "DOWN"}
    ]
    if not directional:
        return {"n": 0}
    wins = sum(
        int(str(row[direction_col]).upper() == str(row[result_col]).upper())
        for row in directional
    )
    return {
        "n": len(directional),
        "wins": wins,
        "losses": len(directional) - wins,
        "accuracy": round(wins / len(directional), 4),
    }


def audit_p25(conn: sqlite3.Connection) -> dict[str, Any]:
    if "forecasts" not in _tables(conn):
        return {"status": "MISSING_TABLE"}
    cols = _columns(conn, "forecasts")
    rows = conn.execute("SELECT * FROM forecasts").fetchall()
    settled = [row for row in rows if row["official_result"] is not None]
    result: dict[str, Any] = {
        "status": "OK",
        "forecasts": len(rows),
        "settled": len(settled),
        "decisions": dict(Counter(str(row["decision"] or "NULL") for row in rows)),
        "abstain_reasons": dict(Counter(str(row["abstain_reason"] or "NULL") for row in rows).most_common(12)),
    }
    if settled:
        result["validated_signal"] = _direction_metrics(settled, "decision", "official_result")
        for column in (
            "brier",
            "external_brier",
            "ptb_brier",
            "ptb_heuristic_brier",
            "market_brier",
            "naive_brier",
            "log_loss",
        ):
            if column in cols:
                result[column] = _avg(settled, column)
    research_cols = {
        "forecast_direction",
        "forecast_p_up",
        "forecast_status",
        "forecast_grade",
        "forecast_correct",
        "forecast_brier",
    }
    if research_cols <= cols:
        research_rows = [row for row in settled if row["forecast_p_up"] is not None]
        result["research_forecast"] = {
            **_direction_metrics(research_rows, "forecast_direction", "official_result"),
            "n_probability": len(research_rows),
            "brier": _avg(research_rows, "forecast_brier"),
            "statuses": dict(Counter(str(row["forecast_status"] or "NULL") for row in research_rows)),
            "grades": dict(Counter(str(row["forecast_grade"] or "NULL") for row in research_rows)),
        }
    return result


def audit_p26(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = _tables(conn)
    result: dict[str, Any] = {
        "status": "OK",
        "table_counts": {
            table: int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in sorted(table for table in tables if table.startswith("p26_"))
        },
    }
    if "p26_oos_predictions" in tables:
        rows = conn.execute("SELECT * FROM p26_oos_predictions").fetchall()
        result["oos_predictions"] = {
            "n": len(rows),
            "model": _prob_metrics(rows, "p_up_raw", "official_label"),
            "market": (
                _prob_metrics(rows, "market_p_up", "official_label")
                if "market_p_up" in _columns(conn, "p26_oos_predictions")
                else {"n": 0}
            ),
            "classes": dict(Counter("UP" if int(row["official_label"]) == 1 else "DOWN" for row in rows)),
        }
    if "p26_paper_decisions" in tables:
        rows = conn.execute("SELECT * FROM p26_paper_decisions").fetchall()
        result["paper_decisions"] = {
            "n": len(rows),
            "would_open": sum(int(row["would_open"] or 0) for row in rows),
            "opened": sum(1 for row in rows if str(row["decision"]).upper() == "OPENED"),
            "reasons": dict(Counter(str(row["reason"] or "NULL") for row in rows).most_common(15)),
            "stages": dict(Counter(str(row["stage"] or "NULL") for row in rows)),
        }
    if "p26_paper_trades" in tables:
        rows = conn.execute("SELECT * FROM p26_paper_trades").fetchall()
        settled = [row for row in rows if str(row["status"]).upper() == "SETTLED"]
        pnl = sum(float(row["realized_pnl"] or 0.0) for row in settled)
        stake = sum(float(row["stake_usdc"] or 0.0) for row in settled)
        wins = sum(int(row["correct"] or 0) for row in settled)
        result["paper_trades"] = {
            "n": len(rows),
            "settled": len(settled),
            "wins": wins,
            "losses": len(settled) - wins,
            "hit_rate": round(wins / len(settled), 4) if settled else None,
            "pnl_usdc": round(pnl, 6),
            "roi": round(pnl / stake, 6) if stake else None,
            "statuses": dict(Counter(str(row["status"] or "NULL") for row in rows)),
            "reasons": dict(Counter(str(row["reason"] or "NULL") for row in rows).most_common(15)),
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--p25-db", default="data/direction_engine.sqlite")
    parser.add_argument("--p26-db", default="data/p26_research.sqlite")
    args = parser.parse_args()

    output: dict[str, Any] = {}
    p25 = _connect_ro(args.p25_db)
    if p25 is None:
        output["p25"] = {"status": "DB_NOT_FOUND", "path": args.p25_db}
    else:
        try:
            output["p25"] = audit_p25(p25)
        finally:
            p25.close()

    p26 = _connect_ro(args.p26_db)
    if p26 is None:
        output["p26"] = {"status": "DB_NOT_FOUND", "path": args.p26_db}
    else:
        try:
            output["p26"] = audit_p26(p26)
        finally:
            p26.close()

    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
