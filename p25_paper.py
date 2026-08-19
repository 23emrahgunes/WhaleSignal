"""Pure paper-trading policy for the P2.5 research forecast.

The policy simulates buying the forecast side at the *actual best ask* observed at a
single canonical checkpoint.  It applies configurable slippage and fees, records a
fixed stake and settles against the official binary outcome.  No networking,
credentials, signing or execution exists in this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from models import Decision, FeatureSnapshot, MarketRef


@dataclass(frozen=True)
class PaperPolicy:
    enabled: bool
    strategy_version: str
    starting_bankroll_usdc: float
    stake_usdc: float
    entry_checkpoints: dict[str, int]
    min_confidence: float
    min_agreement: float
    min_edge: float
    min_price: float
    max_price: float
    slippage: float
    fee_bps: float
    allowed_statuses: frozenset[str]
    allowed_grades: frozenset[str]

    @classmethod
    def from_settings(cls, cfg) -> "PaperPolicy":  # noqa: ANN001
        return cls(
            enabled=bool(cfg.paper_trading_enabled),
            strategy_version=str(cfg.paper_strategy_version),
            starting_bankroll_usdc=float(cfg.paper_starting_bankroll_usdc),
            stake_usdc=float(cfg.paper_stake_usdc),
            entry_checkpoints={
                horizon: int(cfg.paper_entry_checkpoint(horizon))
                for horizon in ("5m", "15m", "1h")
            },
            min_confidence=float(cfg.paper_min_confidence),
            min_agreement=float(cfg.paper_min_agreement),
            min_edge=float(cfg.paper_min_edge),
            min_price=float(cfg.paper_min_price),
            max_price=float(cfg.paper_max_price),
            slippage=float(cfg.paper_slippage),
            fee_bps=float(cfg.paper_fee_bps),
            allowed_statuses=frozenset(cfg.paper_allowed_statuses()),
            allowed_grades=frozenset(cfg.paper_allowed_grades()),
        )

    def entry_checkpoint(self, horizon: str) -> int:
        return int(self.entry_checkpoints.get(horizon, 0))

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "strategy_version": self.strategy_version,
            "starting_bankroll_usdc": self.starting_bankroll_usdc,
            "stake_usdc": self.stake_usdc,
            "entry_checkpoints": dict(self.entry_checkpoints),
            "min_confidence": self.min_confidence,
            "min_agreement": self.min_agreement,
            "min_edge": self.min_edge,
            "min_price": self.min_price,
            "max_price": self.max_price,
            "slippage": self.slippage,
            "fee_bps": self.fee_bps,
            "allowed_statuses": sorted(self.allowed_statuses),
            "allowed_grades": sorted(self.allowed_grades),
            "execution": False,
        }


@dataclass(frozen=True)
class PaperEntryDecision:
    eligible: bool
    reason: str
    side: Optional[str]
    selected_probability: Optional[float]
    entry_bid: Optional[float]
    entry_ask: Optional[float]
    fill_price: Optional[float]
    forecast_edge: Optional[float]
    stake_usdc: float
    shares: Optional[float]
    slippage: float
    fee_usdc: float


@dataclass(frozen=True)
class PaperSettlement:
    correct: bool
    gross_payout: float
    realized_pnl: float
    roi: float


def _skip(
    reason: str,
    *,
    side: Optional[str] = None,
    selected_probability: Optional[float] = None,
    entry_bid: Optional[float] = None,
    entry_ask: Optional[float] = None,
    fill_price: Optional[float] = None,
    forecast_edge: Optional[float] = None,
    stake_usdc: float = 0.0,
    slippage: float = 0.0,
    fee_usdc: float = 0.0,
) -> PaperEntryDecision:
    return PaperEntryDecision(
        eligible=False,
        reason=reason,
        side=side,
        selected_probability=selected_probability,
        entry_bid=entry_bid,
        entry_ask=entry_ask,
        fill_price=fill_price,
        forecast_edge=forecast_edge,
        stake_usdc=stake_usdc,
        shares=None,
        slippage=slippage,
        fee_usdc=fee_usdc,
    )


def evaluate_paper_entry(
    *,
    ref: MarketRef,
    snap: FeatureSnapshot,
    checkpoint: int,
    trace: dict,
    policy: PaperPolicy,
    available_bankroll_usdc: float,
) -> Optional[PaperEntryDecision]:
    """Evaluate the one canonical paper entry for a market.

    ``None`` means this forecast checkpoint is not the configured entry checkpoint.
    A returned ineligible decision is persisted as ``SKIPPED`` so paper coverage is
    transparent and cannot be cherry-picked after settlement.
    """
    expected_checkpoint = policy.entry_checkpoint(ref.combo.horizon.value)
    if int(checkpoint) != expected_checkpoint:
        return None

    if not policy.enabled:
        return _skip("PAPER_DISABLED")

    direction = str(trace.get("forecast_direction") or "").upper()
    if direction not in {Decision.UP.value, Decision.DOWN.value}:
        return _skip("NO_DIRECTIONAL_FORECAST")

    feature_ready = bool(trace.get("feature_ready"))
    if not feature_ready:
        return _skip("FEATURE_NOT_READY", side=direction)

    status = str(trace.get("forecast_status") or "UNKNOWN").upper()
    if status not in policy.allowed_statuses:
        return _skip(f"STATUS_{status}_NOT_ALLOWED", side=direction)

    grade = str(trace.get("forecast_grade") or "UNKNOWN").upper()
    if grade not in policy.allowed_grades:
        return _skip(f"GRADE_{grade}_NOT_ALLOWED", side=direction)

    confidence = float(trace.get("forecast_confidence") or 0.0)
    if confidence < policy.min_confidence:
        return _skip(
            "LOW_CONFIDENCE",
            side=direction,
            stake_usdc=policy.stake_usdc,
        )

    agreement = float(trace.get("forecast_agreement") or 0.0)
    if agreement < policy.min_agreement:
        return _skip(
            "LOW_AGREEMENT",
            side=direction,
            stake_usdc=policy.stake_usdc,
        )

    p_up_raw = trace.get("forecast_p_up")
    if p_up_raw is None:
        return _skip("FORECAST_PROBABILITY_MISSING", side=direction)
    p_up = max(0.0, min(1.0, float(p_up_raw)))
    selected_probability = p_up if direction == Decision.UP.value else 1.0 - p_up

    if direction == Decision.UP.value:
        entry_bid, entry_ask = snap.up_bid, snap.up_ask
    else:
        entry_bid, entry_ask = snap.down_bid, snap.down_ask

    if entry_ask is None:
        return _skip(
            "ASK_MISSING",
            side=direction,
            selected_probability=selected_probability,
            entry_bid=entry_bid,
        )

    ask = float(entry_ask)
    fill = min(0.999, ask + policy.slippage)
    if not policy.min_price <= fill <= policy.max_price:
        return _skip(
            "PRICE_OUT_OF_RANGE",
            side=direction,
            selected_probability=selected_probability,
            entry_bid=entry_bid,
            entry_ask=ask,
            fill_price=fill,
            stake_usdc=policy.stake_usdc,
            slippage=policy.slippage,
        )

    edge = selected_probability - fill
    if edge + 1e-12 < policy.min_edge:
        return _skip(
            "EDGE_BELOW_MINIMUM",
            side=direction,
            selected_probability=selected_probability,
            entry_bid=entry_bid,
            entry_ask=ask,
            fill_price=fill,
            forecast_edge=edge,
            stake_usdc=policy.stake_usdc,
            slippage=policy.slippage,
        )

    stake = float(policy.stake_usdc)
    fee = stake * float(policy.fee_bps) / 10000.0
    if available_bankroll_usdc + 1e-12 < stake + fee:
        return _skip(
            "INSUFFICIENT_PAPER_BANKROLL",
            side=direction,
            selected_probability=selected_probability,
            entry_bid=entry_bid,
            entry_ask=ask,
            fill_price=fill,
            forecast_edge=edge,
            stake_usdc=stake,
            slippage=policy.slippage,
            fee_usdc=fee,
        )

    shares = stake / fill
    return PaperEntryDecision(
        eligible=True,
        reason="OPEN",
        side=direction,
        selected_probability=selected_probability,
        entry_bid=float(entry_bid) if entry_bid is not None else None,
        entry_ask=ask,
        fill_price=fill,
        forecast_edge=edge,
        stake_usdc=stake,
        shares=shares,
        slippage=policy.slippage,
        fee_usdc=fee,
    )


def settle_paper_trade(
    *,
    side: str,
    official_result: str,
    shares: float,
    stake_usdc: float,
    fee_usdc: float,
) -> PaperSettlement:
    if side not in {Decision.UP.value, Decision.DOWN.value}:
        raise ValueError(f"invalid paper side: {side}")
    if official_result not in {Decision.UP.value, Decision.DOWN.value}:
        raise ValueError(f"invalid official result: {official_result}")
    if shares < 0 or stake_usdc <= 0 or fee_usdc < 0:
        raise ValueError("invalid paper position amounts")

    correct = side == official_result
    gross_payout = float(shares) if correct else 0.0
    realized_pnl = gross_payout - float(stake_usdc) - float(fee_usdc)
    roi = realized_pnl / float(stake_usdc)
    return PaperSettlement(
        correct=correct,
        gross_payout=gross_payout,
        realized_pnl=realized_pnl,
        roi=roi,
    )
