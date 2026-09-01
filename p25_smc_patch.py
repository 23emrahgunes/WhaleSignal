"""Runtime integration for the SMC Selective V3 Direction Engine cohort.

Kept separate from the proven V2 modules so the V2 research/control cohort remains
unchanged.  The V3 entrypoint enables these patches before starting the normal P2.5
service.
"""
from __future__ import annotations

import time
from typing import Any

import features
import p25_deep_value_engine as deep_engine
from p25_smc import compute_smc_state

SMC_STRATEGY = "INDEP_PTB_BINANCE_SMC_SELECTIVE_5M_V3"
SMC_SOURCE = "INDEPENDENT_PTB_BINANCE_SMC_SELECTIVE_V3"
SMC_MIN_CONFIRMATIONS = 2
SMC_MAX_OPPOSITIONS = 0
SMC_MIN_ALIGNED_SCORE = 0.45
SMC_REQUIRE_STRUCTURE = True

_ENABLED = False
_ORIGINAL_FEATURE_UPDATE = None
_ORIGINAL_INDEPENDENT_TRACE = None
_ORIGINAL_SNAPSHOT = None


def _smc_rejection(direction: str, smc: dict[str, Any] | None) -> str | None:
    side = str(direction or "").upper()
    if side not in {"UP", "DOWN"}:
        return "SMC_DIRECTION_INVALID"
    if not isinstance(smc, dict) or not bool(smc.get("ready")):
        return "SMC_NOT_READY"

    side_sign = 1 if side == "UP" else -1
    sweep = int(((smc.get("liquidity_sweep") or {}).get("sign") or 0))
    structure = int(((smc.get("structure") or {}).get("sign") or 0))
    fvg = int(((smc.get("fvg") or {}).get("sign") or 0))
    signals = (sweep, structure, fvg)
    confirmations = sum(1 for signal in signals if signal == side_sign)
    oppositions = sum(1 for signal in signals if signal == -side_sign)
    score = float(smc.get("score") or 0.0) * side_sign

    smc["selected_side"] = side
    smc["selected_confirmations"] = confirmations
    smc["selected_oppositions"] = oppositions
    smc["selected_aligned_score"] = round(score, 4)
    smc["required_confirmations"] = SMC_MIN_CONFIRMATIONS
    smc["max_oppositions"] = SMC_MAX_OPPOSITIONS
    smc["min_aligned_score"] = SMC_MIN_ALIGNED_SCORE
    smc["structure_required"] = SMC_REQUIRE_STRUCTURE

    if SMC_REQUIRE_STRUCTURE and structure != side_sign:
        return "SMC_STRUCTURE_NOT_CONFIRMED"
    if oppositions > SMC_MAX_OPPOSITIONS:
        return f"SMC_OPPOSITION_{oppositions}_GT_{SMC_MAX_OPPOSITIONS}"
    if confirmations < SMC_MIN_CONFIRMATIONS:
        return f"SMC_CONFIRMATIONS_{confirmations}_LT_{SMC_MIN_CONFIRMATIONS}"
    if score + 1e-12 < SMC_MIN_ALIGNED_SCORE:
        return f"SMC_SCORE_{score:.3f}_LT_{SMC_MIN_ALIGNED_SCORE:.2f}"
    return None


def enable_smc_v3() -> None:
    """Enable V3 structural confirmation exactly once in this process."""
    global _ENABLED, _ORIGINAL_FEATURE_UPDATE, _ORIGINAL_INDEPENDENT_TRACE, _ORIGINAL_SNAPSHOT
    if _ENABLED:
        return

    _ORIGINAL_FEATURE_UPDATE = features.FeatureEngine.update
    _ORIGINAL_INDEPENDENT_TRACE = deep_engine._independent_paper_trace
    _ORIGINAL_SNAPSHOT = deep_engine.P25Engine.snapshot

    def feature_update_with_smc(self, *args, **kwargs):  # noqa: ANN001,ANN002,ANN003
        fv = _ORIGINAL_FEATURE_UPDATE(self, *args, **kwargs)
        prices = kwargs.get("prices")
        if prices is None and args:
            prices = args[0]
        now = kwargs.get("now")
        if now is None and len(args) >= 8:
            now = args[7]
        if now is None:
            now = getattr(fv, "ts", None) or time.time()
        try:
            smc = compute_smc_state(prices or [], now_ms=int(float(now) * 1000.0))
            fv.smc = smc.to_dict()
        except Exception as exc:  # noqa: BLE001
            fv.smc = {
                "ready": False,
                "reason": f"SMC_ERROR_{type(exc).__name__}",
                "score": 0.0,
                "direction": "NEUTRAL",
                "confirmations_up": 0,
                "confirmations_down": 0,
            }
        return fv

    def independent_trace_with_smc(cfg, ref, snap, fv, trace):  # noqa: ANN001
        paper_trace, reject_reason, alpha_dict = _ORIGINAL_INDEPENDENT_TRACE(
            cfg, ref, snap, fv, trace
        )
        if str(getattr(cfg, "paper_strategy_version", "")) != SMC_STRATEGY:
            return paper_trace, reject_reason, alpha_dict

        smc = getattr(fv, "smc", None) if fv is not None else None
        if not isinstance(smc, dict):
            smc = {"ready": False, "reason": "SMC_FEATURE_MISSING", "score": 0.0}

        if isinstance(alpha_dict, dict):
            alpha_dict["source"] = SMC_SOURCE
            alpha_dict["smc"] = smc
            trace["independent_alpha"] = alpha_dict
        trace["independent_alpha_source"] = SMC_SOURCE
        trace["smc"] = smc

        if reject_reason is not None:
            return paper_trace, reject_reason, alpha_dict
        if paper_trace is None or not isinstance(alpha_dict, dict):
            return paper_trace, reject_reason, alpha_dict

        smc_reason = _smc_rejection(str(alpha_dict.get("direction") or ""), smc)
        if smc_reason is not None:
            alpha_dict["smc_gate"] = "REJECT"
            alpha_dict["smc_gate_reason"] = smc_reason
            return None, f"INDEPENDENT_ALPHA_{smc_reason}", alpha_dict

        alpha_dict["smc_gate"] = "PASS"
        alpha_dict["smc_gate_reason"] = "PASS"
        paper_trace = dict(paper_trace)
        paper_trace["forecast_source"] = SMC_SOURCE
        return paper_trace, None, alpha_dict

    def snapshot_with_smc(self):  # noqa: ANN001
        data = _ORIGINAL_SNAPSHOT(self)
        if str(getattr(self.cfg, "paper_strategy_version", "")) != SMC_STRATEGY:
            return data
        safety = data.setdefault("safety", {})
        safety["paper_independent_alpha_source"] = SMC_SOURCE
        profile = safety.setdefault("paper_strict_profile", {})
        profile.update(
            {
                "smc_enabled": True,
                "smc_bar_sec": 5.0,
                "smc_event_max_age_sec": 20.0,
                "smc_require_structure": SMC_REQUIRE_STRUCTURE,
                "smc_min_confirmations": SMC_MIN_CONFIRMATIONS,
                "smc_max_oppositions": SMC_MAX_OPPOSITIONS,
                "smc_min_aligned_score": SMC_MIN_ALIGNED_SCORE,
                "smc_components": ["LIQUIDITY_SWEEP", "BOS_CHOCH", "FVG_DISPLACEMENT"],
            }
        )
        return data

    features.FeatureEngine.update = feature_update_with_smc
    deep_engine._independent_paper_trace = independent_trace_with_smc
    deep_engine.P25Engine.snapshot = snapshot_with_smc
    _ENABLED = True
