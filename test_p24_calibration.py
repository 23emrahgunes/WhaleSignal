"""P2.4 calibration, hierarchy and selective-threshold tests."""
from __future__ import annotations

import pytest

from calibration import (
    CALIBRATION_VERSION,
    DEFAULT_MARGIN,
    CalSample,
    CalibrationBook,
)


def _sample(
    market_id: str,
    outcome_up: bool,
    p_up: float,
    *,
    checkpoint: int = 60,
    decided: bool = True,
) -> CalSample:
    return CalSample(
        decided=decided,
        outcome_up=outcome_up,
        p_up=p_up,
        decision_up=(p_up > 0.5) if decided else None,
        confidence=2.0 * abs(p_up - 0.5),
        market_implied_up=0.55 if outcome_up else 0.45,
        market_id=market_id,
        checkpoint_sec=checkpoint,
        p_up_no_clob=0.72 if outcome_up else 0.28,
        ptb_baseline=0.68 if outcome_up else 0.32,
    )


def _balanced_book(n: int = 40, *, combo: str = "BTC:5m") -> CalibrationBook:
    book = CalibrationBook(
        min_n=20,
        min_fit_markets=20,
        min_class_markets=5,
        min_threshold_n=20,
        target_accuracy=0.52,
    )
    for i in range(n):
        up = i % 2 == 0
        book.record(combo, _sample(f"m{i:03d}", up, 0.80 if up else 0.20))
    return book


def test_platt_calibration_becomes_ready_and_is_monotonic():
    book = _balanced_book()
    low = book.calibrate("BTC:5m", 0.20)
    high = book.calibrate("BTC:5m", 0.80)
    assert low.ready and high.ready
    assert low.source == high.source == "per_combo"
    assert low.calibrated_p_up is not None and high.calibrated_p_up is not None
    assert 0.0 < low.calibrated_p_up < high.calibrated_p_up < 1.0


def test_hierarchy_falls_back_to_horizon_before_overall():
    book = CalibrationBook(
        min_n=10,
        min_fit_markets=20,
        min_class_markets=4,
        min_threshold_n=20,
    )
    for prefix, combo in (("b", "BTC:5m"), ("e", "ETH:5m")):
        for i in range(12):
            up = i % 2 == 0
            book.record(combo, _sample(f"{prefix}{i}", up, 0.78 if up else 0.22))
    out = book.calibrate("BTC:5m", 0.75)
    assert out.ready
    assert out.source == "per_horizon"
    assert out.n_markets == 24


def test_threshold_policy_is_ready_only_with_evidence():
    book = _balanced_book(40)
    threshold = book.threshold_for("BTC:5m")
    assert threshold.ready
    assert threshold.source == "per_combo"
    assert threshold.n >= 20
    assert threshold.accuracy == pytest.approx(1.0)
    assert threshold.wilson_lower is not None
    assert 0.05 <= threshold.margin <= 0.25


def test_threshold_fails_closed_when_insufficient():
    book = CalibrationBook(min_n=30)
    for i in range(6):
        up = i % 2 == 0
        book.record("BTC:5m", _sample(f"tiny{i}", up, 0.7 if up else 0.3))
    threshold = book.threshold_for("BTC:5m")
    assert not threshold.ready
    assert threshold.margin == pytest.approx(DEFAULT_MARGIN)
    assert threshold.source == "insufficient"


def test_repeated_checkpoints_have_one_unique_market_for_fit():
    book = CalibrationBook(
        min_n=10,
        min_fit_markets=20,
        min_class_markets=5,
        min_threshold_n=20,
    )
    for i in range(20):
        up = i % 2 == 0
        for checkpoint in (120, 60, 30):
            book.record(
                "BTC:5m",
                _sample(
                    f"m{i}", up, 0.77 if up else 0.23,
                    checkpoint=checkpoint,
                ),
            )
    summary = book.summary()["per_combo"]["BTC:5m"]
    assert summary["n_probability"] == 60
    assert summary["unique_markets"] == 20
    assert summary["calibrator_ready"] is True


def test_summary_reports_honest_metrics_and_baselines():
    summary = _balanced_book(40).summary()
    overall = summary["overall"]
    assert summary["version"] == CALIBRATION_VERSION
    assert overall["insufficient"] is False
    assert overall["accuracy"] == pytest.approx(1.0)
    assert 0.0 <= overall["brier"] <= 1.0
    assert 0.0 <= overall["ece"] <= 1.0
    assert overall["baseline_brier"]["coinflip"] == pytest.approx(0.25)
    assert overall["price_edge"]["n"] == 40


def test_calibration_artifact_round_trip(tmp_path):
    book = _balanced_book(40)
    path = tmp_path / "calibration.pkl"
    assert book.save(str(path))
    loaded = CalibrationBook.load(str(path))
    assert loaded is not None
    assert loaded.calibrate("BTC:5m", 0.8).ready
    assert loaded.summary()["version"] == CALIBRATION_VERSION
