"""Regression tests for the 5m deep-value entry timing contract."""
from types import SimpleNamespace

from p25_deep_value_engine import _entry_window_reason


class _Horizon:
    value = "5m"


class _Combo:
    horizon = _Horizon()


class _Ref:
    combo = _Combo()


def _cfg():
    return SimpleNamespace(
        paper_deep_value_entry_tte_min_sec=60.0,
        paper_deep_value_entry_tte_max_sec=90.0,
    )


def _snap(tte):
    return SimpleNamespace(tte_sec=tte, seconds_remaining=tte)


def test_too_early_is_blocked_until_t90():
    reason = _entry_window_reason(_cfg(), _Ref(), _snap(120.0))
    assert reason == "WAITING_FOR_ENTRY_WINDOW_TTE_120.0"


def test_t90_boundary_is_allowed():
    assert _entry_window_reason(_cfg(), _Ref(), _snap(90.0)) is None


def test_middle_of_entry_window_is_allowed():
    assert _entry_window_reason(_cfg(), _Ref(), _snap(75.0)) is None


def test_t60_boundary_is_allowed():
    assert _entry_window_reason(_cfg(), _Ref(), _snap(60.0)) is None


def test_after_t60_is_closed():
    reason = _entry_window_reason(_cfg(), _Ref(), _snap(59.9))
    assert reason == "ENTRY_WINDOW_CLOSED_TTE_59.9"


def test_missing_tte_fails_closed():
    snap = SimpleNamespace(tte_sec=None, seconds_remaining=None)
    assert _entry_window_reason(_cfg(), _Ref(), snap) == "ENTRY_TTE_MISSING"
