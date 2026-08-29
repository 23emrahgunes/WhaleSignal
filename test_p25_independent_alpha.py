"""Deterministic tests for the independent PTB + Binance paper alpha."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from p25_independent_alpha import build_independent_alpha


class _Value:
    def __init__(self, value):
        self.value = value


class _Combo:
    def __init__(self, horizon="5m"):
        self.horizon = _Value(horizon)


def _cfg(**overrides):
    values = dict(
        max_reference_age_ms=8000.0,
        max_spot_age_ms=2500.0,
        paper_independent_deadzone_low=0.42,
        paper_independent_deadzone_high=0.58,
        paper_independent_binance_max_sigma_shift=0.35,
        paper_independent_max_basis_bps=50.0,
        paper_independent_max_basis_open_gap_ms=5000.0,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _ref(**overrides):
    values = dict(
        combo=_Combo("5m"),
        official_reference_open=100.0,
        official_reference_open_time=1000.0,
        proxy_reference_open=100.0,
        proxy_reference_open_time=1000.0,
        reference_current=100.0,
        reference_current_age_ms=100.0,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _snap(**overrides):
    # up_mid/down_mid are intentionally present so tests can prove they do not affect alpha.
    values = dict(
        tte_sec=75.0,
        seconds_remaining=75.0,
        spot_price=100.0,
        spot_age_ms=100.0,
        up_mid=0.05,
        down_mid=0.95,
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
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_official_current_anchor_is_independent_from_polymarket_mid():
    first = build_independent_alpha(
        ref=_ref(reference_current=100.08),
        snap=_snap(up_mid=0.01, down_mid=0.99),
        fv=_fv(),
        cfg=_cfg(),
    )
    second = build_independent_alpha(
        ref=_ref(reference_current=100.08),
        snap=_snap(up_mid=0.99, down_mid=0.01),
        fv=_fv(),
        cfg=_cfg(),
    )
    assert first.ready and second.ready
    assert first.anchor_source == "OFFICIAL_CURRENT"
    assert first.p_up == pytest.approx(second.p_up)
    assert first.direction == second.direction


def test_clob_missing_does_not_block_independent_alpha():
    result = build_independent_alpha(
        ref=_ref(reference_current=100.08),
        snap=_snap(),
        fv=_fv(missing_features=["clob"]),
        cfg=_cfg(),
    )
    assert result.ready


def test_basis_adjusted_binance_is_used_only_when_official_current_missing():
    result = build_independent_alpha(
        ref=_ref(
            reference_current=None,
            reference_current_age_ms=None,
            official_reference_open=100.0,
            proxy_reference_open=100.20,
        ),
        snap=_snap(spot_price=100.40),
        fv=_fv(),
        cfg=_cfg(),
    )
    assert result.ready
    assert result.anchor_source == "BINANCE_BASIS_ADJUSTED"
    assert result.current_equivalent == pytest.approx(100.40 * (100.0 / 100.20))
    assert result.basis_bps is not None


def test_basis_fallback_fails_closed_when_opening_basis_is_too_large():
    result = build_independent_alpha(
        ref=_ref(
            reference_current=None,
            reference_current_age_ms=None,
            official_reference_open=100.0,
            proxy_reference_open=101.0,
        ),
        snap=_snap(spot_price=101.0),
        fv=_fv(),
        cfg=_cfg(paper_independent_max_basis_bps=20.0),
    )
    assert not result.ready
    assert result.reason == "BASIS_TOO_LARGE"


def test_zero_distance_and_zero_binance_pressure_is_neutral():
    result = build_independent_alpha(
        ref=_ref(reference_current=100.0),
        snap=_snap(),
        fv=_fv(),
        cfg=_cfg(),
    )
    assert result.ready
    assert result.p_up == pytest.approx(0.5, abs=1e-9)
    assert result.direction == "NEUTRAL"
    assert result.reason == "DEADZONE_NEUTRAL"


def test_binance_pressure_only_shifts_terminal_mean_within_bounded_sigma():
    positive = _fv(
        ret_multi={"5000": 0.0010, "15000": 0.0012, "30000": 0.0015},
        flow_fast=0.9,
        flow_mid=0.9,
        flow_slow=0.8,
        obi_20=0.8,
        ofi=0.8,
    )
    negative = _fv(
        ret_multi={"5000": -0.0010, "15000": -0.0012, "30000": -0.0015},
        flow_fast=-0.9,
        flow_mid=-0.9,
        flow_slow=-0.8,
        obi_20=-0.8,
        ofi=-0.8,
    )
    up = build_independent_alpha(ref=_ref(reference_current=100.0), snap=_snap(), fv=positive, cfg=_cfg())
    down = build_independent_alpha(ref=_ref(reference_current=100.0), snap=_snap(), fv=negative, cfg=_cfg())
    assert up.ready and down.ready
    assert up.direction == "UP"
    assert down.direction == "DOWN"
    assert up.p_up is not None and up.p_up > 0.58
    assert down.p_up is not None and down.p_up < 0.42
    assert up.sigma_remaining_bps is not None
    assert abs(up.binance_correction_bps or 0.0) <= 0.35 * up.sigma_remaining_bps + 1e-9


def test_non_5m_or_required_binance_feature_missing_fails_closed():
    wrong_horizon = build_independent_alpha(
        ref=_ref(combo=_Combo("15m")),
        snap=_snap(),
        fv=_fv(),
        cfg=_cfg(),
    )
    assert not wrong_horizon.ready
    assert wrong_horizon.reason == "HORIZON_NOT_5M"

    missing_binance = build_independent_alpha(
        ref=_ref(),
        snap=_snap(),
        fv=_fv(missing_features=["clob", "flow_5s"]),
        cfg=_cfg(),
    )
    assert not missing_binance.ready
    assert missing_binance.reason == "BINANCE_FEATURES_MISSING_FLOW_5S"
