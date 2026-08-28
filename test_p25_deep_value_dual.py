"""Regression tests for dual-side DEEP_VALUE_WATCH selection."""
from __future__ import annotations

import pytest

from p25_deep_value_recorder import P25DeepValuePaperRecorder
from test_p25_deep_value import _ref, _seed_book, _settings, _snap, _trace


def test_opposite_side_can_open_when_forecast_label_points_other_way(monkeypatch, tmp_path):
    """A cheap DOWN with model value must not be discarded just because label is UP."""
    p26 = tmp_path / "p26.sqlite"
    condition = "0xdual-opposite"
    _seed_book(p26, condition, side="DOWN", ask=0.18, ask_size=20.0)
    cfg = _settings(
        monkeypatch,
        p26,
        PAPER_STRATEGY_VERSION="DEEP_VALUE_25C_5M_DUAL_TEST",
        PAPER_DEEP_VALUE_MAX_ASK="0.25",
        PAPER_DEEP_VALUE_HORIZONS="5m",
    )
    recorder = P25DeepValuePaperRecorder(str(tmp_path / "p25.sqlite"), cfg)
    try:
        trace = _trace(direction="UP", p_up=0.63)
        opened = recorder.record_deep_value_watch(
            _ref(condition),
            _snap(up_ask=0.82, down_ask=0.18),
            trace,
        )
        assert opened is True
        row = recorder.paper_trade_for_condition(condition)
        assert row is not None
        assert row["side"] == "DOWN"
        assert row["entry_ask"] == pytest.approx(0.18)
        assert row["fill_price"] == pytest.approx(0.185)
        assert row["selected_probability"] == pytest.approx(0.37)
        assert row["value_multiple"] == pytest.approx(0.37 / 0.185)
    finally:
        recorder.close()


def test_when_both_sides_qualify_highest_value_multiple_wins(monkeypatch, tmp_path):
    p26 = tmp_path / "p26.sqlite"
    condition = "0xdual-best"
    _seed_book(p26, condition, side="UP", ask=0.20, ask_size=20.0)
    _seed_book(p26, condition, side="DOWN", ask=0.20, ask_size=20.0)
    cfg = _settings(
        monkeypatch,
        p26,
        PAPER_STRATEGY_VERSION="DEEP_VALUE_25C_5M_DUAL_TEST",
        PAPER_DEEP_VALUE_MAX_ASK="0.25",
        PAPER_DEEP_VALUE_HORIZONS="5m",
    )
    recorder = P25DeepValuePaperRecorder(str(tmp_path / "p25.sqlite"), cfg)
    try:
        opened = recorder.record_deep_value_watch(
            _ref(condition),
            _snap(up_ask=0.20, down_ask=0.20),
            _trace(direction="DOWN", p_up=0.55),
        )
        assert opened is True
        row = recorder.paper_trade_for_condition(condition)
        assert row is not None
        assert row["side"] == "UP"
        assert row["selected_probability"] == pytest.approx(0.55)
        assert row["value_multiple"] > (0.45 / 0.205)
    finally:
        recorder.close()


def test_cheap_opposite_side_still_needs_value_gate(monkeypatch, tmp_path):
    p26 = tmp_path / "p26.sqlite"
    condition = "0xdual-no-value"
    _seed_book(p26, condition, side="DOWN", ask=0.20, ask_size=20.0)
    cfg = _settings(
        monkeypatch,
        p26,
        PAPER_STRATEGY_VERSION="DEEP_VALUE_25C_5M_DUAL_TEST",
        PAPER_DEEP_VALUE_MAX_ASK="0.25",
        PAPER_DEEP_VALUE_HORIZONS="5m",
    )
    recorder = P25DeepValuePaperRecorder(str(tmp_path / "p25.sqlite"), cfg)
    try:
        trace = _trace(direction="UP", p_up=0.90)
        opened = recorder.record_deep_value_watch(
            _ref(condition),
            _snap(up_ask=0.80, down_ask=0.20),
            trace,
        )
        assert opened is False
        assert recorder.paper_trade_for_condition(condition) is None
        assert trace["paper_deep_value_watch_reason"] == "VALUE_MULTIPLE_BELOW_MINIMUM"
    finally:
        recorder.close()


def test_exact_25c_side_is_allowed_and_fills_at_25_5c(monkeypatch, tmp_path):
    p26 = tmp_path / "p26.sqlite"
    condition = "0xdual-25c"
    _seed_book(p26, condition, side="DOWN", ask=0.25, ask_size=10.0)
    cfg = _settings(
        monkeypatch,
        p26,
        PAPER_STRATEGY_VERSION="DEEP_VALUE_25C_5M_DUAL_TEST",
        PAPER_DEEP_VALUE_MAX_ASK="0.25",
        PAPER_DEEP_VALUE_HORIZONS="5m",
    )
    recorder = P25DeepValuePaperRecorder(str(tmp_path / "p25.sqlite"), cfg)
    try:
        # P(DOWN)=0.40 => 0.40 / 0.255 = 1.568x, so it passes the 1.50x gate.
        opened = recorder.record_deep_value_watch(
            _ref(condition),
            _snap(up_ask=0.75, down_ask=0.25),
            _trace(direction="UP", p_up=0.60),
        )
        assert opened is True
        row = recorder.paper_trade_for_condition(condition)
        assert row is not None
        assert row["side"] == "DOWN"
        assert row["entry_ask"] == pytest.approx(0.25)
        assert row["fill_price"] == pytest.approx(0.255)
        assert row["price_band"] == "15-25c"
    finally:
        recorder.close()
