"""P2.5 end-to-end shadow pipeline regression tests."""
from __future__ import annotations

import time

import pytest

from baselines import BaselineOutput
from calibration import CalibrationBook, CalibrationOutput, ThresholdOutput
from config import Settings
from direction_model import MODEL_VERSION, DirectionModel, ModelOutput
from feature_codec import feature_vector_from_payload
from features import FeatureVector
from forecasting import FORECAST_VERSION, ShadowForecaster
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
from recorder import Recorder
from shadow_learning import apply_pending_updates


COMBO = AssetHorizon(Asset.BTC, Horizon.H5M)


def _feature(sign: int = 1, **overrides) -> FeatureVector:
    s = float(sign)
    base = dict(
        combo=COMBO,
        ts=1_000.0,
        seconds_remaining=90.0,
        ret_fast=0.0003 * s,
        ret_mid=0.0006 * s,
        ret_slow=0.0010 * s,
        ret_multi={"5000": 0.0003*s, "15000": 0.0006*s, "60000": 0.0010*s},
        sign_persistence=0.88,
        flip_rate=0.08,
        flow_mid=0.75 * s,
        flow_notional_5s=0.70 * s,
        flow_persistence=0.90,
        rv_fast=0.0008,
        rv_slow=0.0010,
        vol_accel=0.8,
        vol_percentile=0.55,
        distance_bps=8.0 * s,
        distance_slope=0.45 * s,
        obi_20=0.60 * s,
        ofi=0.50 * s,
        book_flow_agree=1.0,
        up_mid=0.66 if sign > 0 else 0.34,
        down_mid=0.34 if sign > 0 else 0.66,
        up_mid_vel=0.010 * s,
        clob_spread=0.04,
        clob_complement_residual=0.0,
        has_reference=True,
        has_clob=True,
        feature_ready=True,
        feature_coverage=0.96,
        price_history_span_sec=180.0,
    )
    base.update(overrides)
    return FeatureVector(**base)


def _market(condition: str = "cond-1") -> MarketRef:
    return MarketRef(
        combo=COMBO,
        condition_id=condition,
        slug="btc-updown-5m-1700000000",
        question="BTC Up or Down?",
        up_token_id=f"{condition}-up",
        down_token_id=f"{condition}-down",
        start_ts=1_700_000_000.0,
        end_ts=1_700_000_300.0,
        resolution_source="Chainlink Data Streams",
        resolution_type=ResolutionType.CHAINLINK_TWAP,
        market_start_ts=1_700_000_000.0,
        market_end_ts=1_700_000_300.0,
    )


class _StubModel:
    def __init__(self, output: ModelOutput) -> None:
        self.output = output
        self.calls = 0

    def predict(self, combo_key: str, fv: FeatureVector) -> ModelOutput:
        self.calls += 1
        return self.output


class _StubCalibration:
    def __init__(self, *, ready: bool, threshold_ready: bool, p: float = 0.82) -> None:
        self.ready = ready
        self.threshold_ready = threshold_ready
        self.p = p

    def calibrate(self, combo_key: str, raw_p: float) -> CalibrationOutput:
        return CalibrationOutput(raw_p, self.p, self.ready, "test", 50)

    def threshold_for(self, combo_key: str) -> ThresholdOutput:
        return ThresholdOutput(0.10, self.threshold_ready, "test", 40, 0.5, 0.8, 0.68)


def _model_output(p: float | None, ready: bool) -> ModelOutput:
    return ModelOutput(
        p_up=p,
        confidence=0.0 if p is None else 2*abs(p-0.5),
        ready=ready,
        source="shared",
        p_up_no_clob=0.73 if p is not None else None,
        baselines=BaselineOutput(0.5, 0.70, 0.65),
        schema_hash="abc123",
    )


def test_data_gate_prevents_model_call():
    model = _StubModel(_model_output(0.8, True))
    forecaster = ShadowForecaster(model, _StubCalibration(ready=True, threshold_ready=True))
    envelope = forecaster.evaluate(
        COMBO, 1.0, _feature(), data_ready=False,
        data_abstain_reason=AbstainReason.STALE_DATA,
    )
    assert model.calls == 0
    assert envelope.prediction.decision == Decision.ABSTAIN
    assert envelope.prediction.abstain_reason == AbstainReason.STALE_DATA


