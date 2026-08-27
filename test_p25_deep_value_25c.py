"""Regression coverage for the 25c max-ask 5m deep-value cohort."""
from __future__ import annotations

import pytest

from p25_deep_value_recorder import P25DeepValuePaperRecorder
from test_p25_deep_value import _ref, _seed_book, _settings, _snap, _trace


def test_exact_twenty_five_cent_touch_is_allowed_with_slippage(monkeypatch, tmp_path):
    p26 = tmp_path / "p26.sqlite"
    condition = "0x25c"
    _seed_book(p26, condition, ask=0.25, ask_size=10.0)
    cfg = _settings(
        monkeypatch,
        p26,
        PAPER_DEEP_VALUE_MAX_ASK="0.25",
        PAPER_DEEP_VALUE_HORIZONS="5m",
        PAPER_STRATEGY_VERSION="DEEP_VALUE_25C_5M_V1",
    )
    recorder = P25DeepValuePaperRecorder(str(tmp_path / "p25.sqlite"), cfg)
    try:
        assert recorder.record_deep_value_watch(
            _ref(condition), _snap(up_ask=0.25), _trace(p_up=0.70)
        )
        row = recorder.paper_trade_for_condition(condition)
        assert row is not None
        assert row["entry_ask"] == pytest.approx(0.25)
        assert row["fill_price"] == pytest.approx(0.255)
        assert row["price_band"] == "15-25c"
        assert row["stake_usdc"] == pytest.approx(1.0)
    finally:
        recorder.close()


def test_above_twenty_five_cent_waits_without_consuming_attempt(monkeypatch, tmp_path):
    p26 = tmp_path / "p26.sqlite"
    condition = "0xabove25"
    _seed_book(p26, condition, ask=0.26, ask_size=10.0)
    cfg = _settings(
        monkeypatch,
        p26,
        PAPER_DEEP_VALUE_MAX_ASK="0.25",
        PAPER_DEEP_VALUE_HORIZONS="5m",
        PAPER_STRATEGY_VERSION="DEEP_VALUE_25C_5M_V1",
    )
    recorder = P25DeepValuePaperRecorder(str(tmp_path / "p25.sqlite"), cfg)
    try:
        trace = _trace(p_up=0.70)
        assert recorder.record_deep_value_watch(
            _ref(condition), _snap(up_ask=0.26), trace
        ) is False
        assert recorder.conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] == 0
        assert trace["paper_deep_value_watch_reason"] == "WAITING_FOR_DIP"
    finally:
        recorder.close()
