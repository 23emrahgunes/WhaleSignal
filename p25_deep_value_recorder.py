"""Deep-value paper recorder for low-price directional reversal research.

This layer is simulation-only.  In DEEP_VALUE_WATCH mode it observes every existing
forecast checkpoint for a market and opens at most one paper trade when the forecast
side first becomes eligible inside the configured price band.  Temporary misses do
not consume the market; only the final checkpoint persists a terminal SKIPPED row.

No credentials, signing, order construction or execution exists here.
"""
from __future__ import annotations

from dataclasses import replace
import logging
from typing import Optional

from models import FeatureSnapshot, MarketRef
from p25_paper import PaperEntryDecision, evaluate_paper_entry
from p25_reconciling_recorder import P25ReconcilingPaperRecorder
from p25_research_recorder import P25ResearchRecorder


log = logging.getLogger("direction_engine.paper.deep_value")


_PRICE_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("01-05c", 0.00, 0.05),
    ("05-10c", 0.05, 0.10),
    ("10-15c", 0.10, 0.15),
    ("15-25c", 0.15, 0.25),
    ("25-35c", 0.25, 0.35),
    ("35-45c", 0.35, 0.45),
)


class P25DeepValuePaperRecorder(P25ReconcilingPaperRecorder):
    """Recorder supporting both legacy canonical and deep-value watch modes."""

    def __init__(self, db_path: str, cfg) -> None:  # noqa: ANN001
        self.paper_cfg = cfg
        super().__init__(db_path, cfg)

    def _deep_value_enabled(self) -> bool:
        return self.paper_cfg.paper_entry_mode_normalized() == "DEEP_VALUE_WATCH"

    def _decision_for_checkpoint(
        self,
        *,
        ref: MarketRef,
        snap: FeatureSnapshot,
        checkpoint: int,
        trace: dict,
    ) -> Optional[PaperEntryDecision]:
        horizon = ref.combo.horizon.value
        if int(checkpoint) not in set(self.paper_cfg.paper_watch_checkpoints(horizon)):
            return None

        # Re-use the canonical policy evaluator by making the current observed
        # checkpoint canonical for this one pure evaluation call.
        entry_checkpoints = dict(self.paper_policy.entry_checkpoints)
        entry_checkpoints[horizon] = int(checkpoint)
        policy = replace(self.paper_policy, entry_checkpoints=entry_checkpoints)
        return evaluate_paper_entry(
            ref=ref,
            snap=snap,
            checkpoint=int(checkpoint),
            trace=trace,
            policy=policy,
            available_bankroll_usdc=self.available_paper_bankroll(),
        )

    def _persist_paper_decision(
        self,
        *,
        ref: MarketRef,
        snap: FeatureSnapshot,
        checkpoint: int,
        trace: dict,
        decision: PaperEntryDecision,
    ) -> bool:
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
                int(checkpoint),
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
        if created:
            if status == "OPEN":
                log.info(
                    "DEEP VALUE PAPER OPEN %s %s stake=%.2f ask=%.3f fill=%.3f edge=%+.3f checkpoint=T-%s",
                    ref.combo.key,
                    decision.side,
                    decision.stake_usdc,
                    decision.entry_ask or 0.0,
                    decision.fill_price or 0.0,
                    decision.forecast_edge or 0.0,
                    checkpoint,
                )
            else:
                log.info(
                    "DEEP VALUE PAPER FINAL SKIP %s reason=%s checkpoint=T-%s",
                    ref.combo.key,
                    skip_reason,
                    checkpoint,
                )
        return created

    @staticmethod
    def _update_trace_from_row(trace: dict, current: dict) -> None:
        trace.update(
            {
                "paper_trade_status": current.get("status"),
                "paper_trade_side": current.get("side"),
                "paper_trade_fill": current.get("fill_price"),
                "paper_trade_skip_reason": current.get("skip_reason"),
                "paper_trade_checkpoint": current.get("checkpoint_sec"),
            }
        )

    def record_forecast(
        self,
        ref: MarketRef,
        snap: FeatureSnapshot,
        checkpoint: int,
        trace: dict,
    ) -> bool:
        if not self._deep_value_enabled():
            return super().record_forecast(ref, snap, checkpoint, trace)

        # Record the research forecast exactly once via the non-paper parent layer.
        inserted = P25ResearchRecorder.record_forecast(
            self,
            ref,
            snap,
            checkpoint,
            trace,
        )
        if not inserted:
            return False

        if not ref.condition_id:
            return True

        current = self.paper_trade_for_condition(ref.condition_id)
        if current is not None:
            self._update_trace_from_row(trace, current)
            return True

        decision = self._decision_for_checkpoint(
            ref=ref,
            snap=snap,
            checkpoint=checkpoint,
            trace=trace,
        )
        if decision is None:
            return True

        watch = self.paper_cfg.paper_watch_checkpoints(ref.combo.horizon.value)
        final_checkpoint = min(watch)

        if decision.eligible:
            self._persist_paper_decision(
                ref=ref,
                snap=snap,
                checkpoint=checkpoint,
                trace=trace,
                decision=decision,
            )
            current = self.paper_trade_for_condition(ref.condition_id)
            if current:
                self._update_trace_from_row(trace, current)
            return True

        # A cheap-price watch must not be consumed by an early transient miss.  Keep
        # observing later checkpoints and only persist one terminal SKIPPED row at the
        # final checkpoint if no entry ever qualified.
        trace.update(
            {
                "paper_trade_status": "WATCHING_DEEP_VALUE",
                "paper_trade_side": decision.side,
                "paper_trade_fill": decision.fill_price,
                "paper_trade_skip_reason": decision.reason,
                "paper_trade_checkpoint": checkpoint,
            }
        )
        if int(checkpoint) == int(final_checkpoint):
            self._persist_paper_decision(
                ref=ref,
                snap=snap,
                checkpoint=checkpoint,
                trace=trace,
                decision=decision,
            )
            current = self.paper_trade_for_condition(ref.condition_id)
            if current:
                self._update_trace_from_row(trace, current)
        return True

    @staticmethod
    def _bucket_label(fill_price: float) -> str:
        value = float(fill_price)
        for index, (label, low, high) in enumerate(_PRICE_BUCKETS):
            if (value > low or index == 0) and value <= high + 1e-12:
                return label
        return "outside"

    def paper_analytics(self, recent_limit: Optional[int] = None) -> dict:
        result = super().paper_analytics(recent_limit)
        rows = self.conn.execute(
            """
            SELECT * FROM paper_trades
            WHERE strategy_version=?
              AND status IN ('OPEN','SETTLED')
              AND fill_price IS NOT NULL
            ORDER BY attempted_at ASC
            """,
            (self.paper_policy.strategy_version,),
        ).fetchall()
        grouped: dict[str, list] = {label: [] for label, _lo, _hi in _PRICE_BUCKETS}
        for row in rows:
            label = self._bucket_label(float(row["fill_price"]))
            if label in grouped:
                grouped[label].append(row)
        result["entry_mode"] = self.paper_cfg.paper_entry_mode_normalized()
        result["price_buckets"] = {
            label: self._paper_metrics(bucket_rows)
            for label, bucket_rows in grouped.items()
        }
        return result
