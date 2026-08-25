"""Regression guard for P3 LIVE pilot: one real network cycle per arm."""
from p3_daemon import _NETWORK_CYCLE_TERMINAL_STATUSES, _halt_after_network_cycle


class _State:
    def __init__(self):
        self.reasons = []

    def halt(self, reason):
        self.reasons.append(str(reason))


def test_network_terminal_results_consume_arm():
    expected = {
        "MERGED_VERIFIED",
        "NO_FILL_VERIFIED",
        "ONE_LEG_UNWOUND_VERIFIED",
        "ONE_LEG_UNWOUND_VERIFIED_HALTED",
        "HALTED_RESIDUAL_EXPOSURE",
        "HALTED_MERGE_NOT_VERIFIED",
        "HALTED_EXCEPTION",
    }
    assert expected == _NETWORK_CYCLE_TERMINAL_STATUSES

    for status in sorted(expected):
        state = _State()
        assert _halt_after_network_cycle(state, {"status": status}) is True
        assert state.reasons == [f"ONE_NETWORK_CYCLE_PER_ARM_COMPLETE:{status}"]


def test_pre_submit_skips_do_not_consume_arm():
    for status in (
        "NO_CONFIRMED_WINDOW",
        "SKIPPED_STALE_CONFIRMATION",
        "SKIPPED_FRESH_DEPTH_OR_MIN_SIZE",
        "SKIPPED_LIVE_EDGE_GATE",
        "SKIPPED_SINGLE_LEG_NOTIONAL_CAP",
        "SKIPPED_UNWIND_DEPTH",
        "SKIPPED_PROJECTED_UNWIND_LOSS",
        "SKIPPED_EDGE_TO_UNWIND_RISK",
        "SKIPPED_INSUFFICIENT_BALANCE",
        "ABORTED_DISARMED_BEFORE_SUBMIT",
    ):
        state = _State()
        assert _halt_after_network_cycle(state, {"status": status}) is False
        assert state.reasons == []
