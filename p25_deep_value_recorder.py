"""Deep-value paper recorder for low-price directional reversal research.

DEEP_VALUE_WATCH is simulation-only.  The normal P2.5 engine already rebuilds the
current market snapshot + research forecast every SNAPSHOT_LOOP_MS (500ms default).
This recorder observes those in-memory ticks and writes NOTHING for ordinary misses.
It persists at most one $-stake paper entry per market, then normal authoritative
settlement records the result.  This lets us study very short 3c/5c/15c dislocations
without turning SQLite into a tick database.

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
        rows = self.conn.execute(
            "SELECT condition_id FROM paper_trades WHERE strategy_version=?",
            (self.paper_policy.strategy_version,),
        ).fetchall()
        self._deep_value_consumed = {str(row[0]) for row in rows if row[0]}

    def _deep_value_enabled(self) -> bool:
        return self.paper_cfg.paper_entry_mode_normalized() == "DEEP_VALUE_WATCH"

    def _decision_for_tick(
        self,
        *,
        ref: MarketRef,
        snap: FeatureSnapshot,
        trace: dict,
    ) -> Optional[PaperEntryDecision]:
        tte = snap.tte_sec if snap.tte_sec is not None else snap.seconds_remaining
        if tte is None or float(tte) <= 0:
            return None
        marker = max(0, int(round(float(tte))))
        horizon = ref.combo.horizon.value

        # Re-use the canonical pure evaluator by making this observed TTE marker the
        # canonical checkpoint for this one call.  Nothing is persisted unless the
        # decision is actually eligible.
        entry_checkpoints = dict(self.paper_policy.entry_checkpoints)
        entry_checkpoints[horizon] = marker
        policy = replace(self.paper_policy, entry_checkpoints=entry_checkpoints)
        return evaluate_paper_entry(
            ref=ref,
            snap=snap,
            checkpoint=marker,
            trace=trace,
            policy=policy,
            available_bankroll_usdc=self.available_paper_bankroll(),
        )

    def _persist_open(
        self,
        *,
        ref: MarketRef,
        snap: FeatureSnapshot,
        trace: dict,
        decision: PaperEntryDecision,
    ) -> bool:
        tte = snap.tte_sec if snap.tte_sec is not None else snap.seconds_remaining
        marker = max(0, int(round(float(tte or 0.0))))
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
                marker,
                snap.ts,
                float(tte or 0.0),
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
                "OPEN",
                None,
            ),
        )
        self.conn.commit()
        created = self.conn.total_changes > before
        self._deep_value_consumed.add(str(ref.condition_id))
        if created:
            multiple = (
                float(decision.selected_probability) / float(decision.fill_price)
                if decision.selected_probability is not None
                and decision.fill_price not in (None, 0)
                else None
            )
            log.info(
                "DEEP VALUE PAPER OPEN %s %s stake=%.2f ask=%.4f fill=%.4f "
                "model_p=%.4f p/price=%s shares=%.3f tte=%.1fs",
                ref.combo.key,
                decision.side,
                decision.stake_usdc,
                decision.entry_ask or 0.0,
                decision.fill_price or 0.0,
                decision.selected_probability or 0.0,
                f"{multiple:.2f}x" if multiple is not None else "NA",
                decision.shares or 0.0,
                float(tte or 0.0),
            )
        return created

    def observe_paper_tick(
        self,
        *,
        ref: MarketRef,
        snap: FeatureSnapshot,
        trace: dict,
        data_ready: bool,
    ) -> bool:
        """Watch the live in-memory P2.5 state; persist only the first eligible dip."""
        if not self._deep_value_enabled() or not self.paper_policy.enabled:
            return False
        if not data_ready or not ref.condition_id:
            return False
        if str(ref.condition_id) in self._deep_value_consumed:
            return False

        decision = self._decision_for_tick(ref=ref, snap=snap, trace=trace)
        if decision is None or not decision.eligible:
            return False
        return self._persist_open(
            ref=ref,
            snap=snap,
            trace=trace,
            decision=decision,
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

        # Deep-value entries are handled by observe_paper_tick every 500ms.  Keep
        # normal sparse checkpoint forecasts for model evaluation/calibration only.
        return P25ResearchRecorder.record_forecast(
            self,
            ref,
            snap,
            checkpoint,
            trace,
        )

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

        bucket_metrics = {}
        for label, bucket_rows in grouped.items():
            metrics = self._paper_metrics(bucket_rows)
            prices = [float(row["fill_price"]) for row in bucket_rows if row["fill_price"] is not None]
            probabilities = [
                float(row["selected_probability"])
                for row in bucket_rows
                if row["selected_probability"] is not None
            ]
            metrics.update(
                {
                    "avg_break_even_hit_rate": (
                        round(sum(prices) / len(prices), 4) if prices else None
                    ),
                    "avg_model_probability": (
                        round(sum(probabilities) / len(probabilities), 4)
                        if probabilities
                        else None
                    ),
                }
            )
            bucket_metrics[label] = metrics

        result["entry_mode"] = self.paper_cfg.paper_entry_mode_normalized()
        result["price_buckets"] = bucket_metrics
        result["db_policy"] = "SPARSE_ONLY_ACTUAL_DEEP_VALUE_ENTRIES"
        return result
