"""P2.3 honest baselines and hierarchical logistic model tests."""
from __future__ import annotations

import random

import pytest

from baselines import baseline_probabilities, ptb_diffusion_probability
from direction_model import (
    MIN_CLASS_MARKETS,
    MIN_MARKETS_PREDICT,
    DirectionModel,
)
from features import FeatureVector
from models import Asset, AssetHorizon, Horizon


def _fv(sign: int = 1, *, combo: AssetHorizon | None = None, jitter: float = 0.0) -> FeatureVector:
    combo = combo or AssetHorizon(Asset.BTC, Horizon.H5M)
    s = float(sign)
    return FeatureVector(
        combo=combo,
        ts=1_000.0,
        seconds_remaining=90.0,
        ret_fast=0.00035 * s + jitter,
        ret_mid=0.00070 * s + jitter,
        ret_slow=0.00120 * s + jitter,
        sign_persistence=0.85,
        flip_rate=0.10,
        flow_fast=0.65 * s,
        flow_mid=0.72 * s,
        flow_slow=0.55 * s,
        flow_persistence=0.90,
        flow_notional_5s=0.68 * s,
        rv_fast=0.0008,
        rv_slow=0.0010,
        vol_accel=0.8,
        vol_percentile=0.55,
        distance_bps=8.0 * s,
        distance_slope=0.4 * s,
        ptb_z=0.8 * s,
        tte_fraction=0.3,
        elapsed_fraction=0.7,
        obi_5=0.6 * s,
        obi_20=0.55 * s,
        ofi=0.45 * s,
        book_flow_agree=1.0,
        up_mid=0.65 if sign > 0 else 0.35,
        down_mid=0.35 if sign > 0 else 0.65,
        clob_spread=0.04,
        up_mid_vel=0.01 * s,
        up_mid_accel=0.002 * s,
        clob_spot_agree=1.0,
        has_reference=True,
        has_clob=True,
        feature_ready=True,
        feature_coverage=0.95,
        price_history_span_sec=180.0,
    )


def test_ptb_baseline_is_bounded_and_monotonic():
    up = _fv(1)
    flat = _fv(1)
    flat.distance_bps = 0.0
    down = _fv(-1)
    p_up = ptb_diffusion_probability(up)
    p_flat = ptb_diffusion_probability(flat)
    p_down = ptb_diffusion_probability(down)
    assert p_up is not None and p_flat is not None and p_down is not None
    assert 0.0 < p_down < p_flat == pytest.approx(0.5) < p_up < 1.0


def test_baselines_are_available_before_model_readiness():
    model = DirectionModel()
    out = model.predict("BTC:5m", _fv(1))
    assert not out.ready and out.p_up is None
    assert out.baselines.coinflip == 0.5
    assert out.baselines.ptb_diffusion is not None
    assert out.baselines.market_implied == pytest.approx(0.65)


def test_readiness_requires_both_classes():
    model = DirectionModel(per_combo_min=20, horizon_min=20)
    for _ in range(MIN_MARKETS_PREDICT + 5):
        model.learn_with_label("BTC:5m", [_fv(1)], 1)
    assert not model.ready_for("BTC:5m")
    stats = model.stats()["with_clob"]["shared"]
    assert stats["class_markets"][0] == 0
    assert stats["class_markets"][1] >= MIN_MARKETS_PREDICT


def test_hierarchical_model_learns_direction_and_market_weights_once():
    rng = random.Random(17)
    model = DirectionModel(per_combo_min=20, horizon_min=20)
    for _ in range(max(MIN_MARKETS_PREDICT // 2, MIN_CLASS_MARKETS)):
        ups = [_fv(1, jitter=rng.gauss(0, 0.00003)) for _ in range(4)]
        downs = [_fv(-1, jitter=rng.gauss(0, 0.00003)) for _ in range(7)]
        model.learn_with_label("BTC:5m", ups, 1)
        model.learn_with_label("BTC:5m", downs, 0)

    up = model.predict("BTC:5m", _fv(1))
    down = model.predict("BTC:5m", _fv(-1))
    assert up.ready and down.ready
    assert up.p_up is not None and down.p_up is not None
    assert up.p_up > 0.5 > down.p_up
    assert up.p_up_no_clob is not None
    assert up.source == "per_combo"

    shared = model.stats()["with_clob"]["shared"]
    # Four/seven checkpoints still contribute one aggregate market weight each.
    assert shared["effective_weight"] == pytest.approx(shared["markets"], abs=1e-3)
    assert shared["samples"] > shared["markets"]


def test_model_artifact_round_trip_and_feature_only_block(tmp_path, monkeypatch):
    model = DirectionModel(per_combo_min=20, horizon_min=20)
    path = tmp_path / "direction.pkl"
    assert model.save(str(path))

    monkeypatch.setenv("PHASE", "P2.5")
    loaded = DirectionModel.load(str(path))
    assert loaded is not None
    assert loaded.stats()["model_version"] == model.stats()["model_version"]

    monkeypatch.setenv("PHASE", "P2.1")
    assert DirectionModel.load(str(path)) is None


def test_baseline_payload_is_json_friendly():
    payload = baseline_probabilities(_fv(1)).to_dict()
    assert set(payload) == {"coinflip", "ptb_diffusion", "market_implied", "version"}
    assert 0.0 < payload["ptb_diffusion"] < 1.0
