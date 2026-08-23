"""Realized LIVE PnL/risk ledger for P3.

The existing p3_live_cycles table remains the execution audit trail. This sidecar
ledger records only sanitized numeric outcomes needed for live risk controls.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any

DDL = """
CREATE TABLE IF NOT EXISTS p3_live_ledger (
    cycle_id                          INTEGER PRIMARY KEY,
    session_id                        TEXT NOT NULL,
    window_id                         INTEGER NOT NULL,
    combo_key                         TEXT NOT NULL,
    quantity_shares                   REAL NOT NULL,
    planned_capital_usdc              REAL NOT NULL,
    planned_net_profit_usdc           REAL NOT NULL,
    planned_net_roi                   REAL NOT NULL,
    projected_worst_unwind_loss_usdc  REAL NOT NULL,
    collateral_before_usdc            REAL,
    collateral_after_usdc             REAL,
    realized_pnl_usdc                 REAL,
    realized_roi                      REAL,
    one_leg_event                     INTEGER NOT NULL DEFAULT 0,
    unwind_attempts                   INTEGER NOT NULL DEFAULT 0,
    outcome                           TEXT NOT NULL,
    created_at_ms                     INTEGER NOT NULL,
    updated_at_ms                     INTEGER NOT NULL,
    FOREIGN KEY(cycle_id) REFERENCES p3_live_cycles(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_p3_live_ledger_time
ON p3_live_ledger(created_at_ms DESC);
CREATE INDEX IF NOT EXISTS idx_p3_live_ledger_outcome
ON p3_live_ledger(outcome,created_at_ms DESC);
"""


def ensure_live_ledger_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    conn.commit()


def create_live_ledger_row(
    conn: sqlite3.Connection,
    *,
    cycle_id: int,
    session_id: str,
    window_id: int,
    combo_key: str,
    quantity_shares: float,
    planned_capital_usdc: float,
    planned_net_profit_usdc: float,
    planned_net_roi: float,
    projected_worst_unwind_loss_usdc: float,
    collateral_before_usdc: float | None,
    outcome: str = "PRE_SUBMIT_CLAIMED",
) -> None:
    now = int(time.time() * 1000)
    conn.execute(
        """
        INSERT INTO p3_live_ledger(
            cycle_id,session_id,window_id,combo_key,quantity_shares,
            planned_capital_usdc,planned_net_profit_usdc,planned_net_roi,
            projected_worst_unwind_loss_usdc,collateral_before_usdc,
            outcome,created_at_ms,updated_at_ms
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            int(cycle_id), str(session_id), int(window_id), str(combo_key),
            float(quantity_shares), float(planned_capital_usdc),
            float(planned_net_profit_usdc), float(planned_net_roi),
            float(projected_worst_unwind_loss_usdc),
            None if collateral_before_usdc is None else float(collateral_before_usdc),
            str(outcome), now, now,
        ),
    )
    conn.commit()


def finalize_live_ledger_row(
    conn: sqlite3.Connection,
    *,
    cycle_id: int,
    outcome: str,
    collateral_after_usdc: float | None,
    one_leg_event: bool = False,
    unwind_attempts: int = 0,
) -> dict[str, float | None]:
    row = conn.execute(
        "SELECT * FROM p3_live_ledger WHERE cycle_id=?",
        (int(cycle_id),),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"live ledger row missing for cycle {cycle_id}")
    before = row["collateral_before_usdc"]
    after = None if collateral_after_usdc is None else float(collateral_after_usdc)
    pnl: float | None = None
    roi: float | None = None
    if before is not None and after is not None:
        pnl = after - float(before)
        capital = float(row["planned_capital_usdc"] or 0.0)
        roi = pnl / capital if capital > 0 else None
    conn.execute(
        """
        UPDATE p3_live_ledger
        SET collateral_after_usdc=?,realized_pnl_usdc=?,realized_roi=?,
            one_leg_event=?,unwind_attempts=?,outcome=?,updated_at_ms=?
        WHERE cycle_id=?
        """,
        (
            after, pnl, roi, int(bool(one_leg_event)), int(unwind_attempts),
            str(outcome), int(time.time() * 1000), int(cycle_id),
        ),
    )
    conn.commit()
    return {"realized_pnl_usdc": pnl, "realized_roi": roi}


def rolling_24h_gross_loss_usdc(conn: sqlite3.Connection, *, now_ms: int | None = None) -> float:
    cutoff = (int(time.time() * 1000) if now_ms is None else int(now_ms)) - 86_400_000
    row = conn.execute(
        """
        SELECT COALESCE(SUM(CASE WHEN realized_pnl_usdc < 0 THEN -realized_pnl_usdc ELSE 0 END),0)
        FROM p3_live_ledger
        WHERE created_at_ms>=?
        """,
        (cutoff,),
    ).fetchone()
    return float(row[0] or 0.0) if row else 0.0


def live_ledger_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    ensure_live_ledger_schema(conn)
    agg = conn.execute(
        """
        SELECT
            COUNT(*) AS cycles,
            SUM(CASE WHEN realized_pnl_usdc IS NOT NULL THEN 1 ELSE 0 END) AS realized_cycles,
            COALESCE(SUM(realized_pnl_usdc),0) AS realized_pnl,
            COALESCE(SUM(CASE WHEN realized_pnl_usdc < 0 THEN -realized_pnl_usdc ELSE 0 END),0) AS gross_loss,
            COALESCE(SUM(one_leg_event),0) AS one_leg_events,
            AVG(CASE WHEN realized_pnl_usdc IS NOT NULL THEN realized_pnl_usdc END) AS avg_pnl
        FROM p3_live_ledger
        """
    ).fetchone()
    recent = [dict(row) for row in conn.execute(
        """
        SELECT cycle_id,session_id,window_id,combo_key,quantity_shares,
               planned_capital_usdc,planned_net_profit_usdc,planned_net_roi,
               projected_worst_unwind_loss_usdc,collateral_before_usdc,
               collateral_after_usdc,realized_pnl_usdc,realized_roi,
               one_leg_event,unwind_attempts,outcome,created_at_ms,updated_at_ms
        FROM p3_live_ledger ORDER BY cycle_id DESC LIMIT 30
        """
    ).fetchall()]
    cycles = int(agg["cycles"] or 0)
    one_leg = int(agg["one_leg_events"] or 0)
    return {
        "cycles": cycles,
        "realized_cycles": int(agg["realized_cycles"] or 0),
        "realized_pnl_usdc": float(agg["realized_pnl"] or 0.0),
        "gross_loss_usdc": float(agg["gross_loss"] or 0.0),
        "average_realized_pnl_usdc": (
            float(agg["avg_pnl"]) if agg["avg_pnl"] is not None else None
        ),
        "one_leg_events": one_leg,
        "one_leg_rate": one_leg / cycles if cycles else 0.0,
        "rolling_24h_gross_loss_usdc": rolling_24h_gross_loss_usdc(conn),
        "recent": recent,
    }
