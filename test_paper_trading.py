"""Regression coverage for P2.5 paper trading and scorecards."""
from __future__ import annotations

import time

import pytest

from models import (
    Asset,
    AssetHorizon,
    Decision,
    FeatureSnapshot,
    Horizon,
    MarketRef,
    ResolutionType,
)
from p25_paper import PaperPolicy, evaluate_paper_entry, settle_paper_trade
from p25_paper_config import PaperSettings
from p25_paper_recorder import P25PaperRecorder
from p25_web import _HTML


BTC5 = AssetHorizon(Asset.BTC, Horizon.H5M)
ETH15 = AssetHorizon(Asset.ETH, Horizon.H15M)


def _settings(monkeypatch, **overrides) -> PaperSettings:
    values = {
        "PHASE": "P2.5",
        "MODEL_TRAINING_ENABLED": "false",
        "CALIBRATION_ENABLED": "false",
        "FORECAST_RECORDING_ENABLED": "true",
        "PAPER_TRADING_ENABLED": "true",
        "PAPER_STRATEGY_VERSION": "TEST_PAPER_V1",
        "PAPER_STARTING_BANKROLL_USDC": "100",
        "PAPER_STAKE_USDC": "2.50",
        "PAPER_ENTRY_CHECKPOINT_5M": "60",
        "PAPER_ENTRY_CHECKPOINT_15M": "240",
        "PAPER_ENTRY_CHECKPOINT_1H": "600",
        "PAPER_MIN_CONFIDENCE": "0.05",
        "PAPER_MIN_AGREEMENT": "0.50",
        "PAPER_MIN_EDGE": "0.00",
        "PAPER_MIN_PRICE": "0.05",
        "PAPER_MAX_PRICE": "0.95",
        "PAPER_SLIPPAGE": "0.005",
        "PAPER_FEE_BPS": "0",
        "PAPER_ALLOWED_STATUSES": "PROVISIONAL,VALIDATED",
        "PAPER_ALLOWED_GRADES": "LOW,MEDIUM,HIGH",
        "PAPER_RECENT_LIMIT": "20",
    }
    values.update({key: str(value) for key, value in overrides.items()})
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    cfg = PaperSettings()
    cfg.enforce_phase_lock()
    return cfg


def _ref(combo: AssetHorizon, condition: str) -> MarketRef:
    duration = float(combo.horizon.seconds)
    start = time.time() - duration + 30.0
    return MarketRef(
        combo=combo,
        condition_id=condition,
        slug=f"{combo.asset.value.lower()}-paper-{combo.horizon.value}-{int(start)}",
        question=f"{combo.asset.value} Up or Down",
        up_token_id=f"{condition}-up",
        down_token_id=f"{condition}-down",
        start_ts=start,
        end_ts=start + duration,
        market_start_ts=start,
        market_end_ts=start + duration,
        resolution_source="test resolution",
        resolution_type=(
            ResolutionType.BINANCE_1H_CANDLE
            if combo.horizon == Horizon.H1H
            else ResolutionType.CHAINLINK_TWAP
        ),
        official_reference_open=100.0,
        official_reference_open_time=start,
        official_reference_source="TEST_REFERENCE",
    )


def _snap(
    combo: AssetHorizon,
    *,
    up_bid: float = 0.58,
    up_ask: float = 0.60,
    down_bid: float = 0.38,
    down_ask: float = 0.40,
    tte: float = 60.0,
) -> FeatureSnapshot:
    return FeatureSnapshot(
        combo=combo,
        ts=time.time(),
        seconds_remaining=tte,
        tte_sec=tte,
        up_bid=up_bid,
        up_ask=up_ask,
        up_mid=(up_bid + up_ask) / 2.0,
        down_bid=down_bid,
        down_ask=down_ask,
        down_mid=(down_bid + down_ask) / 2.0,
        quality_status="OK",
    )


