"""Model-free complete-set structural arbitrage math.

This module never predicts direction.  It evaluates equal-share UP/DOWN
complete-set parity after full-depth VWAP, dynamic fee schedules and an explicit
execution-risk buffer.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from p26_execution import BookLevel, OrderBookSnapshot
from p26_fee import FeeSchedule
from p3_models import (
    ARB_BUY_MERGE,
    ARB_SPLIT_SELL,
    LegSimulation,
    StructuralOpportunity,
)


@dataclass(frozen=True)
class BookPair:
    condition_id: str
    combo_key: str
    up_book_id: int
    down_book_id: int
    up: OrderBookSnapshot
    down: OrderBookSnapshot



def _consume_shares(
    levels: Sequence[BookLevel],
    *,
    quantity_shares: float,
    fee_schedule: FeeSchedule,
    buy: bool,
) -> LegSimulation:
    target = float(quantity_shares)
    if target <= 0:
        raise ValueError("quantity_shares must be positive")
    remaining = target
    shares = 0.0
    notional = 0.0
    fee = 0.0
    worst: Optional[float] = None
    ordered = sorted(levels, key=lambda level: level.price, reverse=not buy)
    for level in ordered:
        if remaining <= 1e-12:
            break
        take = min(remaining, float(level.size))
        if take <= 0:
            continue
        price = float(level.price)
        shares += take
        notional += take * price
        fee += fee_schedule.fee_usdc(shares=take, price=price)
        worst = price
        remaining -= take
    complete = remaining <= 1e-9
    vwap = notional / shares if shares > 0 else None
    return LegSimulation(
        filled_shares=shares,
        notional_usdc=notional,
        fee_usdc=fee,
        vwap=vwap,
        worst_price=worst,
        complete=complete,
    )


def simulate_buy_shares(
    snapshot: OrderBookSnapshot,
    quantity_shares: float,
    fee_schedule: FeeSchedule,
) -> LegSimulation:
    if fee_schedule.token_id != snapshot.token_id:
        raise ValueError("fee schedule/token mismatch")
    return _consume_shares(
        snapshot.asks,
        quantity_shares=quantity_shares,
        fee_schedule=fee_schedule,
        buy=True,
    )


def simulate_sell_shares(
    snapshot: OrderBookSnapshot,
    quantity_shares: float,
    fee_schedule: FeeSchedule,
) -> LegSimulation:
    if fee_schedule.token_id != snapshot.token_id:
        raise ValueError("fee schedule/token mismatch")
    return _consume_shares(
        snapshot.bids,
        quantity_shares=quantity_shares,
        fee_schedule=fee_schedule,
        buy=False,
    )


def _breakpoints(
    up_levels: Iterable[BookLevel],
    down_levels: Iterable[BookLevel],
    max_quantity: float,
) -> tuple[float, ...]:
    cap = float(max_quantity)
    if cap <= 0:
        return ()
    points: set[float] = set()
    totals: list[float] = []
    for levels in (up_levels, down_levels):
        cumulative = 0.0
        for level in levels:
            cumulative += float(level.size)
            if cumulative > 0:
                points.add(min(cumulative, cap))
        totals.append(cumulative)
    common = min(cap, *totals) if totals else 0.0
    if common > 0:
        points.add(common)
    return tuple(sorted(value for value in points if 0 < value <= common + 1e-9))


def evaluate_buy_merge_quantity(
    pair: BookPair,
    *,
    quantity_shares: float,
    up_fee: FeeSchedule,
    down_fee: FeeSchedule,
    detected_ts_ms: int,
    execution_buffer_per_share: float,
) -> Optional[StructuralOpportunity]:
    up = simulate_buy_shares(pair.up, quantity_shares, up_fee)
    down = simulate_buy_shares(pair.down, quantity_shares, down_fee)
    if not up.complete or not down.complete or up.vwap is None or down.vwap is None:
        return None
    q = float(quantity_shares)
    spend = up.notional_usdc + down.notional_usdc
    fees = up.fee_usdc + down.fee_usdc
    gross_edge = 1.0 - up.vwap - down.vwap
    gross_profit = q - spend - fees
    buffer = q * float(execution_buffer_per_share)
    net = gross_profit - buffer
    capital = spend + fees
    roi = net / capital if capital > 0 else 0.0
    return StructuralOpportunity(
        strategy=ARB_BUY_MERGE,
        condition_id=pair.condition_id,
        combo_key=pair.combo_key,
        detected_ts_ms=int(detected_ts_ms),
        up_book_id=pair.up_book_id,
        down_book_id=pair.down_book_id,
        up_book_ts_ms=int(pair.up.ts_ms),
        down_book_ts_ms=int(pair.down.ts_ms),
        source_skew_ms=abs(int(pair.up.ts_ms) - int(pair.down.ts_ms)),
        max_book_age_ms=max(0, int(detected_ts_ms) - min(int(pair.up.ts_ms), int(pair.down.ts_ms))),
        quantity_shares=q,
        up_vwap=float(up.vwap),
        down_vwap=float(down.vwap),
        up_fee_usdc=float(up.fee_usdc),
        down_fee_usdc=float(down.fee_usdc),
        gross_edge_per_share=float(gross_edge),
        gross_profit_usdc=float(gross_profit),
        execution_buffer_usdc=float(buffer),
        net_profit_usdc=float(net),
        capital_usdc=float(capital),
        net_roi=float(roi),
        up_limit_price=float(up.worst_price),
        down_limit_price=float(down.worst_price),
        fee_lineage_ok=True,
    )


def evaluate_split_sell_quantity(
    pair: BookPair,
    *,
    quantity_shares: float,
    up_fee: FeeSchedule,
    down_fee: FeeSchedule,
    detected_ts_ms: int,
    execution_buffer_per_share: float,
) -> Optional[StructuralOpportunity]:
    up = simulate_sell_shares(pair.up, quantity_shares, up_fee)
    down = simulate_sell_shares(pair.down, quantity_shares, down_fee)
    if not up.complete or not down.complete or up.vwap is None or down.vwap is None:
        return None
    q = float(quantity_shares)
    proceeds = up.notional_usdc + down.notional_usdc
    fees = up.fee_usdc + down.fee_usdc
    gross_edge = up.vwap + down.vwap - 1.0
    gross_profit = proceeds - fees - q
    buffer = q * float(execution_buffer_per_share)
    net = gross_profit - buffer
    capital = q
    roi = net / capital if capital > 0 else 0.0
    return StructuralOpportunity(
        strategy=ARB_SPLIT_SELL,
        condition_id=pair.condition_id,
        combo_key=pair.combo_key,
        detected_ts_ms=int(detected_ts_ms),
        up_book_id=pair.up_book_id,
        down_book_id=pair.down_book_id,
        up_book_ts_ms=int(pair.up.ts_ms),
        down_book_ts_ms=int(pair.down.ts_ms),
        source_skew_ms=abs(int(pair.up.ts_ms) - int(pair.down.ts_ms)),
        max_book_age_ms=max(0, int(detected_ts_ms) - min(int(pair.up.ts_ms), int(pair.down.ts_ms))),
        quantity_shares=q,
        up_vwap=float(up.vwap),
        down_vwap=float(down.vwap),
        up_fee_usdc=float(up.fee_usdc),
        down_fee_usdc=float(down.fee_usdc),
        gross_edge_per_share=float(gross_edge),
        gross_profit_usdc=float(gross_profit),
        execution_buffer_usdc=float(buffer),
        net_profit_usdc=float(net),
        capital_usdc=float(capital),
        net_roi=float(roi),
        up_limit_price=float(up.worst_price),
        down_limit_price=float(down.worst_price),
        fee_lineage_ok=True,
    )


def best_buy_merge(
    pair: BookPair,
    *,
    up_fee: FeeSchedule,
    down_fee: FeeSchedule,
    detected_ts_ms: int,
    max_quantity_shares: float,
    execution_buffer_per_share: float = 0.0,
) -> Optional[StructuralOpportunity]:
    candidates = _breakpoints(pair.up.asks, pair.down.asks, max_quantity_shares)
    values = [
        evaluate_buy_merge_quantity(
            pair,
            quantity_shares=q,
            up_fee=up_fee,
            down_fee=down_fee,
            detected_ts_ms=detected_ts_ms,
            execution_buffer_per_share=execution_buffer_per_share,
        )
        for q in candidates
    ]
    feasible = [value for value in values if value is not None]
    return max(feasible, key=lambda value: value.net_profit_usdc, default=None)


def best_split_sell(
    pair: BookPair,
    *,
    up_fee: FeeSchedule,
    down_fee: FeeSchedule,
    detected_ts_ms: int,
    max_quantity_shares: float,
    execution_buffer_per_share: float = 0.0,
) -> Optional[StructuralOpportunity]:
    candidates = _breakpoints(pair.up.bids, pair.down.bids, max_quantity_shares)
    values = [
        evaluate_split_sell_quantity(
            pair,
            quantity_shares=q,
            up_fee=up_fee,
            down_fee=down_fee,
            detected_ts_ms=detected_ts_ms,
            execution_buffer_per_share=execution_buffer_per_share,
        )
        for q in candidates
    ]
    feasible = [value for value in values if value is not None]
    return max(feasible, key=lambda value: value.net_profit_usdc, default=None)
