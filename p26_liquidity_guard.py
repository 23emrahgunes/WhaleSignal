"""Transient-liquidity and ghost-liquidity risk controls for Paper V2.

The module reports *risk*, never an allegation of confirmed spoofing.  Public
order-book behavior cannot establish manipulative intent.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from p26_execution import OrderBookSnapshot, executable_depth_usdc


@dataclass(frozen=True)
class LiquidityMetrics:
    snapshot_count: int
    current_spread: Optional[float]
    executable_depth_usdc: float
    depth_persistence_ms: int
    quote_lifetime_ms: int
    book_flicker_rate: float
    cancel_to_add_ratio: Optional[float]
    sequence_gap_count: int
    current_age_ms: int


@dataclass(frozen=True)
class LiquidityGateResult:
    allowed: bool
    reason: str
    metrics: LiquidityMetrics
    details: tuple[str, ...]


def _level_map(snapshot: OrderBookSnapshot) -> dict[float, float]:
    return {float(level.price): float(level.size) for level in snapshot.asks}


def liquidity_metrics(
    history: Sequence[OrderBookSnapshot],
    *,
    now_ms: int,
    stake_usdc: float,
) -> LiquidityMetrics:
    if not history:
        return LiquidityMetrics(0, None, 0.0, 0, 0, 1.0, None, 0, 10**18)
    ordered = sorted(history, key=lambda snapshot: snapshot.ts_ms)
    current = ordered[-1]
    best = current.best_ask

    # Persistence is the continuous time for which the current best ask existed
    # with enough aggregate depth to cover the requested stake.
    persistent_from = current.ts_ms
    for snapshot in reversed(ordered[:-1]):
        if snapshot.best_ask != best or executable_depth_usdc(snapshot.asks) + 1e-12 < stake_usdc:
            break
        persistent_from = snapshot.ts_ms
    persistence = max(0, current.ts_ms - persistent_from)

    changes = 0
    additions = 0.0
    removals = 0.0
    gaps = 0
    for previous, snapshot in zip(ordered, ordered[1:]):
        if previous.best_ask != snapshot.best_ask or _level_map(previous) != _level_map(snapshot):
            changes += 1
        prev = _level_map(previous)
        curr = _level_map(snapshot)
        for price in set(prev) | set(curr):
            delta = curr.get(price, 0.0) - prev.get(price, 0.0)
            if delta > 0:
                additions += delta
            elif delta < 0:
                removals += -delta
        if previous.sequence is not None and snapshot.sequence is not None:
            if snapshot.sequence > previous.sequence + 1:
                gaps += snapshot.sequence - previous.sequence - 1
    transitions = max(1, len(ordered) - 1)
    ratio = removals / additions if additions > 0 else (float("inf") if removals > 0 else None)
    return LiquidityMetrics(
        snapshot_count=len(ordered),
        current_spread=current.spread,
        executable_depth_usdc=executable_depth_usdc(current.asks),
        depth_persistence_ms=persistence,
        quote_lifetime_ms=persistence,
        book_flicker_rate=changes / transitions,
        cancel_to_add_ratio=ratio,
        sequence_gap_count=gaps,
        current_age_ms=max(0, int(now_ms) - int(current.ts_ms)),
    )


def evaluate_liquidity_gate(
    history: Sequence[OrderBookSnapshot],
    *,
    now_ms: int,
    stake_usdc: float,
    max_book_age_ms: int,
    max_spread: float,
    min_depth_persistence_ms: int,
    min_fill_fraction: float,
    max_flicker_rate: float = 0.85,
    max_cancel_to_add_ratio: float = 8.0,
) -> LiquidityGateResult:
    metrics = liquidity_metrics(history, now_ms=now_ms, stake_usdc=stake_usdc)
    if metrics.snapshot_count == 0:
        return LiquidityGateResult(False, "BOOK_MISSING", metrics, ())
    if metrics.current_age_ms > max_book_age_ms:
        return LiquidityGateResult(False, "STALE_BOOK", metrics, (f"age={metrics.current_age_ms}",))
    if metrics.sequence_gap_count > 0:
        return LiquidityGateResult(False, "BOOK_SEQUENCE_GAP", metrics, (f"gaps={metrics.sequence_gap_count}",))
    if metrics.current_spread is None or metrics.current_spread < 0:
        return LiquidityGateResult(False, "INVALID_SPREAD", metrics, ())
    if metrics.current_spread > max_spread:
        return LiquidityGateResult(False, "WIDE_SPREAD", metrics, (f"spread={metrics.current_spread:.4f}",))
    required_depth = float(stake_usdc) * float(min_fill_fraction)
    if metrics.executable_depth_usdc + 1e-12 < required_depth:
        return LiquidityGateResult(
            False,
            "INSUFFICIENT_BOOK_DEPTH",
            metrics,
            (f"depth={metrics.executable_depth_usdc:.4f}<required={required_depth:.4f}",),
        )
    if metrics.depth_persistence_ms < min_depth_persistence_ms:
        return LiquidityGateResult(
            False,
            "GHOST_LIQUIDITY_RISK",
            metrics,
            (f"persistence_ms={metrics.depth_persistence_ms}",),
        )
    if metrics.book_flicker_rate > max_flicker_rate:
        return LiquidityGateResult(
            False,
            "TRANSIENT_LIQUIDITY_RISK",
            metrics,
            (f"flicker_rate={metrics.book_flicker_rate:.3f}",),
        )
    if metrics.cancel_to_add_ratio is not None and metrics.cancel_to_add_ratio > max_cancel_to_add_ratio:
        return LiquidityGateResult(
            False,
            "SPOOFING_RISK",
            metrics,
            (f"cancel_to_add={metrics.cancel_to_add_ratio:.3f}",),
        )
    return LiquidityGateResult(True, "PASS", metrics, ())
