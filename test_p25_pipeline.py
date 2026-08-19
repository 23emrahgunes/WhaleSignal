"""Regression tests for P2.2-P2.5 shadow direction pipeline."""
from __future__ import annotations

import time

import pytest

from p25_calibration import CalSample, CalibrationBook
from p25_config import Settings
from p25_model import (
    MIN_MARKETS_PREDICT,
    DirectionModel,
    ptb_heuristic_probability,
)
from features import FeatureVector
from p25_engine import decide_chainlink_close
from models import (
    AbstainReason,
    Asset,
    AssetHorizon,
    Decision,
    FeatureSnapshot,
    Horizon,
    MarketRef,
    ResolutionType,
)
from p25_recorder import P25Recorder as Recorder
from p25_regime import classify_regime


BTC5 = AssetHorizon(Asset.BTC, Horizon.H5M)


def _fv(**overrides):
    base = dict(
        combo=BTC5,
        ts=1000.0,
        seconds_remaining=120.0,
        ret_fast=0.0003,
        ret_mid=0.0007,
        ret_slow=0.0012,
        sign_persistence=0.82,
        flip_rate=0.12,
        flow_fast=0.45,
        flow_mid=0.42,
        flow_slow=0.30,
        flow_persistence=0.80,
        rv_fast=0.0008,
        rv_slow=0.0010,
        vol_accel=0.8,
        vol_percentile=0.55,
        mom_vol_ratio=1.2,
        distance_bps=6.0,
        distance_slope=0.2,
        ptb_z=1.1,
        tte_fraction=0.4,
        elapsed_fraction=0.6,
        obi_20=0.30,
        ofi=0.20,
        book_flow_agree=1.0,
        up_mid=0.62,
        down_mid=0.38,
        clob_spread=0.04,
        clob_complement_residual=0.0,
        up_mid_vel=0.01,
        clob_spot_agree=1.0,
        feature_coverage=1.0,
        feature_ready=True,
        missing_features=[],
        has_reference=True,
        has_clob=True,
    )
    base.update(overrides)
    return FeatureVector(**base)


def _ref(condition_id: str = "0xp225") -> MarketRef:
    start = 1_800_000_000.0
    return MarketRef(
        combo=BTC5,
        condition_id=condition_id,
        slug=f"btc-updown-5m-{int(start)}",
        question="Bitcoin Up or Down?",
        up_token_id="up-token",
        down_token_id="down-token",
        start_ts=start,
        end_ts=start + 300,
        market_start_ts=start,
        market_end_ts=start + 300,
        resolution_source="Chainlink BTC/USD Data Stream",
        resolution_type=ResolutionType.CHAINLINK,
        official_reference_open=100.0,
        official_reference_open_time=start,
        official_reference_source="CHAINLINK_DATA_STREAM_RTDS",
    )


def _snapshot() -> FeatureSnapshot:
    return FeatureSnapshot(
        combo=BTC5,
        ts=time.time(),
        seconds_remaining=60.0,
        tte_sec=60.0,
        market_start=1_800_000_000.0,
        market_end=1_800_000_300.0,
        spot_price=101.0,
        reference_price=100.0,
        distance_bps=100.0,
        official_reference_open=100.0,
        official_reference_open_time=1_800_000_000.0,
        official_reference_source="CHAINLINK_DATA_STREAM_RTDS",
        up_bid=0.60,
        up_ask=0.62,
        up_mid=0.61,
        down_bid=0.38,
        down_ask=0.40,
        down_mid=0.39,
        transport_age_ms=50.0,
        source_age_ms=50.0,
        book_age_ms=50.0,
        clob_age_ms=50.0,
        reference_age_ms=50.0,
        quality_status="OK",
    )


# ---------------------------------------------------------------------------
# Phase capabilities/safety
# ---------------------------------------------------------------------------


def test_p25_capabilities(monkeypatch):
    monkeypatch.setenv("PHASE", "P2.5")
    monkeypatch.setenv("MODEL_TRAINING_ENABLED", "true")
    monkeypatch.setenv("CALIBRATION_ENABLED", "true")
    monkeypatch.setenv("FORECAST_RECORDING_ENABLED", "true")
    settings = Settings()
    settings.enforce_phase_lock()
    assert settings.feature_engine_active
    assert settings.predictability_active
    assert settings.model_inference_active
    assert settings.training_active
    assert settings.calibration_active
    assert settings.forecast_recording_active


def test_training_rejected_before_p23(monkeypatch):
    monkeypatch.setenv("PHASE", "P2.2")
    monkeypatch.setenv("MODEL_TRAINING_ENABLED", "true")
    monkeypatch.setenv("CALIBRATION_ENABLED", "false")
    with pytest.raises(SystemExit):
        Settings().enforce_phase_lock()


