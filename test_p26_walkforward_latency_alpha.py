from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from p26_alpha_decay import (
    EdgeObservation,
    analyze_alpha_decay,
    evaluate_alpha_gate,
)
from p26_config import P26Settings
from p26_eval import EvalRecord, evaluate_fold
from p26_features import EXTERNAL_FEATURE_NAMES
from p26_latency import (
    SourceClock,
    compute_latency_metrics,
    evaluate_latency_gate,
)
from p26_walkforward import TemporalRecord, purged_nested_folds, temporal_clusters


def _temporal_records(cluster_count: int = 12, per_cluster: int = 2):
    rows = []
    base = 1_800_000_000_000
    for cluster in range(cluster_count):
        start = base + cluster * 3_600_000
        for index in range(per_cluster):
            rows.append(
                TemporalRecord(
                    condition_id=f"c{cluster}-{index}",
                    market_start_ts_ms=start,
                    market_end_ts_ms=start + 900_000 + index * 60_000,
                    decision_ts_ms=start + 600_000,
                )
            )
    return rows


def test_overlapping_horizons_are_clustered_and_purged():
    records = _temporal_records()
    clusters = temporal_clusters(records)
    assert len(clusters) == 12
    assert all(len(cluster.records) == 2 for cluster in clusters)
    folds = purged_nested_folds(
        records,
        embargo_ms=600_000,
        min_train_records=4,
        min_validation_records=2,
        min_test_records=2,
    )
    assert folds
    for fold in folds:
        assert not (set(fold.train_ids) & set(fold.test_ids))
        assert fold.train_end_ts_ms + fold.embargo_ms < fold.validation_start_ts_ms
        assert fold.validation_end_ts_ms + fold.embargo_ms < fold.test_start_ts_ms


def _eval_records() -> list[EvalRecord]:
    rng = np.random.default_rng(42)
    rows: list[EvalRecord] = []
    base = 1_800_000_000_000
    for cluster in range(18):
        start = base + cluster * 3_600_000
        for index in range(2):
            features = rng.normal(size=len(EXTERNAL_FEATURE_NAMES))
            label = int(features[0] + 0.8 * features[7] > 0)
            rows.append(
                EvalRecord(
                    condition_id=f"e{cluster}-{index}",
                    combo_key="BTC:5m" if index == 0 else "ETH:5m",
                    asset="BTC" if index == 0 else "ETH",
                    horizon="5m",
                    market_start_ts_ms=start,
                    market_end_ts_ms=start + 900_000,
                    decision_ts_ms=start + 600_000,
                    features=tuple(features.tolist()),
                    label_up=label,
                    market_p_up=0.60 if label else 0.40,
                )
            )
    return rows


def test_nested_fold_selects_regularization_on_validation_only(tmp_path):
    records = _eval_records()
    folds = purged_nested_folds(
        records,
        embargo_ms=600_000,
        min_train_records=8,
        min_validation_records=2,
        min_test_records=2,
    )
    assert folds
    settings = P26Settings(
        p25_db_path=str(tmp_path / "p25.sqlite"),
        p26_db_path=str(tmp_path / "p26.sqlite"),
        model_min_train_markets=1,
        model_min_class_markets=1,
    )
    evaluation, predictions = evaluate_fold(settings, records, folds[-1])
    assert evaluation.train_n >= 8
    assert evaluation.validation_n >= 2
    assert evaluation.test_n == len(predictions)
    assert evaluation.selected_c in {0.1, 0.3, 1.0, 3.0}
    assert evaluation.model.brier is not None
    assert evaluation.naive.brier == 0.25
    assert all(row["fold_id"] == folds[-1].fold_id for row in predictions)


def test_latency_mismatch_and_forecast_age_vetoes():
    sources = SourceClock(
        binance_trade_ts_ms=10_000,
        binance_book_ts_ms=9_950,
        chainlink_ts_ms=9_900,
        clob_quote_ts_ms=9_980,
    )
    metrics = compute_latency_metrics(
        decision_ts_ms=10_050,
        sources=sources,
        forecast_created_ts_ms=10_050,
        fill_ts_ms=10_300,
        fill_quote_source_ts_ms=10_250,
    )
    gate = evaluate_latency_gate(
        metrics,
        max_source_skew_ms=200,
        max_decision_data_lag_ms=100,
        max_forecast_age_ms=500,
        max_quote_age_at_fill_ms=100,
    )
    assert gate.allowed
    assert metrics.source_skew_ms == 100
    assert metrics.forecast_age_ms == 250

    bad = compute_latency_metrics(
        decision_ts_ms=20_000,
        sources=SourceClock(20_000, 19_900, 18_000, 19_950),
    )
    rejected = evaluate_latency_gate(
        bad,
        max_source_skew_ms=500,
        max_decision_data_lag_ms=500,
        max_forecast_age_ms=500,
        max_quote_age_at_fill_ms=100,
    )
    assert not rejected.allowed
    assert rejected.reason == "LATENCY_MISMATCH"


def test_future_timestamp_is_rejected():
    metrics = compute_latency_metrics(
        decision_ts_ms=10_000,
        sources=SourceClock(10_001, 9_999, 9_998, 9_997),
    )
    gate = evaluate_latency_gate(
        metrics,
        max_source_skew_ms=100,
        max_decision_data_lag_ms=100,
        max_forecast_age_ms=100,
        max_quote_age_at_fill_ms=100,
    )
    assert gate.reason == "FUTURE_SOURCE_TIMESTAMP"


def test_alpha_decay_half_life_zero_and_ttl_gate():
    metrics = analyze_alpha_decay(
        [
            EdgeObservation(0, 0.08),
            EdgeObservation(500, 0.06),
            EdgeObservation(1000, 0.04),
            EdgeObservation(1500, 0.01),
            EdgeObservation(2000, -0.01),
        ]
    )
    assert metrics.initial_edge == 0.08
    assert metrics.half_life_ms == 1000.0
    assert 1500 < metrics.time_to_zero_edge_ms < 2000
    allowed = evaluate_alpha_gate(
        forecast_age_ms=900,
        current_net_edge=0.035,
        minimum_net_edge=0.02,
        learned_ttl_ms=1600,
        max_fallback_ttl_ms=2000,
    )
    assert allowed.allowed
    expired = evaluate_alpha_gate(
        forecast_age_ms=1700,
        current_net_edge=0.035,
        minimum_net_edge=0.02,
        learned_ttl_ms=1600,
        max_fallback_ttl_ms=2000,
    )
    assert expired.reason == "ALPHA_EXPIRED"
    decayed = evaluate_alpha_gate(
        forecast_age_ms=500,
        current_net_edge=-0.001,
        minimum_net_edge=0.02,
        learned_ttl_ms=None,
        max_fallback_ttl_ms=2000,
    )
    assert decayed.reason == "EDGE_DECAYED_BELOW_ZERO"
