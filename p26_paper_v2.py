"""Fail-closed RESEARCH_PAPER_V2 entry and side-selection policy.

Pre-trade decisions use only data available at ``fill_ts_ms`` plus frozen
historical OOS artifacts.  Current-market future books are reserved for ex-post
analytics and never influence OPEN/SKIP.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional, Sequence

from p26_alpha_decay import AlphaDecayMetrics, evaluate_alpha_gate
from p26_alpha_profile import AlphaTTLDecision
from p26_calibration import ConservativeProbability
from p26_config import P26Settings
from p26_delay_replay import DelayReplayResult, replay_edge_curve
from p26_execution import ExecutionFill, OrderBookSnapshot, simulate_buy
from p26_fee import FeeSchedule, FeeScheduleUnavailable
from p26_latency import SourceClock, compute_latency_metrics, evaluate_latency_gate
from p26_liquidity_guard import LiquidityGateResult, evaluate_liquidity_gate
from p26_portfolio_risk import (
    PortfolioRiskPolicy,
    PortfolioRiskResult,
    PortfolioRiskState,
    evaluate_portfolio_risk,
)
from p26_selection import SideSelection, select_directional_side


@dataclass(frozen=True)
class PaperV2Decision:
    eligible: bool
    reason: str
    side: str
    token_id: Optional[str]
    selected_probability_lower: Optional[float]
    net_edge: Optional[float]
    fill: Optional[ExecutionFill]
    liquidity: Optional[LiquidityGateResult]
    alpha: Optional[AlphaDecayMetrics]
    alpha_ttl: Optional[AlphaTTLDecision]
    portfolio: Optional[PortfolioRiskResult]
    calibration_scope: str
    latency_reason: Optional[str]
    details: tuple[str, ...]
    strategy_version: str = "RESEARCH_PAPER_V2"

    def to_dict(self) -> dict:
        return {
            "eligible": self.eligible,
            "reason": self.reason,
            "side": self.side,
            "token_id": self.token_id,
            "selected_probability_lower": self.selected_probability_lower,
            "net_edge": self.net_edge,
            "fill": asdict(self.fill) if self.fill is not None else None,
            "liquidity": (
                {**asdict(self.liquidity), "metrics": asdict(self.liquidity.metrics)}
                if self.liquidity is not None else None
            ),
            "alpha": asdict(self.alpha) if self.alpha is not None else None,
            "alpha_ttl": asdict(self.alpha_ttl) if self.alpha_ttl is not None else None,
            "portfolio": (
                {**asdict(self.portfolio), "state": asdict(self.portfolio.state)}
                if self.portfolio is not None else None
            ),
            "calibration_scope": self.calibration_scope,
            "latency_reason": self.latency_reason,
            "details": list(self.details),
            "strategy_version": self.strategy_version,
            "execution": False,
        }


def _reject(
    reason: str,
    *,
    side: str,
    token_id: Optional[str],
    probability: ConservativeProbability,
    lower: Optional[float] = None,
    net_edge: Optional[float] = None,
    fill: Optional[ExecutionFill] = None,
    liquidity: Optional[LiquidityGateResult] = None,
    alpha_ttl: Optional[AlphaTTLDecision] = None,
    portfolio: Optional[PortfolioRiskResult] = None,
    latency_reason: Optional[str] = None,
    details: tuple[str, ...] = (),
) -> PaperV2Decision:
    return PaperV2Decision(
        False, reason, side, token_id, lower, net_edge, fill, liquidity, None,
        alpha_ttl, portfolio, probability.scope, latency_reason, details,
    )


def evaluate_paper_v2_entry(
    settings: P26Settings,
    *,
    side: str,
    probability: ConservativeProbability,
    forecast_ts_ms: int,
    fill_ts_ms: int,
    source_clock: SourceClock,
    book_history: Sequence[OrderBookSnapshot],
    alpha_ttl: Optional[AlphaTTLDecision] = None,
    fee_schedule: Optional[FeeSchedule] = None,
    portfolio_state: Optional[PortfolioRiskState] = None,
    portfolio_policy: Optional[PortfolioRiskPolicy] = None,
) -> PaperV2Decision:
    side = side.strip().upper()
    if side not in {"UP", "DOWN"}:
        raise ValueError("side must be UP or DOWN")
    approved_calibration = set(settings.approved_calibration_scopes())
    if probability.scope not in approved_calibration:
        return _reject(
            "CALIBRATION_SCOPE_NOT_APPROVED",
            side=side, token_id=None, probability=probability,
            details=(f"scope={probability.scope}",),
        )
    lower = probability.selected_lower(side)
    if lower is None:
        return _reject(
            "UNCERTAINTY_BUCKET_NOT_READY",
            side=side, token_id=None, probability=probability,
        )
    if alpha_ttl is None or not alpha_ttl.ready or alpha_ttl.ttl_ms is None:
        reason = alpha_ttl.reason if alpha_ttl is not None else "ALPHA_PROFILE_MISSING"
        return _reject(
            reason, side=side, token_id=None, probability=probability,
            lower=lower, alpha_ttl=alpha_ttl,
            details=(alpha_ttl.details if alpha_ttl is not None else ()),
        )
    if not book_history:
        return _reject("BOOK_MISSING", side=side, token_id=None, probability=probability, lower=lower, alpha_ttl=alpha_ttl)

    # Entry can use only books observed no later than the simulated fill time.
    available_books = sorted(
        (book for book in book_history if book.ts_ms <= int(fill_ts_ms)),
        key=lambda book: book.ts_ms,
    )
    if not available_books:
        return _reject("BOOK_NOT_AVAILABLE_AT_FILL", side=side, token_id=None, probability=probability, lower=lower, alpha_ttl=alpha_ttl)
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
        return _reject(
            latency.reason, side=side, token_id=current.token_id,
            probability=probability, lower=lower, alpha_ttl=alpha_ttl,
            latency_reason=latency.reason, details=latency.details,
        )

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
        return _reject(
            liquidity.reason, side=side, token_id=current.token_id,
            probability=probability, lower=lower, liquidity=liquidity,
            alpha_ttl=alpha_ttl, latency_reason=latency.reason,
            details=liquidity.details,
        )

    try:
        fill = simulate_buy(
            current,
            stake_usdc=settings.paper_v2_stake_usdc,
            fee_schedule=fee_schedule,
            require_fee_schedule=True,
        )
    except FeeScheduleUnavailable:
        return _reject(
            "FEE_SCHEDULE_UNAVAILABLE", side=side, token_id=current.token_id,
            probability=probability, lower=lower, liquidity=liquidity,
            alpha_ttl=alpha_ttl, latency_reason=latency.reason,
        )
    except ValueError as exc:
        return _reject(
            "FEE_OR_TOKEN_INTEGRITY_FAILURE", side=side, token_id=current.token_id,
            probability=probability, lower=lower, liquidity=liquidity,
            alpha_ttl=alpha_ttl, latency_reason=latency.reason,
            details=(str(exc),),
        )
    if fill.fill_fraction + 1e-12 < settings.paper_v2_min_fill_fraction:
        return _reject(
            "PARTIAL_FILL_BELOW_MINIMUM", side=side, token_id=current.token_id,
            probability=probability, lower=lower, fill=fill, liquidity=liquidity,
            alpha_ttl=alpha_ttl, latency_reason=latency.reason,
        )
    if fill.all_in_cost_per_share is None:
        return _reject(
            "NO_EXECUTABLE_FILL", side=side, token_id=current.token_id,
            probability=probability, lower=lower, fill=fill, liquidity=liquidity,
            alpha_ttl=alpha_ttl, latency_reason=latency.reason,
        )

    current_edge = lower - fill.all_in_cost_per_share - settings.paper_v2_safety_buffer
    age_ms = max(0, int(fill_ts_ms) - int(forecast_ts_ms))
    alpha_gate = evaluate_alpha_gate(
        forecast_age_ms=age_ms,
        current_net_edge=current_edge,
        minimum_net_edge=settings.paper_v2_min_net_edge,
        learned_ttl_ms=alpha_ttl.ttl_ms,
        max_fallback_ttl_ms=settings.max_forecast_age_ms,
    )
    if not alpha_gate.allowed:
        return _reject(
            alpha_gate.reason, side=side, token_id=current.token_id,
            probability=probability, lower=lower, net_edge=current_edge,
            fill=fill, liquidity=liquidity, alpha_ttl=alpha_ttl,
            latency_reason=latency.reason, details=alpha_gate.details,
        )
    if current_edge + 1e-12 < settings.paper_v2_min_net_edge:
        return _reject(
            "NET_EDGE_BELOW_MINIMUM", side=side, token_id=current.token_id,
            probability=probability, lower=lower, net_edge=current_edge,
            fill=fill, liquidity=liquidity, alpha_ttl=alpha_ttl,
            latency_reason=latency.reason,
        )

    portfolio: Optional[PortfolioRiskResult] = None
    if portfolio_state is None or portfolio_policy is None:
        return _reject(
            "PORTFOLIO_STATE_MISSING", side=side, token_id=current.token_id,
            probability=probability, lower=lower, net_edge=current_edge,
            fill=fill, liquidity=liquidity, alpha_ttl=alpha_ttl,
            latency_reason=latency.reason,
        )
    portfolio = evaluate_portfolio_risk(
        portfolio_state,
        policy=portfolio_policy,
        candidate_stake_usdc=settings.paper_v2_stake_usdc,
        projected_fee_usdc=fill.fee_usdc,
        now_ms=fill_ts_ms,
    )
    if not portfolio.allowed:
        return _reject(
            portfolio.reason, side=side, token_id=current.token_id,
            probability=probability, lower=lower, net_edge=current_edge,
            fill=fill, liquidity=liquidity, alpha_ttl=alpha_ttl,
            portfolio=portfolio, latency_reason=latency.reason,
            details=portfolio.details,
        )
    return PaperV2Decision(
        True, "OPEN", side, current.token_id, lower, current_edge, fill,
        liquidity, None, alpha_ttl, portfolio, probability.scope,
        latency.reason, (),
    )


def evaluate_paper_v2_sides(
    settings: P26Settings,
    *,
    probability: ConservativeProbability,
    forecast_ts_ms: int,
    fill_ts_ms: int,
    up_source_clock: SourceClock,
    down_source_clock: SourceClock,
    up_books: Sequence[OrderBookSnapshot],
    down_books: Sequence[OrderBookSnapshot],
    alpha_ttl: Optional[AlphaTTLDecision],
    up_fee_schedule: Optional[FeeSchedule],
    down_fee_schedule: Optional[FeeSchedule],
    portfolio_state: PortfolioRiskState,
    portfolio_policy: PortfolioRiskPolicy,
) -> tuple[SideSelection, PaperV2Decision, PaperV2Decision]:
    up = evaluate_paper_v2_entry(
        settings, side="UP", probability=probability,
        forecast_ts_ms=forecast_ts_ms, fill_ts_ms=fill_ts_ms,
        source_clock=up_source_clock, book_history=up_books,
        alpha_ttl=alpha_ttl, fee_schedule=up_fee_schedule,
        portfolio_state=portfolio_state, portfolio_policy=portfolio_policy,
    )
    down = evaluate_paper_v2_entry(
        settings, side="DOWN", probability=probability,
        forecast_ts_ms=forecast_ts_ms, fill_ts_ms=fill_ts_ms,
        source_clock=down_source_clock, book_history=down_books,
        alpha_ttl=alpha_ttl, fee_schedule=down_fee_schedule,
        portfolio_state=portfolio_state, portfolio_policy=portfolio_policy,
    )
    up_ts = max((item.ts_ms for item in up_books if item.ts_ms <= fill_ts_ms), default=0)
    down_ts = max((item.ts_ms for item in down_books if item.ts_ms <= fill_ts_ms), default=0)
    selection = select_directional_side(
        up=up, down=down,
        up_token_id=(up_books[-1].token_id if up_books else ""),
        down_token_id=(down_books[-1].token_id if down_books else ""),
        up_book_ts_ms=up_ts, down_book_ts_ms=down_ts,
        p_lower_up=probability.p_lower_up,
        p_lower_down=probability.p_lower_down,
        max_book_skew_ms=settings.max_source_skew_ms,
    )
    return selection, up, down


def evaluate_ex_post_alpha(
    *,
    forecast_ts_ms: int,
    books: Sequence[OrderBookSnapshot],
    conservative_probability: float,
    stake_usdc: float,
    fee_schedule: Optional[FeeSchedule],
    safety_buffer: float,
) -> DelayReplayResult:
    """Post-fill analytics only; callers must never feed this into current entry."""
    return replay_edge_curve(
        forecast_ts_ms=forecast_ts_ms,
        books=books,
        conservative_probability=conservative_probability,
        stake_usdc=stake_usdc,
        fee_bps=0.0,
        fee_schedule=fee_schedule,
        safety_buffer=safety_buffer,
    )
