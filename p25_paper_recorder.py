"""SQLite persistence and analytics for P2.5 paper trading.

The research forecast is still recorded at many checkpoints.  Paper trading is
intentionally different: exactly one canonical entry *attempt* is persisted for each
market and strategy version.  Eligible attempts buy the selected outcome at the
observed best ask plus configured slippage; skipped attempts keep their reason.  On
official resolution, positions are settled at binary payout 1/0.

This is simulation only.  There is no network call, order object, signature or key.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Iterable, Optional

from models import Decision, FeatureSnapshot, MarketRef
from p25_paper import (
    PaperPolicy,
    evaluate_paper_entry,
    settle_paper_trade,
)
from p25_research_recorder import P25ResearchRecorder

log = logging.getLogger("direction_engine.paper")


class P25PaperRecorder(P25ResearchRecorder):
    def __init__(self, db_path: str, cfg) -> None:  # noqa: ANN001
        self.paper_policy = PaperPolicy.from_settings(cfg)
        self.paper_recent_limit = int(cfg.paper_recent_limit)
        super().__init__(db_path)
        self._ensure_paper_schema()

    def _ensure_paper_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_trades (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                condition_id          TEXT NOT NULL,
                market_id             TEXT,
                combo_key             TEXT NOT NULL,
                asset                 TEXT NOT NULL,
                horizon               TEXT NOT NULL,
                slug                  TEXT,
                strategy_version      TEXT NOT NULL,
                checkpoint_sec        INTEGER NOT NULL,
                attempted_at          REAL NOT NULL,
                entry_tte_sec         REAL,
                side                  TEXT,
                forecast_p_up         REAL,
                selected_probability  REAL,
                forecast_confidence   REAL,
                forecast_grade        TEXT,
                forecast_status       TEXT,
                forecast_agreement    REAL,
                entry_bid             REAL,
                entry_ask             REAL,
                fill_price            REAL,
                forecast_edge         REAL,
                stake_usdc            REAL,
                shares                REAL,
                slippage              REAL,
                fee_usdc              REAL,
                status                TEXT NOT NULL,
                skip_reason           TEXT,
                official_result       TEXT,
                correct               INTEGER,
                gross_payout          REAL,
                realized_pnl          REAL,
                roi                   REAL,
                settled_at            REAL,
                UNIQUE(condition_id, strategy_version)
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_paper_status "
            "ON paper_trades(status, attempted_at)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_paper_asset_horizon "
            "ON paper_trades(asset, horizon, status)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_paper_combo "
            "ON paper_trades(combo_key, status)"
        )
        self.conn.commit()

    def _paper_totals(self) -> tuple[float, float, float]:
        realized = float(
            self.conn.execute(
                """
                SELECT COALESCE(SUM(realized_pnl), 0)
                FROM paper_trades WHERE status='SETTLED'
                """
            ).fetchone()[0]
            or 0.0
        )
        open_exposure = float(
            self.conn.execute(
                """
                SELECT COALESCE(SUM(COALESCE(stake_usdc,0)+COALESCE(fee_usdc,0)), 0)
                FROM paper_trades WHERE status='OPEN'
                """
            ).fetchone()[0]
            or 0.0
        )
        equity = self.paper_policy.starting_bankroll_usdc + realized
        return realized, open_exposure, equity

    def available_paper_bankroll(self) -> float:
        _realized, open_exposure, equity = self._paper_totals()
        return max(0.0, equity - open_exposure)

    def record_forecast(
        self,
        ref: MarketRef,
        snap: FeatureSnapshot,
        checkpoint: int,
        trace: dict,
    ) -> bool:
        inserted = super().record_forecast(ref, snap, checkpoint, trace)
        if not inserted:
            return False

        decision = evaluate_paper_entry(
            ref=ref,
            snap=snap,
            checkpoint=checkpoint,
            trace=trace,
            policy=self.paper_policy,
            available_bankroll_usdc=self.available_paper_bankroll(),
        )
        if decision is None:
            return True

        status = "OPEN" if decision.eligible else "SKIPPED"
        skip_reason = None if decision.eligible else decision.reason
        before = self.conn.total_changes
        self.conn.execute(
            """
            INSERT OR IGNORE INTO paper_trades (
                condition_id, market_id, combo_key, asset, horizon, slug,
                strategy_version, checkpoint_sec, attempted_at, entry_tte_sec,
                side, forecast_p_up, selected_probability, forecast_confidence,
                forecast_grade, forecast_status, forecast_agreement,
                entry_bid, entry_ask, fill_price, forecast_edge, stake_usdc,
                shares, slippage, fee_usdc, status, skip_reason
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ref.condition_id,
                ref.market_id,
                ref.combo.key,
                ref.combo.asset.value,
                ref.combo.horizon.value,
                ref.slug,
                self.paper_policy.strategy_version,
                checkpoint,
                snap.ts,
                snap.tte_sec,
                decision.side,
                trace.get("forecast_p_up"),
                decision.selected_probability,
                trace.get("forecast_confidence"),
                trace.get("forecast_grade"),
                trace.get("forecast_status"),
                trace.get("forecast_agreement"),
                decision.entry_bid,
                decision.entry_ask,
                decision.fill_price,
                decision.forecast_edge,
                decision.stake_usdc,
                decision.shares,
                decision.slippage,
                decision.fee_usdc,
                status,
                skip_reason,
            ),
        )
        self.conn.commit()
        created = self.conn.total_changes > before

        current = self.paper_trade_for_condition(ref.condition_id)
        if current:
            trace.update(
                {
                    "paper_trade_status": current.get("status"),
                    "paper_trade_side": current.get("side"),
                    "paper_trade_fill": current.get("fill_price"),
                    "paper_trade_skip_reason": current.get("skip_reason"),
                    "paper_trade_checkpoint": current.get("checkpoint_sec"),
                }
            )
        if created:
            if status == "OPEN":
                log.info(
                    "PAPER OPEN %s %s stake=%.2f ask=%.3f fill=%.3f edge=%+.3f",
                    ref.combo.key,
                    decision.side,
                    decision.stake_usdc,
                    decision.entry_ask or 0.0,
                    decision.fill_price or 0.0,
                    decision.forecast_edge or 0.0,
                )
            else:
                log.info(
                    "PAPER SKIP %s reason=%s checkpoint=T-%s",
                    ref.combo.key,
                    skip_reason,
                    checkpoint,
                )
        return True

    def settle(self, ref: MarketRef) -> None:
        official = ref.official_result or ref.resolved_outcome
        super().settle(ref)
        if not ref.condition_id or official is None:
            return

        result = official.value
        rows = self.conn.execute(
            """
            SELECT id, side, shares, stake_usdc, fee_usdc
            FROM paper_trades
            WHERE condition_id=? AND status='OPEN'
            """,
            (ref.condition_id,),
        ).fetchall()
        settled_at = ref.official_resolved_at or time.time()
        for row in rows:
            settlement = settle_paper_trade(
                side=str(row["side"]),
                official_result=result,
                shares=float(row["shares"]),
                stake_usdc=float(row["stake_usdc"]),
                fee_usdc=float(row["fee_usdc"] or 0.0),
            )
            self.conn.execute(
                """
                UPDATE paper_trades SET
                    status='SETTLED', official_result=?, correct=?,
                    gross_payout=?, realized_pnl=?, roi=?, settled_at=?
                WHERE id=?
                """,
                (
                    result,
                    1 if settlement.correct else 0,
                    settlement.gross_payout,
                    settlement.realized_pnl,
                    settlement.roi,
                    settled_at,
                    row["id"],
                ),
            )
            log.info(
                "PAPER SETTLE %s side=%s result=%s correct=%s pnl=%+.4f",
                ref.combo.key,
                row["side"],
                result,
                settlement.correct,
                settlement.realized_pnl,
            )

        self.conn.execute(
            """
            UPDATE paper_trades
            SET official_result=?, settled_at=COALESCE(settled_at, ?)
            WHERE condition_id=? AND status='SKIPPED'
            """,
            (result, settled_at, ref.condition_id),
        )
        self.conn.commit()

    def paper_trade_for_condition(self, condition_id: str) -> Optional[dict]:
        if not condition_id:
            return None
        row = self.conn.execute(
            """
            SELECT * FROM paper_trades
            WHERE condition_id=? AND strategy_version=?
            LIMIT 1
            """,
            (condition_id, self.paper_policy.strategy_version),
        ).fetchone()
        return self._paper_row(row) if row is not None else None

    @staticmethod
    def _paper_row(row: sqlite3.Row) -> dict:
        data = dict(row)
        for key in (
            "forecast_p_up",
            "selected_probability",
            "forecast_confidence",
            "forecast_agreement",
            "entry_bid",
            "entry_ask",
            "fill_price",
            "forecast_edge",
            "stake_usdc",
            "shares",
            "slippage",
            "fee_usdc",
            "gross_payout",
            "realized_pnl",
            "roi",
        ):
            if data.get(key) is not None:
                data[key] = round(float(data[key]), 6)
        return data

    @staticmethod
    def _paper_metrics(rows: Iterable[sqlite3.Row]) -> dict:
        records = list(rows)
        attempts = len(records)
        open_rows = [row for row in records if row["status"] == "OPEN"]
        settled = [row for row in records if row["status"] == "SETTLED"]
        skipped = [row for row in records if row["status"] == "SKIPPED"]
        wins = sum(1 for row in settled if int(row["correct"] or 0) == 1)
        losses = len(settled) - wins
        stake = sum(float(row["stake_usdc"] or 0.0) for row in settled)
        pnl = sum(float(row["realized_pnl"] or 0.0) for row in settled)
        traded = len(open_rows) + len(settled)
        fills = [
            float(row["fill_price"])
            for row in records
            if row["fill_price"] is not None and row["status"] in {"OPEN", "SETTLED"}
        ]
        confidences = [
            float(row["forecast_confidence"])
            for row in records
            if row["forecast_confidence"] is not None
        ]
        return {
            "attempts": attempts,
            "trades": traded,
            "open": len(open_rows),
            "settled": len(settled),
            "skipped": len(skipped),
            "coverage": round(traded / attempts, 4) if attempts else 0.0,
            "wins": wins,
            "losses": losses,
            "hit_rate": round(wins / len(settled), 4) if settled else None,
            "stake_settled_usdc": round(stake, 4),
            "realized_pnl_usdc": round(pnl, 4),
            "roi": round(pnl / stake, 4) if stake > 0 else None,
            "avg_fill_price": round(sum(fills) / len(fills), 4) if fills else None,
            "avg_forecast_confidence": (
                round(sum(confidences) / len(confidences), 4)
                if confidences
                else None
            ),
        }

    def paper_analytics(self, recent_limit: Optional[int] = None) -> dict:
        rows = self.conn.execute(
            """
            SELECT * FROM paper_trades
            WHERE strategy_version=?
            ORDER BY attempted_at ASC
            """,
            (self.paper_policy.strategy_version,),
        ).fetchall()
        overall = self._paper_metrics(rows)
        realized, open_exposure, equity = self._paper_totals()
        overall.update(
            {
                "starting_bankroll_usdc": round(
                    self.paper_policy.starting_bankroll_usdc, 4
                ),
                "equity_usdc": round(equity, 4),
                "open_exposure_usdc": round(open_exposure, 4),
                "available_bankroll_usdc": round(
                    max(0.0, equity - open_exposure), 4
                ),
                "realized_pnl_usdc": round(realized, 4),
            }
        )

        def grouped(field: str) -> dict[str, dict]:
            keys = sorted({str(row[field]) for row in rows})
            return {
                key: self._paper_metrics(
                    row for row in rows if str(row[field]) == key
                )
                for key in keys
            }

        skip_reasons: dict[str, int] = {}
        for row in rows:
            if row["status"] == "SKIPPED":
                reason = str(row["skip_reason"] or "UNKNOWN")
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

        limit = max(1, int(recent_limit or self.paper_recent_limit))
        recent_rows = self.conn.execute(
            """
            SELECT * FROM paper_trades
            WHERE strategy_version=?
            ORDER BY attempted_at DESC LIMIT ?
            """,
            (self.paper_policy.strategy_version, limit),
        ).fetchall()
        open_rows = self.conn.execute(
            """
            SELECT * FROM paper_trades
            WHERE strategy_version=? AND status='OPEN'
            ORDER BY attempted_at DESC
            """,
            (self.paper_policy.strategy_version,),
        ).fetchall()

        return {
            "enabled": self.paper_policy.enabled,
            "paper_only": True,
            "policy": self.paper_policy.to_dict(),
            "overall": overall,
            "per_asset": grouped("asset"),
            "per_horizon": grouped("horizon"),
            "per_combo": grouped("combo_key"),
            "skip_reasons": skip_reasons,
            "open_positions": [self._paper_row(row) for row in open_rows],
            "recent_markets": [self._paper_row(row) for row in recent_rows],
        }

    def forecast_analytics(self, min_n: int = 30) -> dict:
        analytics = super().forecast_analytics(min_n)
        rows = self.conn.execute(
            """
            SELECT combo_key, forecast_direction, forecast_p_up,
                   forecast_confidence, forecast_grade, forecast_status,
                   forecast_correct, forecast_brier
            FROM forecasts
            WHERE official_result IS NOT NULL
            """
        ).fetchall()

        per_asset_rows: dict[str, list[sqlite3.Row]] = {}
        per_horizon_rows: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            asset, _, horizon = str(row["combo_key"]).partition(":")
            per_asset_rows.setdefault(asset, []).append(row)
            per_horizon_rows.setdefault(horizon, []).append(row)

        analytics["per_asset"] = {
            key: {"research_forecast": self._research_metrics(group, min_n)}
            for key, group in sorted(per_asset_rows.items())
        }
        analytics["per_horizon"] = {
            key: {"research_forecast": self._research_metrics(group, min_n)}
            for key, group in sorted(per_horizon_rows.items())
        }
        return analytics

    def stats(self) -> dict:
        stats = super().stats()
        strategy = self.paper_policy.strategy_version
        stats.update(
            {
                "paper_attempts": self.conn.execute(
                    "SELECT COUNT(*) FROM paper_trades WHERE strategy_version=?",
                    (strategy,),
                ).fetchone()[0],
                "paper_open": self.conn.execute(
                    """
                    SELECT COUNT(*) FROM paper_trades
                    WHERE strategy_version=? AND status='OPEN'
                    """,
                    (strategy,),
                ).fetchone()[0],
                "paper_settled": self.conn.execute(
                    """
                    SELECT COUNT(*) FROM paper_trades
                    WHERE strategy_version=? AND status='SETTLED'
                    """,
                    (strategy,),
                ).fetchone()[0],
                "paper_skipped": self.conn.execute(
                    """
                    SELECT COUNT(*) FROM paper_trades
                    WHERE strategy_version=? AND status='SKIPPED'
                    """,
                    (strategy,),
                ).fetchone()[0],
            }
        )
        return stats
