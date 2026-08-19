"""P2.2 predictability/regime regression tests."""
from __future__ import annotations

from features import FeatureVector
from models import Asset, AssetHorizon, AbstainReason, Horizon, Regime
from regime import POLICY_VERSION, classify_regime


def _fv(direction: int = 1, horizon: Horizon = Horizon.H5M, **overrides) -> FeatureVector:
    combo = AssetHorizon(Asset.BTC, horizon)
    s = float(direction)
    base = dict(
        combo=combo,
        ts=1_000.0,
        seconds_remaining=120.0,
        ret_fast=0.00020 * s,
        ret_mid=0.00045 * s,
        ret_slow=0.00080 * s,
        ret_multi={
            "5000": 0.00020 * s,
            "15000": 0.00045 * s,
            "60000": 0.00080 * s,
        },
        sign_persistence=0.86,
        flip_rate=0.10,
        flow_mid=0.70 * s,
        flow_notional_5s=0.60 * s,
        flow_persistence=0.88,
        rv_fast=0.0008,
        rv_slow=0.0010,
        vol_accel=0.80,
        vol_percentile=0.55,
        distance_bps=7.0 * s,
        distance_slope=0.45 * s,
        obi_20=0.55 * s,
        ofi=0.40 * s,
        book_flow_agree=1.0,
        up_mid=0.64 if direction > 0 else 0.36,
        down_mid=0.36 if direction > 0 else 0.64,
        up_mid_vel=0.010 * s,
        clob_spread=0.04,
        clob_complement_residual=0.0,
        has_reference=True,
        has_clob=True,
        price_history_span_sec=180.0 if horizon != Horizon.H1H else 600.0,
        feature_coverage=0.96,
        feature_ready=True,
    )
    base.update(overrides)
    return FeatureVector(**base)


def test_clean_uptrend_is_predictable():
    result = classify_regime(_fv(1))
    assert result.regime == Regime.TREND_UP
    assert not result.abstain
    assert result.abstain_reason == AbstainReason.NONE
    assert result.direction_score > 0.5
    assert result.agreement > 0.8
    assert result.predictability >= 0.58
    assert result.policy_version == POLICY_VERSION


def test_clean_downtrend_is_predictable():
    result = classify_regime(_fv(-1))
    assert result.regime == Regime.TREND_DOWN
    assert not result.abstain
    assert result.direction_score < -0.5


def test_short_history_fails_closed():
    result = classify_regime(_fv(1, price_history_span_sec=12.0))
    assert result.regime == Regime.UNSAFE
    assert result.abstain
    assert result.abstain_reason == AbstainReason.INSUFFICIENT_DATA
    assert any("history" in reason for reason in result.reasons)


def test_high_volatility_abstains_before_model():
    result = classify_regime(_fv(1, vol_percentile=0.99, vol_accel=4.5))
    assert result.regime == Regime.HIGH_VOL
    assert result.abstain
    assert result.abstain_reason == AbstainReason.HIGH_VOL


def test_opposing_groups_trigger_feature_conflict():
    fv = _fv(
        1,
        flow_mid=-0.95,
        flow_notional_5s=-0.95,
        obi_20=-0.95,
        ofi=-0.95,
        up_mid=0.50,
        down_mid=0.50,
        up_mid_vel=0.0,
    )
    result = classify_regime(fv)
    assert result.abstain
    assert result.abstain_reason == AbstainReason.FEATURE_CONFLICT
    assert result.conflict >= 0.55


def test_bad_clob_geometry_is_chaotic():
    result = classify_regime(
        _fv(1, clob_spread=0.30, clob_complement_residual=0.12)
    )
    assert result.regime == Regime.CHAOTIC
    assert result.abstain
    assert result.abstain_reason == AbstainReason.CHAOTIC


def test_hourly_requires_longer_warmup():
    result = classify_regime(
        _fv(1, horizon=Horizon.H1H, price_history_span_sec=200.0)
    )
    assert result.abstain
    assert result.abstain_reason == AbstainReason.INSUFFICIENT_DATA


def test_result_diagnostics_are_bounded_and_serializable():
    result = classify_regime(_fv(1))
    payload = result.to_dict()
    assert -1.0 <= payload["direction_score"] <= 1.0
    assert 0.0 <= payload["agreement"] <= 1.0
    assert 0.0 <= payload["conflict"] <= 1.0
    assert 0.0 <= payload["predictability"] <= 1.0
    assert payload["components"]
