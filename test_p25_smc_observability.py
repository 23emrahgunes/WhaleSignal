from pathlib import Path
from types import SimpleNamespace

from p25_smc import MicroBar, analyze_smc_bars
from p25_smc_patch import (
    _record_gate_card,
    _reset_smc_gate_telemetry_for_tests,
    smc_gate_telemetry,
)


def _bar(index, open_, high, low, close):
    start = index * 5_000
    return MicroBar(
        start_ms=start,
        end_ms=start + 5_000,
        open=float(open_),
        high=float(high),
        low=float(low),
        close=float(close),
    )


def test_liquid_market_retained_displacement_counts_as_fvg():
    bars = [
        _bar(0, 100.00, 100.10, 99.90, 100.02),
        _bar(1, 100.02, 100.12, 99.92, 100.00),
        _bar(2, 100.00, 100.10, 99.90, 99.98),
        _bar(3, 99.98, 100.08, 99.88, 100.00),
        _bar(4, 100.00, 100.10, 99.90, 100.02),
        _bar(5, 100.02, 100.12, 99.92, 100.00),
        _bar(6, 100.00, 100.10, 99.90, 100.01),
        _bar(7, 100.01, 100.11, 99.91, 100.00),
        _bar(8, 100.00, 100.10, 99.90, 100.02),
        # Two-bars-back candle has a high above the following candle's low, so
        # there is no literal wick gap.
        _bar(9, 100.02, 100.60, 99.92, 100.05),
        _bar(10, 100.00, 101.20, 99.90, 101.10),
        _bar(11, 101.05, 101.30, 100.40, 101.00),
    ]

    state = analyze_smc_bars(bars, now_ms=60_000)

    assert state.ready is True
    assert state.fvg.sign == 1
    assert state.fvg.kind == "BULL_FVG_RETAINED_DISPLACEMENT"
    assert state.fvg.strength >= 0.70


def test_gate_telemetry_deduplicates_dynamic_reason_per_condition():
    _reset_smc_gate_telemetry_for_tests()
    ref = SimpleNamespace(
        condition_id="condition-1",
        market_id="market-1",
        combo=SimpleNamespace(key="BTC:5m"),
    )
    alpha = {
        "direction": "UP",
        "p_up": 0.81,
        "z_terminal": 0.88,
        "smc": {
            "ready": True,
            "score": 0.51,
            "structure": {"kind": "BULL_BOS", "sign": 1},
            "liquidity_sweep": {"kind": "NONE", "sign": 0},
            "fvg": {"kind": "BULL_FVG_RETAINED_DISPLACEMENT", "sign": 1},
        },
    }
    card = {
        "combo": "BTC:5m",
        "tte_sec": 181.4,
        "paper_deep_value_watch_reason": "WAITING_FOR_ENTRY_WINDOW_TTE_181.4",
        "independent_alpha": alpha,
        "paper_trade": None,
    }

    _record_gate_card(ref, card)
    card["paper_deep_value_watch_reason"] = "WAITING_FOR_ENTRY_WINDOW_TTE_180.9"
    _record_gate_card(ref, card)

    payload = smc_gate_telemetry()
    assert payload["conditions_seen"] == 1
    assert payload["reason_counts_unique_condition_bucket"] == {
        "WAITING_FOR_ENTRY_WINDOW": 1
    }

    card["paper_trade"] = {"status": "OPEN"}
    _record_gate_card(ref, card)
    payload = smc_gate_telemetry()
    assert payload["paper_entry_conditions"] == 1
    assert payload["reason_counts_unique_condition_bucket"]["PAPER_ENTRY"] == 1


def test_smc_v3_uses_fast_operational_state_snapshot():
    text = Path("p25_smc_patch.py").read_text(encoding="utf-8")
    assert "SMC_V3_OPERATIONAL_STATE_FAST_PATH" in text
    assert "core_engine.P25Engine.snapshot = fast_core_snapshot" in text
    assert 'data["smc_v3_telemetry"] = smc_gate_telemetry()' in text
