"""Pure sizing/risk math for guarded P3 LIVE execution.

LIVE v2 sizes in equal shares, never by proportional dollar scaling. Capital is a
result of the selected equal-share quantity, not the primary sizing variable.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Iterable, Sequence

FEE_QUANTUM = Decimal("0.00001")
SHARE_QUANTUM = Decimal("0.000001")


@dataclass(frozen=True)
class DepthFill:
    price: float
    shares: float


@dataclass(frozen=True)
class DepthQuote:
    requested_shares: float
    filled_shares: float
    complete: bool
    notional_usdc: float
    vwap: float | None
    worst_price: float | None
    capacity_shares: float
    min_order_size: float
    fills: tuple[DepthFill, ...]


def floor_shares(value: float) -> float:
    """Round down to 6-decimal share precision so we never over-request depth."""
    raw = Decimal(str(max(0.0, float(value))))
    return float(raw.quantize(SHARE_QUANTUM, rounding=ROUND_DOWN))


def select_equal_share_quantity(
    *,
    strict_optimal_shares: float,
    target_shares: float,
    hard_max_shares: float,
    up_capacity_shares: float,
    down_capacity_shares: float,
    min_order_size: float,
) -> float:
    """Choose one quantity that is used identically for UP and DOWN."""
    q = floor_shares(
        min(
            float(strict_optimal_shares),
            float(target_shares),
            float(hard_max_shares),
            float(up_capacity_shares),
            float(down_capacity_shares),
        )
    )
    if q <= 0 or q + 1e-9 < float(min_order_size):
        return 0.0
    return q


def consume_depth(
    levels: Sequence[tuple[float, float]] | Iterable[tuple[float, float]],
    *,
    shares: float,
    buy: bool,
    price_limit: float | None = None,
    min_order_size: float = 0.0,
) -> DepthQuote:
    """Consume visible depth for an exact share quantity.

    For BUY, lower asks are consumed first and ``price_limit`` is the maximum price.
    For SELL, higher bids are consumed first and ``price_limit`` is the minimum price.
    """
    target = floor_shares(shares)
    normalized = [(float(p), max(0.0, float(s))) for p, s in levels]
    normalized.sort(key=lambda x: x[0], reverse=not buy)

    capacity = 0.0
    for price, size in normalized:
        if size <= 0:
            continue
        if price_limit is not None:
            if buy and price > float(price_limit) + 1e-12:
                continue
            if not buy and price < float(price_limit) - 1e-12:
                continue
        capacity += size

    remaining = target
    filled = 0.0
    notional = 0.0
    worst: float | None = None
    fills: list[DepthFill] = []
    for price, size in normalized:
        if remaining <= 1e-9:
            break
        if size <= 0:
            continue
        if price_limit is not None:
            if buy and price > float(price_limit) + 1e-12:
                continue
            if not buy and price < float(price_limit) - 1e-12:
                continue
        take = min(remaining, size)
        if take <= 0:
            continue
        fills.append(DepthFill(price=price, shares=take))
        filled += take
        notional += take * price
        remaining -= take
        worst = price

    complete = target > 0 and remaining <= 1e-6
    return DepthQuote(
        requested_shares=target,
        filled_shares=filled,
        complete=complete,
        notional_usdc=notional,
        vwap=(notional / filled if filled > 0 else None),
        worst_price=worst,
        capacity_shares=capacity,
        min_order_size=max(0.0, float(min_order_size)),
        fills=tuple(fills),
    )


def fee_for_fills(
    fills: Sequence[DepthFill],
    *,
    enabled: bool,
    rate: float,
    exponent: float,
) -> float:
    if not enabled or rate <= 0:
        return 0.0
    total = Decimal("0")
    for fill in fills:
        p = float(fill.price)
        if not 0.0 < p < 1.0:
            raise ValueError("fee price must be in (0,1)")
        curve = (p * (1.0 - p)) ** float(exponent)
        raw = Decimal(str(float(fill.shares) * float(rate) * curve))
        total += raw.quantize(FEE_QUANTUM, rounding=ROUND_HALF_UP)
    return float(total)


def buy_merge_metrics(
    *,
    quantity_shares: float,
    up_buy: DepthQuote,
    down_buy: DepthQuote,
    up_fee_usdc: float,
    down_fee_usdc: float,
    execution_buffer_per_share: float = 0.0,
) -> dict[str, float]:
    q = float(quantity_shares)
    if q <= 0 or not up_buy.complete or not down_buy.complete:
        raise ValueError("complete equal-share buy quotes are required")
    if abs(up_buy.requested_shares - down_buy.requested_shares) > 1e-6:
        raise ValueError("UP and DOWN share quantities must be equal")
    spend = float(up_buy.notional_usdc) + float(down_buy.notional_usdc)
    fees = float(up_fee_usdc) + float(down_fee_usdc)
    buffer = q * max(0.0, float(execution_buffer_per_share))
    net = q - spend - fees - buffer
    capital = spend + fees
    return {
        "quantity_shares": q,
        "capital_usdc": capital,
        "net_profit_usdc": net,
        "net_roi": net / capital if capital > 0 else 0.0,
        "gross_edge_per_share": 1.0 - (spend / q if q > 0 else 1.0),
        "fees_usdc": fees,
        "execution_buffer_usdc": buffer,
    }


def projected_unwind_loss(
    *,
    buy_quote: DepthQuote,
    buy_fee_usdc: float,
    sell_quote: DepthQuote,
    sell_fee_usdc: float,
) -> float:
    """Projected loss if this leg alone fills and is immediately sold."""
    if not buy_quote.complete or not sell_quote.complete:
        return math.inf
    entry_cost = float(buy_quote.notional_usdc) + float(buy_fee_usdc)
    exit_value = float(sell_quote.notional_usdc) - float(sell_fee_usdc)
    return max(0.0, entry_cost - exit_value)


def edge_to_unwind_loss_ratio(net_profit_usdc: float, worst_unwind_loss_usdc: float) -> float:
    loss = max(0.0, float(worst_unwind_loss_usdc))
    if loss <= 1e-12:
        return math.inf
    return max(0.0, float(net_profit_usdc)) / loss