def _trace(
    *,
    direction: str = "UP",
    p_up: float = 0.70,
    confidence: float = 0.25,
    agreement: float = 0.80,
    status: str = "PROVISIONAL",
    grade: str = "LOW",
) -> dict:
    return {
        "phase": "P2.5",
        "model_version": "MODEL_B2_LOGISTIC_V1",
        "model_source": "MODEL_B2_FULL:shared",
        "feature_ready": True,
        "feature_coverage": 1.0,
        "predictability": 0.75,
        "conflict_score": 0.10,
        "directional_consensus": 0.80,
        "regime": "TREND_UP" if direction == "UP" else "TREND_DOWN",
        "p_up_raw": p_up,
        "p_up_calibrated": p_up,
        "p_up_ptb": p_up,
        "p_up_ptb_heuristic": p_up,
        "p_up_external": p_up,
        "p_up_market": p_up,
        "confidence": 0.0,
        "decision": "ABSTAIN",
        "abstain_reason": "MODEL_UNVALIDATED",
        "threshold": 0.62,
        "threshold_source": "DEFAULT_INSUFFICIENT",
        "calibration_source": "RAW_INSUFFICIENT_BIN",
        "forecast_direction": direction,
        "forecast_p_up": p_up,
        "forecast_confidence": confidence,
        "forecast_grade": grade,
        "forecast_status": status,
        "forecast_source": "ROBUST_ENSEMBLE_V1",
        "forecast_agreement": agreement,
        "forecast_model_maturity": 0.25,
    }


def test_noncanonical_checkpoint_does_not_create_attempt(monkeypatch):
    policy = PaperPolicy.from_settings(_settings(monkeypatch))
    result = evaluate_paper_entry(
        ref=_ref(BTC5, "0xwrongcp"),
        snap=_snap(BTC5),
        checkpoint=30,
        trace=_trace(),
        policy=policy,
        available_bankroll_usdc=100.0,
    )
    assert result is None


def test_down_entry_uses_real_down_ask_plus_slippage(monkeypatch):
    policy = PaperPolicy.from_settings(_settings(monkeypatch))
    result = evaluate_paper_entry(
        ref=_ref(BTC5, "0xdown"),
        snap=_snap(BTC5, down_bid=0.58, down_ask=0.60),
        checkpoint=60,
        trace=_trace(direction="DOWN", p_up=0.30),
        policy=policy,
        available_bankroll_usdc=100.0,
    )
    assert result is not None and result.eligible
    assert result.side == "DOWN"
    assert result.entry_ask == pytest.approx(0.60)
    assert result.fill_price == pytest.approx(0.605)
    assert result.selected_probability == pytest.approx(0.70)
    assert result.forecast_edge == pytest.approx(0.095)
    assert result.shares == pytest.approx(2.50 / 0.605)


def test_conflicted_and_negative_edge_forecasts_are_skipped(monkeypatch):
    policy = PaperPolicy.from_settings(_settings(monkeypatch))
    conflicted = evaluate_paper_entry(
        ref=_ref(BTC5, "0xconflict"),
        snap=_snap(BTC5),
        checkpoint=60,
        trace=_trace(status="CONFLICTED"),
        policy=policy,
        available_bankroll_usdc=100.0,
    )
    assert conflicted is not None and not conflicted.eligible
    assert "STATUS_CONFLICTED" in conflicted.reason

    negative_edge = evaluate_paper_entry(
        ref=_ref(BTC5, "0xedge"),
        snap=_snap(BTC5, up_bid=0.78, up_ask=0.80),
        checkpoint=60,
        trace=_trace(direction="UP", p_up=0.70),
        policy=policy,
        available_bankroll_usdc=100.0,
    )
    assert negative_edge is not None and not negative_edge.eligible
    assert negative_edge.reason == "EDGE_BELOW_MINIMUM"


