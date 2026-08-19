"""P2.1 feature-only regression tests."""
from __future__ import annotations

import time

import pytest

from binance_feed import SymbolFeed
from config import Settings
from features import FeatureEngine, pct_return, price_at, realized_vol, windows_for
from models import Asset, AssetHorizon, Horizon, LocalBook, Trade


def _series(now_ms: int, seconds: int = 70, step_ms: int = 100) -> list[tuple[int, float]]:
    n = seconds * 1000 // step_ms
    start = now_ms - seconds * 1000
    return [(start + i * step_ms, 100.0 + i * 0.001) for i in range(n + 1)]


def test_price_at_never_uses_future_sample():
    prices = [(1000, 10.0), (1100, 11.0)]
    assert price_at(prices, 900) is None
    assert price_at(prices, 1050) == 10.0


def test_pct_return_requires_real_window_history():
    now = 10_000
    prices = [(now - 1000, 100.0), (now, 101.0)]
    assert pct_return(prices, 1000, now) == pytest.approx(0.01)
    assert pct_return(prices, 60_000, now) is None


def test_realized_vol_requires_window_anchor():
    now = 100_000
    short = _series(now, seconds=3)
    assert realized_vol(short, 60_000, now) is None
    long = _series(now, seconds=70)
    rv = realized_vol(long, 60_000, now)
    assert rv is not None and rv >= 0


def test_windows_include_subsecond_and_hourly_long_horizons():
    w5 = windows_for(Horizon.H5M)
    assert 100 in w5 and 250 in w5 and 180000 in w5
    w1h = windows_for(Horizon.H1H)
    for w in (300000, 600000, 900000, 1800000):
        assert w in w1h


def test_symbol_feed_retains_book_mid_feature_history():
    feed = SymbolFeed("BTCUSDT", 100, feature_ring_max=500)
    base = int(time.time() * 1000)
    feed.apply_snapshot({
        "lastUpdateId": 10,
        "bids": [["99", "2"]],
        "asks": [["101", "2"]],
    })
    event_ts = base + 100
    feed.on_depth({
        "E": event_ts,
        "U": 11,
        "u": 11,
        "b": [["100", "2"]],
        "a": [],
    })
    assert feed.feature_prices
    assert feed.feature_prices[-1][0] == event_ts
    assert feed.feature_prices[-1][1] == pytest.approx(100.5)


def test_feature_engine_p21_full_core_features():
    combo = AssetHorizon(Asset.BTC, Horizon.H5M)
    fe = FeatureEngine(combo)
    now = 1_000.0
    now_ms = int(now * 1000)
    prices = _series(now_ms, seconds=70)
    trades = [
        Trade(price=100.0 + i * 0.01, qty=1.0, ts_ms=now_ms - i * 200, is_buyer_maker=(i % 4 == 0))
        for i in range(100)
    ]
    book = LocalBook(
        "BTCUSDT",
        bids={100.0: 10.0, 99.9: 8.0},
        asks={100.1: 3.0, 100.2: 2.0},
        synced=True,
    )
    fv = fe.update(
        prices, trades, book, 100.0, 0.60, 0.40, 120.0, now,
        up_bid=0.59, up_ask=0.61, down_bid=0.39, down_ask=0.41,
        clob_up_obi=0.2, clob_down_obi=-0.1,
    )
    assert fv.has_reference and fv.has_clob
    assert fv.ret_multi["100"] is not None
    assert fv.ret_multi["60000"] is not None
    assert fv.flow_multi["5000"]["trade_count"] > 0
    assert fv.rv_multi["60000"] is not None
    assert fv.obi_20 > 0
    assert fv.up_spread == pytest.approx(0.02)
    assert fv.down_spread == pytest.approx(0.02)
    assert fv.clob_complement_residual == pytest.approx(0.0)
    assert fv.tte_fraction == pytest.approx(0.4)
    assert 0.0 <= fv.feature_coverage <= 1.0
    dumped = fv.to_dict()
    assert "ret_multi" in dumped and "momentum_multi" in dumped and "flow_multi" in dumped


def test_feature_engine_does_not_mark_short_history_ready():
    combo = AssetHorizon(Asset.ETH, Horizon.H15M)
    fe = FeatureEngine(combo)
    now = 1000.0
    now_ms = int(now * 1000)
    prices = _series(now_ms, seconds=8)
    book = LocalBook("ETHUSDT", bids={99.0: 1.0}, asks={101.0: 1.0}, synced=True)
    fv = fe.update(prices, [], book, 100.0, 0.5, 0.5, 800.0, now)
    assert fv.ret_multi["60000"] is None
    assert not fv.feature_ready
    assert "ret_60s" in fv.missing_features


def test_p21_phase_hard_locks_training(monkeypatch):
    monkeypatch.setenv("PHASE", "P2.1")
    monkeypatch.setenv("MODEL_TRAINING_ENABLED", "false")
    monkeypatch.setenv("CALIBRATION_ENABLED", "false")
    s = Settings()
    s.enforce_phase_lock()
    assert s.feature_only_phase
    assert not s.training_active
    assert not s.calibration_active
    assert not s.model_inference_active


def test_p21_phase_rejects_training(monkeypatch):
    monkeypatch.setenv("PHASE", "P2.1")
    monkeypatch.setenv("MODEL_TRAINING_ENABLED", "true")
    s = Settings()
    with pytest.raises(SystemExit):
        s.enforce_phase_lock()
