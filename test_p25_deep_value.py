"""Regression tests for P2.5 DEEP_VALUE_WATCH paper research."""
from __future__ import annotations

import time

import pytest

from models import Asset, AssetHorizon, FeatureSnapshot, Horizon, MarketRef, ResolutionType
from p25_deep_value_config import DeepValuePaperSettings
from p25_deep_value_recorder import P25DeepValuePaperRecorder
from p26_book_store import BookSnapshotStore
from p26_execution import OrderBookSnapshot
from p26_fee import FeeScheduleStore


BTC5 = AssetHorizon(Asset.BTC, Horizon.H5M)


def _settings(monkeypatch, p26_path, **overrides):
    values = {
        "PHASE": "P2.5",
        "MODEL_TRAINING_ENABLED": "false",
        "CALIBRATION_ENABLED": "false",
        "FORECAST_RECORDING_ENABLED": "true",
        "PAPER_TRADING_ENABLED": "true",
        "PAPER_ENTRY_MODE": "DEEP_VALUE_WATCH",
        "PAPER_STRATEGY_VERSION": "DEEP_VALUE_10C_TEST",
        "PAPER_STARTING_BANKROLL_USDC": "100",
        "PAPER_STAKE_USDC": "1.00",
        "PAPER_MIN_CONFIDENCE": "0.05",
        "PAPER_MIN_AGREEMENT": "0.50",
        "PAPER_MIN_EDGE": "0.00",
        "PAPER_MIN_PRICE": "0.01",
        "PAPER_MAX_PRICE": "0.95",
        "PAPER_SLIPPAGE": "0.005",
        "PAPER_FEE_BPS": "0",
        "PAPER_ALLOWED_STATUSES": "PROVISIONAL,VALIDATED",
        "PAPER_ALLOWED_GRADES": "LOW,MEDIUM,HIGH",
        "PAPER_DEEP_VALUE_MIN_ASK": "0.01",
        "PAPER_DEEP_VALUE_MAX_ASK": "0.10",
        "PAPER_DEEP_VALUE_PREFILTER_BUFFER": "0.03",
        "PAPER_DEEP_VALUE_MIN_TTE_SEC": "5",
        "PAPER_DEEP_VALUE_P26_DB_PATH": str(p26_path),
        "PAPER_DEEP_VALUE_MAX_BOOK_AGE_MS": "5000",
        "PAPER_DEEP_VALUE_REQUIRE_DEPTH": "true",
        "PAPER_DEEP_VALUE_REQUIRE_FEE_SCHEDULE": "true",
        "PAPER_DEEP_VALUE_MIN_VALUE_MULTIPLE": "1.5",
    }
    values.update({key: str(value) for key, value in overrides.items()})
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    cfg = DeepValuePaperSettings()
    cfg.enforce_phase_lock()
    return cfg


def _ref(condition: str) -> MarketRef:
    duration = float(BTC5.horizon.seconds)
    start = time.time() - 60.0
    return MarketRef(
        combo=BTC5,
        condition_id=condition,
        market_id=f"market-{condition}",
        slug=f"btc-updown-5m-{condition}",
        question="BTC Up or Down",
        up_token_id=f"{condition}-up",
        down_token_id=f"{condition}-down",
        start_ts=start,
        end_ts=start + duration,
        market_start_ts=start,
        market_end_ts=start + duration,
        resolution_source="test",
        resolution_type=ResolutionType.CHAINLINK_TWAP,
        official_reference_open=100.0,
        official_reference_open_time=start,
        official_reference_source="TEST",
    )


def _snap(*, up_ask=0.05, down_ask=0.95, tte=120.0):
    return FeatureSnapshot(
        combo=BTC5,
        ts=time.time(),
        seconds_remaining=tte,
        tte_sec=tte,
        up_bid=max(0.001, up_ask - 0.01),
        up_ask=up_ask,
        up_mid=max(0.001, up_ask - 0.005),
        down_bid=max(0.001, down_ask - 0.01),
        down_ask=down_ask,
        down_mid=max(0.001, down_ask - 0.005),
        quality_status="OK",
    )


def _trace(*, direction="UP", p_up=0.70):
    return {
        "feature_ready": True,
        "forecast_direction": direction,
        "forecast_p_up": p_up,
        "forecast_confidence": 0.30,
        "forecast_grade": "HIGH",
        "forecast_status": "VALIDATED",
        "forecast_agreement": 0.80,
    }


def _seed_book(p26_path, condition, *, side="UP", ask=0.05, ask_size=25.0):
    token = f"{condition}-{side.lower()}"
    other = f"{condition}-{'down' if side == 'UP' else 'up'}"
    now_ms = int(time.time() * 1000)
    store = BookSnapshotStore(str(p26_path))
    try:
        snapshot = OrderBookSnapshot.from_levels(
            token_id=token,
            ts_ms=now_ms,
            bids=[(max(0.001, ask - 0.01), 100.0)],
            asks=[(ask, ask_size)],
        )
        store.insert(
            condition_id=condition,
            combo_key="BTC:5m",
            side=side,
            snapshot=snapshot,
            recv_ts_ms=now_ms,
        )
    finally:
        store.close()

    fees = FeeScheduleStore(str(p26_path))
    try:
        fees.upsert_market_info(
            condition_id=condition,
            combo_key="BTC:5m",
            market_end_ts_ms=now_ms + 120_000,
            payload={
                "t": [
                    {"t": token if side == "UP" else other, "o": "UP"},
                    {"t": other if side == "UP" else token, "o": "DOWN"},
                ],
            },
            source_ts_ms=now_ms,
            source="TEST_FEE",
        )
    finally:
        fees.close()


