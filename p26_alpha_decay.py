"""Signal-edge decay analysis and TTL gates for P2.6."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class EdgeObservation:
    delay_ms: int
    net_edge: float


@dataclass(frozen=True)
class AlphaDecayMetrics:
    initial_edge: float
    last_edge: float
    edge_retention_ratio: Optional[float]
    half_life_ms: Optional[float]
    time_to_zero_edge_ms: Optional[float]
    observation_count: int


@dataclass(frozen=True)
class AlphaGateResult:
    allowed: bool
    reason: str
    details: tuple[str, ...]


def _crossing_time(
    observations: list[EdgeObservation],
    threshold: float,
) -> Optional[float]:
    for previous, current in zip(observations, observations[1:]):
        y0 = previous.net_edge - threshold
        y1 = current.net_edge - threshold
        if y0 == 0:
            return float(previous.delay_ms)
        if y0 > 0 >= y1:
            if current.net_edge == previous.net_edge:
                return float(current.delay_ms)
            fraction = (threshold - previous.net_edge) / (
                current.net_edge - previous.net_edge
            )
            return previous.delay_ms + fraction * (
                current.delay_ms - previous.delay_ms
            )
    if observations and observations[0].net_edge <= threshold:
        return float(observations[0].delay_ms)
    return None


def analyze_alpha_decay(observations: Iterable[EdgeObservation]) -> AlphaDecayMetrics:
    ordered = sorted(observations, key=lambda item: item.delay_ms)
    if not ordered:
        raise ValueError("at least one edge observation is required")
    if ordered[0].delay_ms < 0:
        raise ValueError("delay cannot be negative")
    initial = float(ordered[0].net_edge)
    last = float(ordered[-1].net_edge)
    retention = last / initial if initial > 0 else None
    half_life = (
        _crossing_time(ordered, initial * 0.5) if initial > 0 else None
    )
    zero = _crossing_time(ordered, 0.0)
    return AlphaDecayMetrics(
        initial_edge=initial,
        last_edge=last,
        edge_retention_ratio=retention,
        half_life_ms=half_life,
        time_to_zero_edge_ms=zero,
        observation_count=len(ordered),
    )


def evaluate_alpha_gate(
    *,
    forecast_age_ms: int,
    current_net_edge: float,
    minimum_net_edge: float,
    learned_ttl_ms: Optional[int],
    max_fallback_ttl_ms: int,
) -> AlphaGateResult:
    ttl = int(learned_ttl_ms) if learned_ttl_ms is not None else int(max_fallback_ttl_ms)
    if forecast_age_ms < 0:
        return AlphaGateResult(False, "INVALID_FORECAST_AGE", ())
    if forecast_age_ms > ttl:
        return AlphaGateResult(
            False,
            "ALPHA_EXPIRED",
            (f"forecast_age_ms={forecast_age_ms}>ttl_ms={ttl}",),
        )
    if current_net_edge <= 0:
        return AlphaGateResult(
            False,
            "EDGE_DECAYED_BELOW_ZERO",
            (f"current_net_edge={current_net_edge:.6f}",),
        )
    if current_net_edge < minimum_net_edge:
        return AlphaGateResult(
            False,
            "EDGE_DECAYED_BELOW_THRESHOLD",
            (
                f"current_net_edge={current_net_edge:.6f}",
                f"minimum_net_edge={minimum_net_edge:.6f}",
            ),
        )
    return AlphaGateResult(True, "PASS", ())
