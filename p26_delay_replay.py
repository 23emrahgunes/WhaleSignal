"""Replay executable net edge at fixed delays after a P2.6 forecast."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from p26_alpha_decay import AlphaDecayMetrics, EdgeObservation, analyze_alpha_decay
from p26_execution import OrderBookSnapshot, simulate_buy


DEFAULT_DELAYS_MS = (0, 100, 250, 500, 1000, 2000, 5000, 10000)


@dataclass(frozen=True)
class DelayReplayResult:
    observations: tuple[EdgeObservation, ...]
    decay: AlphaDecayMetrics
    missing_delays_ms: tuple[int, ...]


def replay_edge_curve(
    *,
    forecast_ts_ms: int,
    books: Sequence[OrderBookSnapshot],
    conservative_probability: float,
    stake_usdc: float,
    fee_bps: float,
    safety_buffer: float,
    delays_ms: Iterable[int] = DEFAULT_DELAYS_MS,
    max_book_wait_ms: int = 200,
) -> DelayReplayResult:
    ordered = sorted(books, key=lambda book: book.ts_ms)
    observations: list[EdgeObservation] = []
    missing: list[int] = []
    for delay in delays_ms:
        target = int(forecast_ts_ms) + int(delay)
        candidates = [book for book in ordered if target <= book.ts_ms <= target + max_book_wait_ms]
        if not candidates:
            missing.append(int(delay))
            continue
        book = candidates[0]
        fill = simulate_buy(book, stake_usdc=stake_usdc, fee_bps=fee_bps)
        if fill.all_in_cost_per_share is None or not fill.complete:
            missing.append(int(delay))
            continue
        edge = float(conservative_probability) - fill.all_in_cost_per_share - float(safety_buffer)
        observations.append(EdgeObservation(int(delay), edge))
    return DelayReplayResult(
        observations=tuple(observations),
        decay=analyze_alpha_decay(observations),
        missing_delays_ms=tuple(missing),
    )
