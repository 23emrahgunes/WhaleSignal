from pathlib import Path

from p25_smc import MicroBar, analyze_smc_bars
from p25_smc_patch import (
    SMC_MIN_ALIGNED_SCORE,
    SMC_MIN_CONFIRMATIONS,
    SMC_SOURCE,
    SMC_STRATEGY,
    _smc_rejection,
)


def _bar(i, o, h, l, c):
    start = i * 5000
    return MicroBar(start, start + 5000, float(o), float(h), float(l), float(c))


def test_bullish_structure_plus_fvg_is_detected_as_two_confirmations():
    bars = [
        _bar(0, 100.10, 100.20, 100.00, 100.05),
        _bar(1, 100.05, 100.15, 99.95, 100.00),
        _bar(2, 100.00, 100.10, 99.90, 99.95),
        _bar(3, 99.95, 100.05, 99.85, 99.90),
        _bar(4, 99.90, 100.00, 99.80, 99.85),
        _bar(5, 99.85, 99.95, 99.75, 99.80),
        _bar(6, 99.80, 99.90, 99.70, 99.75),
        _bar(7, 99.75, 99.85, 99.65, 99.70),
        _bar(8, 99.70, 99.80, 99.60, 99.65),
        _bar(9, 99.65, 99.75, 99.55, 99.60),
        _bar(10, 99.60, 101.20, 99.58, 101.10),
        _bar(11, 101.05, 101.50, 100.30, 101.40),
    ]
    state = analyze_smc_bars(bars, now_ms=60_000)
    assert state.ready is True
    assert state.structure.sign == 1
    assert state.structure.kind in {"BULL_CHOCH", "BULL_BOS"}
    assert state.fvg.sign == 1
    assert state.confirmations_up >= 2
    assert state.score > 0


def test_bullish_sell_side_sweep_reclaim_is_detected():
    bars = [
        _bar(0, 100.20, 100.30, 100.00, 100.15),
        _bar(1, 100.15, 100.25, 100.00, 100.10),
        _bar(2, 100.10, 100.20, 100.00, 100.05),
        _bar(3, 100.05, 100.15, 100.00, 100.08),
        _bar(4, 100.08, 100.18, 100.00, 100.10),
        _bar(5, 100.10, 100.20, 100.00, 100.12),
        _bar(6, 100.12, 100.22, 100.02, 100.15),
        _bar(7, 100.15, 100.25, 100.05, 100.18),
        _bar(8, 100.18, 100.28, 100.08, 100.20),
        _bar(9, 100.20, 100.30, 100.10, 100.22),
        _bar(10, 100.22, 100.25, 99.45, 100.18),
        _bar(11, 100.18, 100.30, 100.12, 100.25),
    ]
    state = analyze_smc_bars(bars, now_ms=60_000)
    assert state.ready is True
    assert state.sweep.sign == 1
    assert state.sweep.kind == "SELL_SIDE_SWEEP_RECLAIM"


def _smc(structure=1, sweep=0, fvg=1, score=0.50):
    return {
        "ready": True,
        "score": score,
        "liquidity_sweep": {"sign": sweep},
        "structure": {"sign": structure},
        "fvg": {"sign": fvg},
    }


def test_v3_gate_requires_structure_two_of_three_no_opposition_and_score():
    ok = _smc(structure=1, sweep=0, fvg=1, score=0.50)
    assert _smc_rejection("UP", ok) is None
    assert ok["selected_confirmations"] == SMC_MIN_CONFIRMATIONS
    assert ok["selected_aligned_score"] >= SMC_MIN_ALIGNED_SCORE

    assert "STRUCTURE" in _smc_rejection(
        "UP", _smc(structure=0, sweep=1, fvg=1, score=0.65)
    )
    assert "CONFIRMATIONS" in _smc_rejection(
        "UP", _smc(structure=1, sweep=0, fvg=0, score=0.50)
    )
    assert "OPPOSITION" in _smc_rejection(
        "UP", _smc(structure=1, sweep=-1, fvg=1, score=0.50)
    )
    assert "SMC_SCORE" in _smc_rejection(
        "UP", _smc(structure=1, sweep=0, fvg=1, score=0.30)
    )


def test_v3_contract_and_transactional_deploy_profile():
    assert SMC_STRATEGY == "INDEP_PTB_BINANCE_SMC_SELECTIVE_5M_V3"
    assert SMC_SOURCE == "INDEPENDENT_PTB_BINANCE_SMC_SELECTIVE_V3"
    text = Path("deploy_p25_smc_v3.sh").read_text(encoding="utf-8")
    assert "env -i PATH=\"$PATH\" HOME=\"$HOME\"" in text
    assert "PAPER_STRATEGY_VERSION': 'INDEP_PTB_BINANCE_SMC_SELECTIVE_5M_V3'" in text
    assert "PAPER_INDEPENDENT_DEADZONE_LOW': '0.28'" in text
    assert "PAPER_INDEPENDENT_DEADZONE_HIGH': '0.72'" in text
    assert "PAPER_STRICT_MIN_ABS_Z': '0.58'" in text
    assert "PAPER_STRICT_STABILITY_SEC': '5.0'" in text
    assert "PAPER_STRICT_MAX_FLIP_RATE': '0.55'" in text
    assert "PAPER_MIN_EDGE': '0.10'" in text
    assert "PAPER_DEEP_VALUE_MIN_VALUE_MULTIPLE': '1.15'" in text
    assert "PAPER_DEEP_VALUE_MAX_BOOK_AGE_MS': '600'" in text
    assert "SMC=STRUCTURE+2of3+score>=0.45" in text
    assert "DRY_REQUIRED+UNARMED" in text


def test_smc_entrypoint_selects_v3_without_changing_v2_entrypoint():
    text = Path("p25_main_smc.py").read_text(encoding="utf-8")
    assert "enable_smc_v3()" in text
    assert "p25_main._DIRECTIONAL_ALL5M_STRATEGY = SMC_STRATEGY" in text
    assert "p25_main.main()" in text
