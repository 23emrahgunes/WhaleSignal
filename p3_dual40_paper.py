"""Conservative paper-fill helpers for resting DUAL40 maker bids.

A repeated order-book snapshot must not be counted as fresh sell flow. Paper fill is
therefore the maximum executable ask depth observed at or below the maker price,
not the sum of capacities across polling ticks. This deliberately under-counts
separate sell waves but never turns one visible share into a full 30-share fill.
"""
from __future__ import annotations

from collections.abc import Iterable


def visible_ask_capacity(
    asks: Iterable[tuple[float, float]],
    *,
    max_price: float,
) -> float:
    """Return non-negative visible ask shares priced at or below ``max_price``."""
    limit = float(max_price)
    total = 0.0
    for raw_price, raw_size in asks:
        try:
            price = float(raw_price)
            size = float(raw_size)
        except (TypeError, ValueError):
            continue
        if price <= limit + 1e-12:
            total += max(0.0, size)
    return total


def observed_fill_from_visible_depth(
    *,
    previous_filled: float,
    target_shares: float,
    visible_capacity: float,
) -> float:
    """Advance only to the largest independently observed executable capacity.

    Taking ``max(previous, current_capacity)`` avoids re-consuming an unchanged book
    on every poll. The result is always bounded to ``[0, target_shares]``.
    """
    target = float(target_shares)
    if target <= 0:
        raise ValueError("target_shares must be positive")
    previous = max(0.0, float(previous_filled))
    capacity = max(0.0, float(visible_capacity))
    return min(target, max(previous, capacity))