def test_calibration_rejected_before_p24(monkeypatch):
    monkeypatch.setenv("PHASE", "P2.3")
    monkeypatch.setenv("MODEL_TRAINING_ENABLED", "false")
    monkeypatch.setenv("CALIBRATION_ENABLED", "true")
    with pytest.raises(SystemExit):
        Settings().enforce_phase_lock()


# ---------------------------------------------------------------------------
# P2.2 predictability/regime
# ---------------------------------------------------------------------------


def test_predictability_v2_trend_passes():
    result = classify_regime(_fv(), min_predictability=0.55)
    assert not result.abstain
    assert result.regime.value == "TREND_UP"
    assert result.predictability >= 0.55
    assert result.directional_consensus > 0.5
    assert result.conflict_score < 0.3
    assert result.components["coverage"] == pytest.approx(1.0)


def test_predictability_v2_conflict_abstains():
    result = classify_regime(
        _fv(
            flow_fast=-0.8,
            flow_mid=-0.8,
            flow_slow=-0.7,
            flow_persistence=0.95,
            obi_20=-0.6,
            ofi=-0.5,
            up_mid_vel=-0.02,
            book_flow_agree=1.0,
            clob_spot_agree=-1.0,
        ),
        min_predictability=0.50,
    )
    assert result.abstain
    assert result.abstain_reason == AbstainReason.FEATURE_CONFLICT
    assert result.conflict_score > 0.4


def test_predictability_v2_high_vol_abstains():
    result = classify_regime(
        _fv(vol_percentile=0.99, vol_accel=3.0),
        min_predictability=0.50,
    )
    assert result.abstain
    assert result.abstain_reason == AbstainReason.HIGH_VOL


def test_predictability_v2_missing_features_abstains():
    result = classify_regime(
        _fv(
            feature_ready=False,
            feature_coverage=0.40,
            missing_features=["ret_60s", "rv_60s"],
        )
    )
    assert result.abstain
    assert result.abstain_reason == AbstainReason.INSUFFICIENT_DATA


# ---------------------------------------------------------------------------
# P2.3 baselines/models
# ---------------------------------------------------------------------------


def test_ptb_heuristic_is_bounded_and_monotonic():
    down = ptb_heuristic_probability(_fv(ptb_z=-1.5, distance_bps=-8.0))
    flat = ptb_heuristic_probability(_fv(ptb_z=0.0, distance_bps=0.0))
    up = ptb_heuristic_probability(_fv(ptb_z=1.5, distance_bps=8.0))
    assert 0.0 < down < flat < up < 1.0
    assert flat == pytest.approx(0.5, abs=0.02)


def test_three_model_variants_train_and_predict():
    model = DirectionModel(
        per_combo_min=999,
        min_markets_predict=MIN_MARKETS_PREDICT,
    )
    for _ in range(MIN_MARKETS_PREDICT + 3):
        model.learn_with_label(BTC5.key, [_fv()], 1)
        model.learn_with_label(
            BTC5.key,
            [
                _fv(
                    ret_fast=-0.0003,
                    ret_mid=-0.0007,
                    ret_slow=-0.0012,
                    flow_fast=-0.45,
                    flow_mid=-0.42,
                    flow_slow=-0.30,
                    distance_bps=-6.0,
                    distance_slope=-0.2,
                    ptb_z=-1.1,
                    obi_20=-0.30,
                    ofi=-0.20,
                    up_mid=0.38,
                    down_mid=0.62,
                    up_mid_vel=-0.01,
                )
            ],
            0,
        )
    output = model.predict(BTC5.key, _fv())
    assert output.ready
    assert output.p_up is not None and output.p_up > 0.5
    assert output.p_up_no_clob is not None
    assert output.p_up_ptb is not None
    assert output.p_up_ptb_heuristic is not None
    stats = model.stats()
    assert stats["b2_full"]["shared_ready"]
    assert stats["b1_external"]["shared_ready"]
    assert stats["ptb_only"]["shared_ready"]


# ---------------------------------------------------------------------------
# P2.4 calibration/threshold
# ---------------------------------------------------------------------------


def test_reliability_calibration_and_learned_threshold():
    book = CalibrationBook(min_n=20)
    combo = BTC5.key
    for idx in range(80):
        outcome_up = idx < 60
        book.record(
            combo,
            CalSample(
                decided=True,
                outcome_up=outcome_up,
                p_up=0.70,
                decision_up=True,
                confidence=0.40,
                market_implied_up=0.55,
            ),
        )
    calibrated = book.calibrate(
        combo,
        0.70,
        min_samples=50,
        min_bin_samples=12,
        prior_strength=20.0,
    )
    assert calibrated.source == "PER_COMBO_RELIABILITY"
    assert 0.70 < calibrated.p_up < 0.80

    threshold = book.decision_threshold(
        combo,
        default=0.62,
        min_samples=60,
        min_covered=24,
        target_accuracy=0.56,
    )
    assert threshold.source == "PER_COMBO_LEARNED"
    assert threshold.covered >= 24
    assert threshold.accuracy is not None and threshold.accuracy >= 0.56


