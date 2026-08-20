"""Latency-, depth-, liquidity- and alpha-aware RESEARCH_PAPER_V2 policy."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional, Sequence

from p26_alpha_decay import AlphaDecayMetrics, evaluate_alpha_gate
from p26_calibration import ConservativeProbability
from p26_config import P26Settings
from p26_delay_replay import DelayReplayResult, replay_edge_curve
from p26_execution import ExecutionFill, OrderBookSnapshot, simulate_buy
from p26_latency import SourceClock, compute_latency_metrics, evaluate_latency_gate
from p26_liquidity_guard import LiquidityGateResult, evaluate_liquidity_gate


@dataclass(frozen=True)
class PaperV2Decision:
    eligible: bool
    reason: str
    side: str
    selected_probability_lower: Optional[float]
    net_edge: Optional[float]
    fill: Optional[ExecutionFill]
    liquidity: Optional[LiquidityGateResult]
    alpha: Optional[AlphaDecayMetrics]
    latency_reason: Optional[str]
    details: tuple[str, ...]
    strategy_version: str = "RESEARCH_PAPER_V2"

    def to_dict(self) -> dict:
        return {
            "eligible": self.eligible,
            "reason": self.reason,
            "side": self.side,
            "selected_probability_lower": self.selected_probability_lower,
            "net_edge": self.net_edge,
            "fill": asdict(self.fill) if self.fill is not None else None,
            "liquidity": (
                {**asdict(self.liquidity), "metrics": asdict(self.liquidity.metrics)}
                if self.liquidity is not None else None
            ),
            "alpha": asdict(self.alpha) if self.alpha is not None else None,
            "latency_reason": self.latency_reason,
            "details": list(self.details),
            "strategy_version": self.strategy_version,
            "execution": False,
        }


def evaluate_paper_v2_entry(
    settings: P26Settings,
    *,
    side: str,
    probability: ConservativeProbability,
    forecast_ts_ms: int,
    fill_ts_ms: int,
    source_clock: SourceClock,
    book_history: Sequence[OrderBookSnapshot],
) -> PaperV2Decision:
    side = side.strip().upper()
    if side not in {"UP", "DOWN"}:
        raise ValueError("side must be UP or DOWN")
    lower = probability.selected_lower(side)
    if lower is None:
        return PaperV2Decision(False, "UNCERTAINTY_BUCKET_NOT_READY", side, None, None, None, None, None, None, ())
    if not book_history:
        return PaperV2Decision(False, "BOOK_MISSING", side, lower, None, None, None, None, None, ())
    available_books = sorted(
        (book for book in book_history if book.ts_ms <= int(fill_ts_ms)),
        key=lambda book: book.ts_ms,
    )
    if not available_books:
        return PaperV2Decision(False, "BOOK_NOT_AVAILABLE_AT_FILL", side, lower, None, None, None, None, None, ())
    current = available_books[-1]

    latency_metrics = compute_latency_metrics(
        decision_ts_ms=int(forecast_ts_ms),
        sources=source_clock,
        forecast_created_ts_ms=int(forecast_ts_ms),
        fill_ts_ms=int(fill_ts_ms),
        fill_quote_source_ts_ms=current.ts_ms,
    )
    latency = evaluate_latency_gate(
        latency_metrics,
        required_source_count=4,
        max_source_skew_ms=settings.max_source_skew_ms,
        max_decision_data_lag_ms=settings.max_decision_data_lag_ms,
        max_forecast_age_ms=settings.max_forecast_age_ms,
        max_quote_age_at_fill_ms=settings.max_quote_age_at_fill_ms,
    )
    if not latency.allowed:
        return PaperV2Decision(False, latency.reason, side, lower, None, None, None, None, latency.reason, latency.details)

    liquidity = evaluate_liquidity_gate(
        available_books,
        now_ms=fill_ts_ms,
        stake_usdc=settings.paper_v2_stake_usdc,
        max_book_age_ms=settings.max_quote_age_at_fill_ms,
        max_spread=settings.paper_v2_max_spread,
        min_depth_persistence_ms=settings.paper_v2_min_depth_persistence_ms,
        min_fill_fraction=settings.paper_v2_min_fill_fraction,
        max_flicker_rate=settings.paper_v2_max_flicker_rate,
        max_cancel_to_add_ratio=settings.paper_v2_max_cancel_to_add_ratio,
    )
    if not liquidity.allowed:
        return PaperV2Decision(False, liquidity.reason, side, lower, None, None, liquidity, None, latency.reason, liquidity.details)

    fill = simulate_buy(
        current,
        stake_usdc=settings.paper_v2_stake_usdc,
        fee_bps=settings.paper_v2_fee_bps,
    )
    if fill.fill_fraction + 1e-12 < settings.paper_v2_min_fill_fraction:
        return PaperV2Decision(False, "PARTIAL_FILL_BELOW_MINIMUM", side, lower, None, fill, liquidity, None, latency.reason, ())
    if fill.all_in_cost_per_share is None:
        return PaperV2Decision(False, "NO_EXECUTABLE_FILL", side, lower, None, fill, liquidity, None, latency.reason, ())

    replay: DelayReplayResult = replay_edge_curve(
        forecast_ts_ms=forecast_ts_ms,
        books=book_history,
        conservative_probability=lower,
        stake_usdc=settings.paper_v2_stake_usdc,
        fee_bps=settings.paper_v2_fee_bps,
        safety_buffer=settings.paper_v2_safety_buffer,
    )
    age_ms = max(0, int(fill_ts_ms) - int(forecast_ts_ms))
    current_edge = lower - fill.all_in_cost_per_share - settings.paper_v2_safety_buffer
    learned_ttl_ms = (
        int(replay.decay.time_to_zero_edge_ms)
        if replay.decay.time_to_zero_edge_ms is not None
        else None
    )
    alpha_gate = evaluate_alpha_gate(
        forecast_age_ms=age_ms,
        current_net_edge=current_edge,
        minimum_net_edge=settings.paper_v2_min_net_edge,
        learned_ttl_ms=learned_ttl_ms,
        max_fallback_ttl_ms=settings.max_forecast_age_ms,
    )
    if not replay.observations:
        return PaperV2Decision(False, "ALPHA_PROFILE_MISSING", side, lower, current_edge, fill, liquidity, replay.decay, latency.reason, ())
    if not alpha_gate.allowed:
        return PaperV2Decision(False, alpha_gate.reason, side, lower, current_edge, fill, liquidity, replay.decay, latency.reason, alpha_gate.details)
    if current_edge + 1e-12 < settings.paper_v2_min_net_edge:
        return PaperV2Decision(False, "NET_EDGE_BELOW_MINIMUM", side, lower, current_edge, fill, liquidity, replay.decay, latency.reason, ())
    return PaperV2Decision(True, "OPEN", side, lower, current_edge, fill, liquidity, replay.decay, latency.reason, ())
