"""Latency mismatch and as-of alignment metrics for P2.6."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SourceClock:
    binance_trade_ts_ms: Optional[int]
    binance_book_ts_ms: Optional[int]
    chainlink_ts_ms: Optional[int]
    clob_quote_ts_ms: Optional[int]

    def available(self) -> list[int]:
        return [
            int(value)
            for value in (
                self.binance_trade_ts_ms,
                self.binance_book_ts_ms,
                self.chainlink_ts_ms,
                self.clob_quote_ts_ms,
            )
            if value is not None
        ]


@dataclass(frozen=True)
class LatencyMetrics:
    decision_ts_ms: int
    source_skew_ms: Optional[int]
    decision_data_lag_ms: Optional[int]
    forecast_age_ms: Optional[int]
    quote_age_at_fill_ms: Optional[int]
    no_future_sources: bool
    source_count: int


@dataclass(frozen=True)
class LatencyGateResult:
    allowed: bool
    reason: str
    metrics: LatencyMetrics
    details: tuple[str, ...]


def compute_latency_metrics(
    *,
    decision_ts_ms: int,
    sources: SourceClock,
    forecast_created_ts_ms: Optional[int] = None,
    fill_ts_ms: Optional[int] = None,
    fill_quote_source_ts_ms: Optional[int] = None,
) -> LatencyMetrics:
    values = sources.available()
    no_future = all(value <= decision_ts_ms for value in values)
    source_skew = max(values) - min(values) if len(values) >= 2 else None
    data_lag = decision_ts_ms - max(values) if values else None
    forecast_age = (
        int(fill_ts_ms) - int(forecast_created_ts_ms)
        if fill_ts_ms is not None and forecast_created_ts_ms is not None
        else None
    )
    quote_age = (
        int(fill_ts_ms) - int(fill_quote_source_ts_ms)
        if fill_ts_ms is not None and fill_quote_source_ts_ms is not None
        else None
    )
    return LatencyMetrics(
        decision_ts_ms=int(decision_ts_ms),
        source_skew_ms=source_skew,
        decision_data_lag_ms=data_lag,
        forecast_age_ms=forecast_age,
        quote_age_at_fill_ms=quote_age,
        no_future_sources=no_future,
        source_count=len(values),
    )


def evaluate_latency_gate(
    metrics: LatencyMetrics,
    *,
    required_source_count: int = 4,
    max_source_skew_ms: int,
    max_decision_data_lag_ms: int,
    max_forecast_age_ms: int,
    max_quote_age_at_fill_ms: int,
) -> LatencyGateResult:
    if not metrics.no_future_sources:
        return LatencyGateResult(False, "FUTURE_SOURCE_TIMESTAMP", metrics, ())
    if metrics.source_count < required_source_count:
        return LatencyGateResult(
            False,
            "ASYNC_SOURCE_SNAPSHOT",
            metrics,
            (f"source_count={metrics.source_count}<{required_source_count}",),
        )
    if metrics.source_skew_ms is None or metrics.source_skew_ms > max_source_skew_ms:
        return LatencyGateResult(
            False,
            "LATENCY_MISMATCH",
            metrics,
            (f"source_skew_ms={metrics.source_skew_ms}",),
        )
    if (
        metrics.decision_data_lag_ms is None
        or metrics.decision_data_lag_ms < 0
        or metrics.decision_data_lag_ms > max_decision_data_lag_ms
    ):
        return LatencyGateResult(
            False,
            "DATA_TOO_OLD_AT_DECISION",
            metrics,
            (f"decision_data_lag_ms={metrics.decision_data_lag_ms}",),
        )
    if metrics.forecast_age_ms is not None and (
        metrics.forecast_age_ms < 0 or metrics.forecast_age_ms > max_forecast_age_ms
    ):
        return LatencyGateResult(
            False,
            "FORECAST_TOO_OLD",
            metrics,
            (f"forecast_age_ms={metrics.forecast_age_ms}",),
        )
    if metrics.quote_age_at_fill_ms is not None and (
        metrics.quote_age_at_fill_ms < 0
        or metrics.quote_age_at_fill_ms > max_quote_age_at_fill_ms
    ):
        return LatencyGateResult(
            False,
            "QUOTE_TOO_OLD",
            metrics,
            (f"quote_age_at_fill_ms={metrics.quote_age_at_fill_ms}",),
        )
    return LatencyGateResult(True, "PASS", metrics, ())