def test_regime_gate_prevents_model_call():
    model = _StubModel(_model_output(0.8, True))
    forecaster = ShadowForecaster(model, _StubCalibration(ready=True, threshold_ready=True))
    chaotic = _feature(clob_spread=0.30, clob_complement_residual=0.12)
    envelope = forecaster.evaluate(COMBO, 1.0, chaotic, data_ready=True)
    assert model.calls == 0
    assert envelope.prediction.abstain_reason == AbstainReason.CHAOTIC


def test_model_and_calibration_gates_fail_closed():
    not_ready_model = _StubModel(_model_output(None, False))
    forecaster = ShadowForecaster(
        not_ready_model, _StubCalibration(ready=False, threshold_ready=False)
    )
    first = forecaster.evaluate(COMBO, 1.0, _feature(), data_ready=True)
    assert first.prediction.abstain_reason == AbstainReason.MODEL_NOT_TRAINED

    raw_model = _StubModel(_model_output(0.8, True))
    forecaster = ShadowForecaster(
        raw_model, _StubCalibration(ready=False, threshold_ready=False, p=0.8)
    )
    second = forecaster.evaluate(COMBO, 1.0, _feature(), data_ready=True)
    assert second.raw_p_up == pytest.approx(0.8)
    assert second.prediction.decision == Decision.ABSTAIN
    assert second.prediction.abstain_reason == AbstainReason.INSUFFICIENT_DATA
    assert "calibration_not_ready" in second.diagnostics


def test_full_pipeline_emits_shadow_direction_only_when_all_gates_ready():
    model = _StubModel(_model_output(0.78, True))
    forecaster = ShadowForecaster(
        model, _StubCalibration(ready=True, threshold_ready=True, p=0.82)
    )
    envelope = forecaster.evaluate(COMBO, 1.0, _feature(), data_ready=True)
    assert envelope.prediction.decision == Decision.UP
    assert envelope.prediction.abstain_reason == AbstainReason.NONE
    assert envelope.calibration_ready and envelope.threshold_ready
    record = envelope.to_record()
    assert record["forecast_version"] == FORECAST_VERSION
    assert record["p_up_raw"] == pytest.approx(0.78)
    assert record["p_up_calibrated"] == pytest.approx(0.82)


def test_feature_codec_round_trip():
    original = _feature()
    restored = feature_vector_from_payload(
        original.combo, original.ts, original.seconds_remaining, original.to_dict()
    )
    assert restored is not None
    assert restored.feature_ready
    assert restored.ret_multi == original.ret_multi
    assert restored.distance_bps == pytest.approx(original.distance_bps)


def _snapshot(fv: FeatureVector) -> FeatureSnapshot:
    return FeatureSnapshot(
        combo=COMBO,
        ts=time.time(),
        seconds_remaining=60.0,
        tte_sec=60.0,
        market_start=1_700_000_000.0,
        market_end=1_700_000_300.0,
        spot_price=101.0,
        reference_price=100.0,
        up_bid=0.59,
        up_ask=0.61,
        up_mid=0.60,
        down_bid=0.39,
        down_ask=0.41,
        down_mid=0.40,
        transport_age_ms=10.0,
        source_age_ms=20.0,
        book_age_ms=20.0,
        clob_age_ms=20.0,
        quality_status="OK",
        extra=fv.to_dict(),
    )


def _forecast_record(decision: str = "UP") -> dict:
    return {
        "ts": time.time(),
        "forecast_version": FORECAST_VERSION,
        "model_version": MODEL_VERSION,
        "model_source": "shared",
        "model_schema_hash": "abc",
        "p_up_raw": 0.80,
        "p_up_calibrated": 0.82,
        "p_up_no_clob": 0.73,
        "baseline_coinflip": 0.5,
        "baseline_ptb": 0.70,
        "market_implied_up": 0.65,
        "calibration_ready": True,
        "calibration_source": "overall",
        "calibration_markets": 50,
        "threshold_ready": True,
        "threshold_source": "overall",
        "decision_margin": 0.10,
        "decision": decision,
        "abstain_reason": "NONE" if decision != "ABSTAIN" else "LOW_PREDICTABILITY",
        "confidence": 0.64,
        "predictability": 0.80,
        "regime": "TREND_UP",
        "direction_score": 0.8,
        "agreement": 0.9,
        "conflict": 0.1,
        "data_ready": True,
        "feature_ready": True,
        "why": ["test"],
        "regime_diagnostics": {},
        "diagnostics": ["decision_ready"],
    }