def test_calibration_is_honest_when_insufficient():
    book = CalibrationBook(min_n=30)
    book.record(
        BTC5.key,
        CalSample(
            decided=False,
            outcome_up=True,
            p_up=0.72,
            decision_up=True,
        ),
    )
    calibrated = book.calibrate(
        BTC5.key,
        0.72,
        min_samples=50,
        min_bin_samples=12,
        prior_strength=20.0,
    )
    assert calibrated.p_up == pytest.approx(0.72)
    assert "INSUFFICIENT" in calibrated.source


# ---------------------------------------------------------------------------
# P2.5 forecast recorder/analytics
# ---------------------------------------------------------------------------


def _trace(decision: str = "UP") -> dict:
    return {
        "phase": "P2.5",
        "model_version": "MODEL_B2_LOGISTIC_V1",
        "model_source": "MODEL_B2_FULL:shared",
        "feature_ready": True,
        "feature_coverage": 1.0,
        "predictability": 0.80,
        "conflict_score": 0.10,
        "directional_consensus": 0.80,
        "regime": "TREND_UP",
        "p_up_raw": 0.72,
        "p_up_calibrated": 0.70,
        "p_up_ptb": 0.66,
        "p_up_ptb_heuristic": 0.64,
        "p_up_external": 0.68,
        "p_up_market": 0.60,
        "confidence": 0.32,
        "decision": decision,
        "abstain_reason": (
            "NONE" if decision != "ABSTAIN" else "LOW_PREDICTABILITY"
        ),
        "threshold": 0.62,
        "threshold_source": "DEFAULT_INSUFFICIENT",
        "calibration_source": "RAW_INSUFFICIENT",
    }


def test_forecast_dedup_settlement_and_analytics(tmp_path):
    recorder = Recorder(str(tmp_path / "p25.sqlite"))
    ref = _ref()
    snap = _snapshot()
    recorder.record_market(ref)
    recorder.record_snapshot(ref, snap, 60)
    assert recorder.record_forecast(ref, snap, 60, _trace())
    assert not recorder.record_forecast(ref, snap, 60, _trace())

    stats = recorder.stats()
    assert stats["forecasts"] == 1

    ref.resolved = True
    ref.official_result = Decision.UP
    ref.resolved_outcome = Decision.UP
    ref.official_result_source = "winning_asset_id"
    ref.computed_result = Decision.UP
    ref.computed_result_source = "CHAINLINK_DATA_STREAM_RTDS_CLOSE"
    recorder.settle(ref)

    stats = recorder.stats()
    assert stats["labeled_forecasts"] == 1
    assert stats["labeled_snapshots"] == 1
    analytics = recorder.forecast_analytics(min_n=1)["overall"]
    assert analytics["n"] == 1
    assert analytics["coverage"] == 1.0
    assert analytics["accuracy"] == 1.0
    assert analytics["brier_b2"] == pytest.approx(0.09)
    assert analytics["brier_naive_50"] == pytest.approx(0.25)
    recorder.close()


def test_official_only_evaluates_forecast_but_not_training_snapshot(tmp_path):
    recorder = Recorder(str(tmp_path / "official_only.sqlite"))
    ref = _ref("0xofficial")
    snap = _snapshot()
    recorder.record_market(ref)
    recorder.record_snapshot(ref, snap, 60)
    recorder.record_forecast(ref, snap, 60, _trace("ABSTAIN"))

    ref.resolved = True
    ref.official_result = Decision.DOWN
    ref.resolved_outcome = Decision.DOWN
    ref.official_result_source = "winning_outcome"
    ref.computed_result = None
    recorder.settle(ref)

    stats = recorder.stats()
    assert stats["labeled_forecasts"] == 1
    assert stats["labeled_snapshots"] == 0
    row = recorder.conn.execute(
        "SELECT label_status FROM markets WHERE condition_id=?",
        (ref.condition_id,),
    ).fetchone()
    assert row[0] == "UNKNOWN"
    recorder.close()


def test_chainlink_close_audit():
    up, source = decide_chainlink_close(100.0, 100.01)
    down, _ = decide_chainlink_close(100.0, 99.99)
    assert up == Decision.UP
    assert down == Decision.DOWN
    assert source == "CHAINLINK_DATA_STREAM_RTDS_CLOSE"
    assert decide_chainlink_close(None, 100.0) == (None, None)


def test_no_execution_configuration_exists():
    fields = Settings.model_fields
    forbidden = {
        "private_key",
        "api_secret",
        "order_submit",
        "live_execution_enabled",
        "wallet_key",
    }
    assert forbidden.isdisjoint(fields)
