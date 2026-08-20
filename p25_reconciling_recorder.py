"""Restart-safe paper recorder helpers.

Normal paper settlement is triggered by an in-memory market-resolution callback.
A deploy/restart can erase that market reference after a paper position was opened
but before Gamma finalized the market.  This subclass exposes the still-OPEN rows
and can settle one condition idempotently from an authoritative official result.

Simulation only: no credentials, signing, orders or execution.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from p25_paper import settle_paper_trade
from p25_paper_recorder import P25PaperRecorder

log = logging.getLogger("direction_engine.paper_reconcile")


class P25ReconcilingPaperRecorder(P25PaperRecorder):
    """Paper recorder with explicit stale-OPEN reconciliation primitives."""

    def open_paper_trades(self) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT id, condition_id, market_id, combo_key, asset, horizon, slug,
                   strategy_version, attempted_at, side, shares, stake_usdc,
                   fee_usdc, status
            FROM paper_trades
            WHERE strategy_version=? AND status='OPEN'
            ORDER BY attempted_at ASC, id ASC
            """,
            (self.paper_policy.strategy_version,),
        ).fetchall()
        return [dict(row) for row in rows]

    def settle_open_paper_condition(
        self,
        condition_id: str,
        official_result: str,
        *,
        settled_at: Optional[float] = None,
        source: Optional[str] = None,
    ) -> int:
        """Settle all OPEN paper rows for one condition id.

        The operation is idempotent: once a row is SETTLED it is no longer selected.
        It deliberately updates only the paper simulation table; normal live callbacks
        continue to own the market/forecast/model-label path.
        """
        result = str(official_result or "").strip().upper()
        if result not in {"UP", "DOWN"}:
            raise ValueError("official_result UP veya DOWN olmali")
        if not condition_id:
            return 0

        rows = self.conn.execute(
            """
            SELECT id, combo_key, side, shares, stake_usdc, fee_usdc
            FROM paper_trades
            WHERE condition_id=? AND strategy_version=? AND status='OPEN'
            ORDER BY id ASC
            """,
            (condition_id, self.paper_policy.strategy_version),
        ).fetchall()
        if not rows:
            return 0

        timestamp = float(settled_at or time.time())
        count = 0
        for row in rows:
            settlement = settle_paper_trade(
                side=str(row["side"]),
                official_result=result,
                shares=float(row["shares"]),
                stake_usdc=float(row["stake_usdc"]),
                fee_usdc=float(row["fee_usdc"] or 0.0),
            )
            cursor = self.conn.execute(
                """
                UPDATE paper_trades SET
                    status='SETTLED', official_result=?, correct=?,
                    gross_payout=?, realized_pnl=?, roi=?, settled_at=?
                WHERE id=? AND status='OPEN'
                """,
                (
                    result,
                    1 if settlement.correct else 0,
                    settlement.gross_payout,
                    settlement.realized_pnl,
                    settlement.roi,
                    timestamp,
                    row["id"],
                ),
            )
            if cursor.rowcount:
                count += 1
                log.info(
                    "PAPER RECONCILE SETTLE %s side=%s result=%s "
                    "correct=%s pnl=%+.4f source=%s",
                    row["combo_key"],
                    row["side"],
                    result,
                    settlement.correct,
                    settlement.realized_pnl,
                    source or "authoritative_gamma",
                )
        self.conn.commit()
        return count