def test_binary_paper_settlement_math():
    win = settle_paper_trade(
        side="UP",
        official_result="UP",
        shares=5.0,
        stake_usdc=2.5,
        fee_usdc=0.1,
    )
    assert win.correct
    assert win.gross_payout == pytest.approx(5.0)
    assert win.realized_pnl == pytest.approx(2.4)
    assert win.roi == pytest.approx(0.96)

    loss = settle_paper_trade(
        side="UP",
        official_result="DOWN",
        shares=5.0,
        stake_usdc=2.5,
        fee_usdc=0.1,
    )
    assert not loss.correct
    assert loss.gross_payout == 0
    assert loss.realized_pnl == pytest.approx(-2.6)


def test_recorder_opens_once_settles_and_builds_crypto_market_scorecards(
    monkeypatch,
    tmp_path,
):
    cfg = _settings(monkeypatch)
    recorder = P25PaperRecorder(str(tmp_path / "paper.sqlite"), cfg)
    try:
        btc = _ref(BTC5, "0xbtc")
        recorder.record_market(btc)
        btc_snap = _snap(BTC5, up_bid=0.58, up_ask=0.60, tte=60)
        assert recorder.record_forecast(btc, btc_snap, 60, _trace())
        assert recorder.record_forecast(btc, btc_snap, 30, _trace())
        assert recorder.stats()["paper_open"] == 1
        assert recorder.stats()["paper_attempts"] == 1

        btc.resolved = True
        btc.official_result = Decision.UP
        btc.resolved_outcome = Decision.UP
        btc.official_result_source = "test"
        btc.computed_result = Decision.UP
        btc.computed_result_source = "test-audit"
        recorder.settle(btc)

        eth = _ref(ETH15, "0xeth")
        recorder.record_market(eth)
        eth_snap = _snap(
            ETH15,
            up_bid=0.38,
            up_ask=0.40,
            down_bid=0.58,
            down_ask=0.60,
            tte=240,
        )
        assert recorder.record_forecast(
            eth,
            eth_snap,
            240,
            _trace(direction="DOWN", p_up=0.30),
        )
        eth.resolved = True
        eth.official_result = Decision.UP
        eth.resolved_outcome = Decision.UP
        eth.official_result_source = "test"
        eth.computed_result = Decision.UP
        eth.computed_result_source = "test-audit"
        recorder.settle(eth)

        skipped = _ref(BTC5, "0xskip")
        recorder.record_market(skipped)
        assert recorder.record_forecast(
            skipped,
            _snap(BTC5, tte=60),
            60,
            _trace(confidence=0.01),
        )

        analytics = recorder.paper_analytics()
        overall = analytics["overall"]
        assert overall["attempts"] == 3
        assert overall["trades"] == 2
        assert overall["settled"] == 2
        assert overall["skipped"] == 1
        assert overall["wins"] == 1
        assert overall["losses"] == 1
        assert overall["hit_rate"] == pytest.approx(0.5)
        assert analytics["per_asset"]["BTC"]["wins"] == 1
        assert analytics["per_asset"]["ETH"]["losses"] == 1
        assert analytics["per_combo"]["BTC:5m"]["attempts"] == 2
        assert analytics["per_combo"]["ETH:15m"]["settled"] == 1
        assert analytics["skip_reasons"]["LOW_CONFIDENCE"] == 1
        assert len(analytics["recent_markets"]) == 3

        forecast = recorder.forecast_analytics(min_n=1)
        assert "BTC" in forecast["per_asset"]
        assert "ETH" in forecast["per_asset"]
        assert "5m" in forecast["per_horizon"]
        assert "15m" in forecast["per_horizon"]
    finally:
        recorder.close()


def test_paper_config_and_dashboard_are_explicitly_non_execution(monkeypatch):
    cfg = _settings(monkeypatch)
    fields = set(cfg.model_fields)
    assert "paper_trading_enabled" in fields
    assert "private_key" not in fields
    assert "order_submit" not in fields
    assert "Paper Trade — Genel Durum" in _HTML
    assert "Kripto Bazlı Sonuç" in _HTML
    assert "Market Bazlı Paper İşlemler" in _HTML
    assert "paper_order_submissions" not in _HTML
