"""STRICT V1 value-layer tests without SQLite fixtures."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import p25_strict_value as strict_value


def _policy():
    return SimpleNamespace(enabled=True, stake_usdc=1.0, slippage=0.005)


def _cfg(**overrides):
    values = dict(
        paper_deep_value_enabled=True,
        paper_deep_value_min_tte_sec=5.0,
        paper_deep_value_require_depth=True,
        paper_deep_value_min_depth_multiple=1.5,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _ref():
    return SimpleNamespace(condition_id="cond-1")


def _snap():
    return SimpleNamespace(tte_sec=70.0, seconds_remaining=70.0)


def _forecast(direction="UP", p_up=0.70):
    return {
        "forecast_direction": direction,
        "forecast_p_up": p_up,
        "feature_ready": True,
    }


def _depth(capacity=10.0, required=5.0):
    return SimpleNamespace(
        token_id="token-up",
        bid=0.19,
        ask=0.20,
        fill_price=0.205,
        capacity_shares=capacity,
        required_shares=required,
        age_ms=200,
        fee_usdc=0.0,
        fee_source="TEST",
        price_band="15-25c",
    )


def _candidate(side="UP", capacity=10.0, required=5.0):
    return SimpleNamespace(
        side=side,
        selected_probability=0.70 if side == "UP" else 0.30,
        depth=_depth(capacity, required),
        edge=0.495,
        value_multiple=3.41,
    )


def test_strict_value_only_evaluates_alpha_side(monkeypatch):
    seen = []

    monkeypatch.setattr(
        strict_value,
        "_forecast_gate",
        lambda trace, policy: (
            {"p_up": 0.70, "p_down": 0.30, "confidence": 0.40, "agreement": 1.0},
            "OK",
        ),
    )

    def fake_candidate(**kwargs):
        seen.append(kwargs["side"])
        return _candidate(side=kwargs["side"]), {"reason": "ELIGIBLE", "price_band": "15-25c"}

    monkeypatch.setattr(strict_value, "_candidate_for_side", fake_candidate)
    decision, diag = strict_value.evaluate_strict_value_watch(
        ref=_ref(),
        snap=_snap(),
        trace=_forecast("UP", 0.70),
        policy=_policy(),
        cfg=_cfg(),
        available_bankroll_usdc=100.0,
    )
    assert decision is not None
    assert decision.side == "UP"
    assert seen == ["UP"]
    assert diag["scan_mode"] == "DIRECTION_LOCKED_STRICT"


def test_strict_value_rejects_less_than_one_point_five_x_depth(monkeypatch):
    monkeypatch.setattr(
        strict_value,
        "_forecast_gate",
        lambda trace, policy: (
            {"p_up": 0.70, "p_down": 0.30, "confidence": 0.40, "agreement": 1.0},
            "OK",
        ),
    )
    monkeypatch.setattr(
        strict_value,
        "_candidate_for_side",
        lambda **kwargs: (
            _candidate(side=kwargs["side"], capacity=7.0, required=5.0),
            {"reason": "ELIGIBLE", "price_band": "15-25c"},
        ),
    )
    decision, diag = strict_value.evaluate_strict_value_watch(
        ref=_ref(),
        snap=_snap(),
        trace=_forecast("UP", 0.70),
        policy=_policy(),
        cfg=_cfg(),
        available_bankroll_usdc=100.0,
    )
    assert decision is None
    assert str(diag["reason"]).startswith("DEPTH_BUFFER_INSUFFICIENT")


def test_strict_value_accepts_exact_depth_buffer(monkeypatch):
    monkeypatch.setattr(
        strict_value,
        "_forecast_gate",
        lambda trace, policy: (
            {"p_up": 0.70, "p_down": 0.30, "confidence": 0.40, "agreement": 1.0},
            "OK",
        ),
    )
    monkeypatch.setattr(
        strict_value,
        "_candidate_for_side",
        lambda **kwargs: (
            _candidate(side=kwargs["side"], capacity=7.5, required=5.0),
            {"reason": "ELIGIBLE", "price_band": "15-25c"},
        ),
    )
    decision, diag = strict_value.evaluate_strict_value_watch(
        ref=_ref(),
        snap=_snap(),
        trace=_forecast("UP", 0.70),
        policy=_policy(),
        cfg=_cfg(),
        available_bankroll_usdc=100.0,
    )
    assert decision is not None
    assert decision.side == "UP"
    assert diag["depth_min_multiple"] == pytest.approx(1.5)