def test_five_cent_touch_opens_one_dollar_when_full_depth_is_sufficient(monkeypatch, tmp_path):
    p26 = tmp_path / "p26.sqlite"
    condition = "0x5c"
    _seed_book(p26, condition, ask=0.05, ask_size=25.0)
    cfg = _settings(monkeypatch, p26)
    recorder = P25DeepValuePaperRecorder(str(tmp_path / "p25.sqlite"), cfg)
    try:
        ref = _ref(condition)
        assert recorder.record_deep_value_watch(ref, _snap(up_ask=0.05), _trace())
        row = recorder.paper_trade_for_condition(condition)
        assert row is not None
        assert row["status"] == "OPEN"
        assert row["entry_mode"] == "DEEP_VALUE_WATCH"
        assert row["price_band"] == "03-05c"
        assert row["stake_usdc"] == pytest.approx(1.0)
        assert row["entry_ask"] == pytest.approx(0.05)
        assert row["fill_price"] == pytest.approx(0.055)
        assert row["shares"] == pytest.approx(1.0 / 0.055)
        assert row["depth_capacity_shares"] == pytest.approx(25.0)
        assert row["depth_required_shares"] == pytest.approx(1.0 / 0.055)
        assert row["value_multiple"] > 10.0
        assert recorder.record_deep_value_watch(ref, _snap(up_ask=0.04), _trace()) is False
        assert recorder.conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] == 1
    finally:
        recorder.close()


def test_exact_ten_cent_touch_is_allowed_even_with_slippage(monkeypatch, tmp_path):
    p26 = tmp_path / "p26.sqlite"
    condition = "0x10c"
    _seed_book(p26, condition, ask=0.10, ask_size=12.0)
    cfg = _settings(monkeypatch, p26)
    recorder = P25DeepValuePaperRecorder(str(tmp_path / "p25.sqlite"), cfg)
    try:
        assert recorder.record_deep_value_watch(_ref(condition), _snap(up_ask=0.10), _trace())
        row = recorder.paper_trade_for_condition(condition)
        assert row is not None
        assert row["entry_ask"] == pytest.approx(0.10)
        assert row["fill_price"] == pytest.approx(0.105)
        assert row["price_band"] == "05-10c"
    finally:
        recorder.close()


def test_above_ten_cent_waits_without_consuming_one_shot(monkeypatch, tmp_path):
    p26 = tmp_path / "p26.sqlite"
    condition = "0xwait"
    _seed_book(p26, condition, ask=0.11, ask_size=100.0)
    cfg = _settings(monkeypatch, p26)
    recorder = P25DeepValuePaperRecorder(str(tmp_path / "p25.sqlite"), cfg)
    try:
        trace = _trace()
        assert recorder.record_deep_value_watch(_ref(condition), _snap(up_ask=0.11), trace) is False
        assert recorder.conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] == 0
        assert trace["paper_deep_value_watch_reason"] == "WAITING_FOR_DIP"
    finally:
        recorder.close()


def test_visible_best_ask_without_enough_one_dollar_depth_does_not_open(monkeypatch, tmp_path):
    p26 = tmp_path / "p26.sqlite"
    condition = "0xthin"
    _seed_book(p26, condition, ask=0.05, ask_size=5.0)
    cfg = _settings(monkeypatch, p26)
    recorder = P25DeepValuePaperRecorder(str(tmp_path / "p25.sqlite"), cfg)
    try:
        trace = _trace()
        assert recorder.record_deep_value_watch(_ref(condition), _snap(up_ask=0.05), trace) is False
        assert recorder.conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] == 0
        assert str(trace["paper_deep_value_watch_reason"]).startswith("DEPTH_INSUFFICIENT")
    finally:
        recorder.close()


def test_deep_mode_keeps_checkpoint_forecasts_but_checkpoint_does_not_consume_paper(monkeypatch, tmp_path):
    p26 = tmp_path / "p26.sqlite"
    cfg = _settings(monkeypatch, p26)
    recorder = P25DeepValuePaperRecorder(str(tmp_path / "p25.sqlite"), cfg)
    try:
        ref = _ref("0xforecast")
        recorder.record_market(ref)
        assert recorder.record_forecast(ref, _snap(up_ask=0.60, tte=60), 60, _trace())
        assert recorder.conn.execute("SELECT COUNT(*) FROM forecasts").fetchone()[0] == 1
        assert recorder.conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] == 0
    finally:
        recorder.close()
