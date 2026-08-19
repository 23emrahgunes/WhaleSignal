"""Integration-level smoke tests for the P2.5 decision path."""

import pytest

from features import FeatureVector
from models import (
    AbstainReason,
    Asset,
    AssetHorizon,
    Decision,
    FeatureSnapshot,
    Horizon,
    QStatus,
    QualityReport,
)
from p25_calibration import CalibrationBook
from p25_config import Settings
from p25_engine import P25Engine
from p25_model import DirectionModel, MIN_MARKETS_PREDICT
from p25_recorder import P25Recorder


COMBO = AssetHorizon(Asset.BTC, Horizon.H5M)


def _fv(sign=1.0):
    return FeatureVector(
        combo=COMBO,
        ts=1.0,
        seconds_remaining=120.0,
        ret_fast=0.0004 * sign,
        ret_mid=0.0008 * sign,
        ret_slow=0.0012 * sign,
        sign_persistence=0.85,
        flip_rate=0.10,
        flow_fast=0.55 * sign,
        flow_mid=0.48 * sign,
        flow_slow=0.35 * sign,
        flow_persistence=0.85,
        rv_fast=0.0008,
        rv_slow=0.0010,
        vol_accel=0.8,
        vol_percentile=0.55,
        mom_vol_ratio=1.2 * sign,
        distance_bps=7.0 * sign,
        distance_slope=0.2 * sign,
        ptb_z=1.2 * sign,
        tte_fraction=0.4,
        elapsed_fraction=0.6,
        obi_20=0.35 * sign,
        ofi=0.25 * sign,
        book_flow_agree=1.0,
        up_mid=0.64 if sign > 0 else 0.36,
        down_mid=0.36 if sign > 0 else 0.64,
        clob_spread=0.04,
        clob_complement_residual=0.0,
        up_mid_vel=0.015 * sign,
        clob_spot_agree=1.0,
        feature_ready=True,
        feature_coverage=1.0,
        missing_features=[],
        has_reference=True,
        has_clob=True,
    )


def _quality():
    return QualityReport(
        time=QStatus.OK,
        market=QStatus.OK,
        tokens=QStatus.OK,
        clob=QStatus.OK,
        reference=QStatus.OK,
        clock=QStatus.OK,
        model=QStatus.OK,
        prediction_ready=True,
        snapshot_recordable=True,
        abstain_reason=AbstainReason.NONE,
        notes=[],
    )


def _snapshot():
    return FeatureSnapshot(
        combo=COMBO,
        ts=1.0,
        seconds_remaining=120.0,
        tte_sec=120.0,
        up_mid=0.64,
        down_mid=0.36,
    )


def test_p25_engine_model_decision_path(monkeypatch, tmp_path):
    monkeypatch.setenv("PHASE", "P2.5")
    monkeypatch.setenv("MODEL_TRAINING_ENABLED", "true")
    monkeypatch.setenv("CALIBRATION_ENABLED", "true")
    cfg = Settings()
    cfg.enforce_phase_lock()

    model = DirectionModel(per_combo_min=999, min_markets_predict=MIN_MARKETS_PREDICT)
    for _ in range(MIN_MARKETS_PREDICT + 3):
        model.learn_with_label(COMBO.key, [_fv(1.0)], 1)
        model.learn_with_label(COMBO.key, [_fv(-1.0)], 0)

    recorder = P25Recorder(str(tmp_path / "smoke.sqlite"))
    engine = P25Engine(cfg, None, recorder, model, CalibrationBook(min_n=10))
    bundle = engine.decide(None, _snapshot(), _quality(), _fv(1.0))
    # replace missing ref-dependent trace via a tiny ref-like object
    recorder.close()