def test_recorder_forecast_dedup_and_official_only_label(tmp_path):
    recorder = Recorder(str(tmp_path / "p25.sqlite"))
    ref = _market()
    fv = _feature()
    recorder.record_market(ref)
    assert recorder.record_snapshot(ref, _snapshot(fv), 60)
    assert recorder.record_forecast(
        ref, 60, _forecast_record(), feature_coverage=0.96, quality_status="OK"
    )
    assert not recorder.record_forecast(
        ref, 60, _forecast_record(), feature_coverage=0.96, quality_status="OK"
    )

    ref.resolved = True
    ref.official_result = Decision.UP
    ref.resolved_outcome = Decision.UP
    ref.official_result_source = "outcomePrices"
    status = recorder.settle(ref)
    assert status == "OFFICIAL_ONLY"
    stats = recorder.stats()
    assert stats["official_only"] == 1
    assert stats["labeled_snapshots"] == 1
    assert stats["labeled_forecasts"] == 1
    row = recorder.forecast_rows(ref.condition_id)[0]
    assert row["final_result"] == "UP" and row["correct"] == 1
    recorder.close()


def test_mismatch_is_excluded_from_labels(tmp_path):
    recorder = Recorder(str(tmp_path / "mismatch.sqlite"))
    ref = _market("cond-mismatch")
    recorder.record_market(ref)
    recorder.record_snapshot(ref, _snapshot(_feature()), 60)
    recorder.record_forecast(
        ref, 60, _forecast_record(), feature_coverage=0.96, quality_status="OK"
    )
    ref.resolved = True
    ref.official_result = Decision.UP
    ref.resolved_outcome = Decision.UP
    ref.computed_result = Decision.DOWN
    assert recorder.settle(ref) == "MISMATCH"
    assert recorder.stats()["labeled_snapshots"] == 0
    assert recorder.forecast_rows(ref.condition_id)[0]["final_result"] is None
    recorder.close()


def test_restart_safe_learning_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("PHASE", "P2.5")
    recorder = Recorder(str(tmp_path / "learning.sqlite"))
    ref = _market("cond-learn")
    recorder.record_market(ref)
    recorder.record_snapshot(ref, _snapshot(_feature()), 60)
    recorder.record_forecast(
        ref, 60, _forecast_record(decision="ABSTAIN"),
        feature_coverage=0.96, quality_status="OK",
    )
    ref.resolved = True
    ref.official_result = Decision.UP
    ref.resolved_outcome = Decision.UP
    recorder.settle(ref)

    model = DirectionModel(per_combo_min=20, horizon_min=20)
    calibration = CalibrationBook(min_n=10, min_fit_markets=10, min_threshold_n=10)
    update = apply_pending_updates(
        recorder, model, calibration,
        training_enabled=True, calibration_enabled=True,
        model_path=str(tmp_path / "model.pkl"),
        calibration_path=str(tmp_path / "calibration.pkl"),
    )
    assert update.model_markets == 1 and update.feature_rows == 1
    assert update.calibration_markets == 1 and update.forecast_rows == 1
    again = apply_pending_updates(
        recorder, model, calibration,
        training_enabled=True, calibration_enabled=True,
        model_path=str(tmp_path / "model.pkl"),
        calibration_path=str(tmp_path / "calibration.pkl"),
    )
    assert again.model_markets == 0 and again.calibration_markets == 0
    stats = recorder.stats()
    assert stats["model_updates"] == 1 and stats["calibration_updates"] == 1
    recorder.close()


def test_p25_config_allows_shadow_learning_but_never_execution(monkeypatch):
    monkeypatch.setenv("PHASE", "P2.5")
    monkeypatch.setenv("MODEL_TRAINING_ENABLED", "true")
    monkeypatch.setenv("CALIBRATION_ENABLED", "true")
    cfg = Settings()
    cfg.enforce_phase_lock()
    assert cfg.training_active and cfg.calibration_active
    assert cfg.model_inference_active
