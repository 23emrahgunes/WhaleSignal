"""Depth-aware, read-only execution simulation for P2.6 Paper V2."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from p26_fee import FeeSchedule, FeeScheduleUnavailable


@dataclass(frozen=True, order=True)
class BookLevel:
    price: float
    size: float  # outcome shares

    def __post_init__(self) -> None:
        if not 0.0 < float(self.price) < 1.0:
            raise ValueError("book price must be in (0,1)")
        if float(self.size) <= 0:
            raise ValueError("book size must be positive")


@dataclass(frozen=True)
class OrderBookSnapshot:
    token_id: str
    ts_ms: int
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    sequence: Optional[int] = None
    source: str = "POLYMARKET_CLOB"

    @property
    def best_bid(self) -> Optional[float]:
        return max((level.price for level in self.bids), default=None)

    @property
    def best_ask(self) -> Optional[float]:
        return min((level.price for level in self.asks), default=None)

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @classmethod
    def from_levels(
        cls,
        *,
        token_id: str,
        ts_ms: int,
        bids: Iterable[tuple[float, float] | BookLevel],
        asks: Iterable[tuple[float, float] | BookLevel],
        sequence: Optional[int] = None,
        source: str = "POLYMARKET_CLOB",
    ) -> "OrderBookSnapshot":
        def convert(values, reverse: bool):
            levels = [
                value if isinstance(value, BookLevel) else BookLevel(*value)
                for value in values
            ]
            return tuple(sorted(levels, key=lambda level: level.price, reverse=reverse))

        return cls(
            token_id=str(token_id),
            ts_ms=int(ts_ms),
            bids=convert(bids, True),
            asks=convert(asks, False),
            sequence=(int(sequence) if sequence is not None else None),
            source=str(source),
        )


@dataclass(frozen=True)
class ExecutionFill:
    requested_stake_usdc: float
    filled_stake_usdc: float
    fill_fraction: float
    shares: float
    orderbook_vwap: Optional[float]
    fee_usdc: float
    fee_per_share: Optional[float]
    all_in_cost_per_share: Optional[float]
    levels_consumed: int
    best_ask: Optional[float]
    worst_ask: Optional[float]
    price_impact: Optional[float]
    fee_enabled: bool = False
    fee_rate: Optional[float] = None
    fee_exponent: Optional[float] = None
    fee_source: Optional[str] = None
    fee_formula_version: Optional[str] = None

    @property
    def complete(self) -> bool:
        return self.fill_fraction >= 1.0 - 1e-12


def executable_depth_usdc(asks: Sequence[BookLevel]) -> float:
    return sum(float(level.price) * float(level.size) for level in asks)


def simulate_buy(
    snapshot: OrderBookSnapshot,
    *,
    stake_usdc: float,
    fee_bps: float = 0.0,
    fee_schedule: Optional[FeeSchedule] = None,
    require_fee_schedule: bool = False,
) -> ExecutionFill:
    """Consume asks in price order and return a realizable VWAP simulation.

    ``size`` is outcome shares and each level's USDC capacity is ``price*size``.
    Fees are modeled on filled notional and converted to cost per acquired share.
    """
    stake = float(stake_usdc)
    if stake <= 0:
        raise ValueError("stake must be positive")
    if fee_bps < 0:
        raise ValueError("fee_bps cannot be negative")
    if require_fee_schedule and fee_schedule is None:
        raise FeeScheduleUnavailable("FEE_SCHEDULE_UNAVAILABLE")
    if fee_schedule is not None and fee_schedule.token_id != snapshot.token_id:
        raise ValueError("fee schedule token does not match order book token")

    remaining = stake
    spent = 0.0
    shares = 0.0
    consumed = 0
    worst: Optional[float] = None
    fee = 0.0
    asks = sorted(snapshot.asks, key=lambda level: level.price)
    for level in asks:
        if remaining <= 1e-12:
            break
        capacity = float(level.price) * float(level.size)
        take_notional = min(remaining, capacity)
        if take_notional <= 0:
            continue
        level_shares = take_notional / float(level.price)
        shares += level_shares
        spent += take_notional
        if fee_schedule is not None:
            fee += fee_schedule.fee_usdc(
                shares=level_shares, price=float(level.price)
            )
        remaining -= take_notional
        consumed += 1
        worst = float(level.price)

    fill_fraction = spent / stake
    if fee_schedule is None:
        fee = spent * float(fee_bps) / 10_000.0
    if shares > 0:
        vwap = spent / shares
        fee_per_share = fee / shares
        all_in = (spent + fee) / shares
    else:
        vwap = None
        fee_per_share = None
        all_in = None
    best = snapshot.best_ask
    impact = (vwap - best) if vwap is not None and best is not None else None
    return ExecutionFill(
        requested_stake_usdc=stake,
        filled_stake_usdc=spent,
        fill_fraction=fill_fraction,
        shares=shares,
        orderbook_vwap=vwap,
        fee_usdc=fee,
        fee_per_share=fee_per_share,
        all_in_cost_per_share=all_in,
        levels_consumed=consumed,
        best_ask=best,
        worst_ask=worst,
        price_impact=impact,
        fee_enabled=(bool(fee_schedule.enabled) if fee_schedule is not None else fee_bps > 0),
        fee_rate=(fee_schedule.rate if fee_schedule is not None else None),
        fee_exponent=(fee_schedule.exponent if fee_schedule is not None else None),
        fee_source=(fee_schedule.source if fee_schedule is not None else "LEGACY_FIXED_BPS"),
        fee_formula_version=(
            fee_schedule.formula_version if fee_schedule is not None
            else "LEGACY_FIXED_BPS_V1"
        ),
    )
