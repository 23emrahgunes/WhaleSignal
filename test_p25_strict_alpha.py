"""Deterministic STRICT V1 alpha-gate tests."""
from __future__ import annotations

from types import SimpleNamespace

from p25_independent_alpha import build_independent_alpha


class _Value:
    def __init__(self, value):
        self.value = value


class _Combo:
    horizon = _Value("5m")


def _cfg(**overrides):
    values = dict(
        max_reference_age_ms=8000.0,
        max_spot_age_ms=2500.0,
        paper_independent_deadzone_low=0.33,
        paper_independent_deadzone_high=0.67,
        paper_independent_binance_max_sigma_shift=0.35,
        paper_independent_max_basis_bps=50.0,
        paper_independent_max_basis_open_gap_ms=5000.0,
        paper_strict_entry_enabled=True,
        paper_strict_require_official_current=True,
        paper_strict_require_ptb_side_alignment=True,
        paper_strict_min_abs_z=0.45,
        paper_strict_max_counter_sigma=0.10,
        paper_strict_max_vol_percentile=0.92,
        paper_strict_max_flip_rate=0.55,
        paper_strict_max_vol_accel=1.80,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _ref(**overrides):
    values = dict(
        combo=_Combo(),
        official_reference_open=100.0,
        official_reference_open_time=1000.0,
        proxy_reference_open=100.0,
        proxy_reference_open_time=1000.0,
        reference_current=100.15,
        reference_current_age_ms=100.0,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _snap(**overrides):
    values = dict(
        tte_sec=70.0,
        seconds_remaining=70.0,
        spot_price=100.15,
        spot_age_ms=100.0,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _fv(**overrides):
    values = dict(
        missing_features=[],
        rv_multi={"5000": 0.0007, "30000": 0.0017, "60000": 0.0024},
        ret_multi={"5000": 0.0, "15000": 0.0, "30000": 0.0},
        flow_fast=0.0,
        flow_mid=0.0,
        flow_slow=0.0,
        obi_20=0.0,
        ofi=0.0,
        vol_percentile=0.50,
        flip_rate=0.20,
        vol_accel=1.0,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_strict_good_official_ptb_setup_passes():
    result = build_independent_alpha(ref=_ref(), snap=_snap(), fv=_fv(), cfg=_cfg())
    assert result.ready
    assert result.source == "INDEPENDENT_PTB_BINANCE_STRICT_V1"
    assert result.anchor_source == "OFFICIAL_CURRENT"
    assert result.direction == "UP"
    assert result.p_up is not None and result.p_up >= 0.67
    assert result.z_terminal is not None and abs(result.z_terminal) >= 0.45
    assert result.distance_bps is not None and result.distance_bps > 0


def test_strict_requires_fresh_official_current():
    result = build_independent_alpha(
        ref=_ref(reference_current=None, reference_current_age_ms=None),
        snap=_snap(spot_price=100.30),
        fv=_fv(),
        cfg=_cfg(),
    )
    assert not result.ready
    assert result.reason == "STRICT_OFFICIAL_CURRENT_REQUIRED"


def test_strict_rejects_high_vol_flip_and_acceleration():
    high_vol = build_independent_alpha(
        ref=_ref(), snap=_snap(), fv=_fv(vol_percentile=0.93), cfg=_cfg()
    )
    assert not high_vol.ready
    assert high_vol.reason == "STRICT_HIGH_VOL_PERCENTILE"

    flip = build_independent_alpha(
        ref=_ref(), snap=_snap(), fv=_fv(flip_rate=0.56), cfg=_cfg()
    )
    assert not flip.ready
    assert flip.reason == "STRICT_FLIP_RATE"

    accel = build_independent_alpha(
        ref=_ref(), snap=_snap(), fv=_fv(vol_accel=1.81), cfg=_cfg()
    )
    assert not accel.ready
    assert accel.reason == "STRICT_VOL_ACCEL"


def test_strict_rejects_large_counter_binance_pressure():
    negative = _fv(
        ret_multi={"5000": -0.0020, "15000": -0.0020, "30000": -0.0020},
        flow_fast=-1.0,
        flow_mid=-1.0,
        flow_slow=-1.0,
        obi_20=-1.0,
        ofi=-1.0,
    )
    result = build_independent_alpha(
        ref=_ref(reference_current=100.30),
        snap=_snap(),
        fv=negative,
        cfg=_cfg(),
    )
    assert result.direction == "UP"
    assert not result.ready
    assert result.counter_sigma is not None and result.counter_sigma > 0.10
    assert result.reason == "STRICT_COUNTER_PRESSURE"


def test_strict_probability_deadzone_is_wider():
    result = build_independent_alpha(
        ref=_ref(reference_current=100.02),
        snap=_snap(),
        fv=_fv(),
        cfg=_cfg(),
    )
    assert not result.ready
    assert result.direction == "NEUTRAL"
    assert result.reason == "DEADZONE_NEUTRAL"
