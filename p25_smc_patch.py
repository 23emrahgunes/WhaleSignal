"""Runtime integration for the SMC Selective V3 Direction Engine cohort.

V3 stays isolated from the proven V2 control cohort. It adds structural SMC
confirmation, a fast operational ``/api/state`` path that does not scan the complete
historical forecast table, and in-memory/log telemetry for every distinct gate reason
seen per market condition.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from typing import Any

import features
import p25_deep_value_engine as deep_engine
import p25_engine as core_engine
from p25_smc import BAR_MS, EVENT_MAX_AGE_SEC, compute_smc_state

SMC_STRATEGY = "INDEP_PTB_BINANCE_SMC_SELECTIVE_5M_V3"
SMC_SOURCE = "INDEPENDENT_PTB_BINANCE_SMC_SELECTIVE_V3"
SMC_MIN_CONFIRMATIONS = 2
SMC_MAX_OPPOSITIONS = 0
SMC_MIN_ALIGNED_SCORE = 0.45
SMC_REQUIRE_STRUCTURE = True

log = logging.getLogger("direction_engine.p25.smc_v3")

_ENABLED = False
_ORIGINAL_FEATURE_UPDATE = None
_ORIGINAL_INDEPENDENT_TRACE = None
_ORIGINAL_CORE_SNAPSHOT = None
_ORIGINAL_SNAPSHOT = None
_ORIGINAL_CARD = None

_GATE_LOCK = threading.Lock()
_GATE_REASON_COUNTS: Counter[str] = Counter()
_GATE_SEEN: set[tuple[str, str]] = set()
_GATE_CONDITIONS: set[str] = set()
_GATE_ENTRY_CONDITIONS: set[str] = set()
_GATE_LAST_BY_COMBO: dict[str, dict[str, Any]] = {}


def _reason_bucket(reason: object) -> str:
    raw = str(reason or "UNKNOWN").strip() or "UNKNOWN"
    prefixes = (
        ("WAITING_FOR_ENTRY_WINDOW_TTE_", "WAITING_FOR_ENTRY_WINDOW"),
        ("ENTRY_WINDOW_CLOSED_TTE_", "ENTRY_WINDOW_CLOSED"),
        ("INDEPENDENT_ALPHA_STABILITY_", "INDEPENDENT_ALPHA_STABILITY"),
        ("INDEPENDENT_ALPHA_SMC_CONFIRMATIONS_", "SMC_CONFIRMATIONS_BELOW_MINIMUM"),
        ("INDEPENDENT_ALPHA_SMC_OPPOSITION_", "SMC_OPPOSITION"),
        ("INDEPENDENT_ALPHA_SMC_SCORE_", "SMC_SCORE_BELOW_MINIMUM"),
        ("SMC_CONFIRMATIONS_", "SMC_CONFIRMATIONS_BELOW_MINIMUM"),
        ("SMC_OPPOSITION_", "SMC_OPPOSITION"),
        ("SMC_SCORE_", "SMC_SCORE_BELOW_MINIMUM"),
    )
    for prefix, bucket in prefixes:
        if raw.startswith(prefix):
            return bucket
    return raw


def _smc_summary(smc: dict[str, Any]) -> dict[str, Any]:
    def event(name: str) -> dict[str, Any]:
        item = smc.get(name) or {}
        return {
            "kind": item.get("kind"),
            "sign": item.get("sign"),
            "strength": item.get("strength"),
            "age_sec": item.get("age_sec"),
        }

    return {
        "ready": bool(smc.get("ready")),
        "reason": smc.get("reason"),
        "score": smc.get("score"),
        "direction": smc.get("direction"),
        "confirmations_up": smc.get("confirmations_up"),
        "confirmations_down": smc.get("confirmations_down"),
        "selected_confirmations": smc.get("selected_confirmations"),
        "selected_oppositions": smc.get("selected_oppositions"),
        "selected_aligned_score": smc.get("selected_aligned_score"),
        "structure": event("structure"),
        "liquidity_sweep": event("liquidity_sweep"),
        "fvg": event("fvg"),
    }


def _record_gate_card(ref, card: dict[str, Any]) -> None:  # noqa: ANN001
    condition_id = str(
        getattr(ref, "condition_id", None)
        or getattr(ref, "market_id", None)
        or card.get("condition_id")
        or "UNKNOWN"
    )
    combo = str(
        getattr(getattr(ref, "combo", None), "key", None)
        or card.get("combo")
        or "UNKNOWN"
    )
    paper = card.get("paper_trade") or {}
    paper_status = str(paper.get("status") or "").upper()
    alpha = card.get("independent_alpha") or {}
    smc = alpha.get("smc") or card.get("smc") or {}

    if paper_status in {"OPEN", "SETTLED"}:
        raw_reason = "PAPER_ENTRY"
    else:
        raw_reason = (
            card.get("paper_deep_value_watch_reason")
            or alpha.get("smc_gate_reason")
            or alpha.get("reason")
            or card.get("abstain_reason")
            or "UNKNOWN"
        )
    bucket = _reason_bucket(raw_reason)

    current = {
        "updated_at": time.time(),
        "condition_id": condition_id,
        "combo": combo,
        "tte_sec": card.get("tte_sec"),
        "reason": str(raw_reason),
        "reason_bucket": bucket,
        "direction": alpha.get("direction") or card.get("paper_direction"),
        "p_up": (
            alpha.get("p_up")
            if alpha.get("p_up") is not None
            else card.get("paper_p_up")
        ),
        "z_terminal": alpha.get("z_terminal"),
        "stability_elapsed_sec": alpha.get("stability_elapsed_sec"),
        "paper_status": paper_status or None,
        "smc": _smc_summary(smc if isinstance(smc, dict) else {}),
    }

    key = (condition_id, bucket)
    with _GATE_LOCK:
        _GATE_CONDITIONS.add(condition_id)
        _GATE_LAST_BY_COMBO[combo] = current
        is_new = key not in _GATE_SEEN
        if is_new:
            _GATE_SEEN.add(key)
            _GATE_REASON_COUNTS[bucket] += 1
            if bucket == "PAPER_ENTRY":
                _GATE_ENTRY_CONDITIONS.add(condition_id)

    if is_new:
        smc_view = current["smc"]
        log.info(
            "SMC GATE condition=%s combo=%s bucket=%s raw=%s tte=%s dir=%s "
            "p_up=%s z=%s smc_score=%s structure=%s sweep=%s fvg=%s",
            condition_id[-12:],
            combo,
            bucket,
            raw_reason,
            card.get("tte_sec"),
            current.get("direction"),
            current.get("p_up"),
            current.get("z_terminal"),
            smc_view.get("score"),
            (smc_view.get("structure") or {}).get("kind"),
            (smc_view.get("liquidity_sweep") or {}).get("kind"),
            (smc_view.get("fvg") or {}).get("kind"),
        )


def smc_gate_telemetry() -> dict[str, Any]:
    with _GATE_LOCK:
        ordered = dict(
            sorted(
                _GATE_REASON_COUNTS.items(),
                key=lambda item: (-item[1], item[0]),
            )
        )
        return {
            "strategy": SMC_STRATEGY,
            "conditions_seen": len(_GATE_CONDITIONS),
            "paper_entry_conditions": len(_GATE_ENTRY_CONDITIONS),
            "reason_counts_unique_condition_bucket": ordered,
            "last_by_combo": dict(_GATE_LAST_BY_COMBO),
        }


def _reset_smc_gate_telemetry_for_tests() -> None:
    with _GATE_LOCK:
        _GATE_REASON_COUNTS.clear()
        _GATE_SEEN.clear()
        _GATE_CONDITIONS.clear()
        _GATE_ENTRY_CONDITIONS.clear()
        _GATE_LAST_BY_COMBO.clear()


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
    global _ENABLED
    global _ORIGINAL_FEATURE_UPDATE, _ORIGINAL_INDEPENDENT_TRACE
    global _ORIGINAL_CORE_SNAPSHOT, _ORIGINAL_SNAPSHOT, _ORIGINAL_CARD

    if _ENABLED:
        return

    # The historical P2.5 state snapshot computes a full resolved-forecast report.
    # On a long-lived VPS database this can take longer than the HTTP timeout and
    # leave SnapshotCache prewarm unavailable. V3's operational state deliberately
    # defers that research scan while retaining cards, paper, safety and LIVE state.
    _ORIGINAL_CORE_SNAPSHOT = core_engine.P25Engine.snapshot

    def fast_core_snapshot(self):  # noqa: ANN001
        data = super(core_engine.P25Engine, self).snapshot()
        data["phase"] = self.cfg.phase
        data["mode"] = "SHADOW"
        data["forecast_analytics"] = {
            "status": "DEFERRED",
            "reason": "SMC_V3_OPERATIONAL_STATE_FAST_PATH",
        }
        cards = data.get("cards", [])
        footer = data.setdefault("footer", {})
        footer["features_ready"] = sum(
            1
            for card in cards
            if card.get("active") and card.get("feature", {}).get("ready")
        )
        footer["model_ready_cards"] = sum(
            1
            for card in cards
            if card.get("active") and card.get("p_up_raw") is not None
        )
        footer["forecast_writes_runtime"] = self._forecast_writes
        stats = self.recorder.stats()
        footer["forecasts"] = stats.get("forecasts", 0)
        footer["labeled_forecasts"] = stats.get("labeled_forecasts", 0)
        data.setdefault("safety", {}).update(
            {
                "model_inference_enabled": self.cfg.model_inference_active,
                "forecast_recording_enabled": self.cfg.forecast_recording_active,
                "execution_enabled": False,
                "private_key_loaded": False,
            }
        )
        return data

    core_engine.P25Engine.snapshot = fast_core_snapshot

    _ORIGINAL_FEATURE_UPDATE = features.FeatureEngine.update
    _ORIGINAL_INDEPENDENT_TRACE = deep_engine._independent_paper_trace
    _ORIGINAL_SNAPSHOT = deep_engine.P25Engine.snapshot
    _ORIGINAL_CARD = deep_engine.P25Engine._card_p25

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
            smc = {
                "ready": False,
                "reason": "SMC_FEATURE_MISSING",
                "score": 0.0,
            }

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

    def card_with_smc_telemetry(self, ref, snap, q, bundle, fv):  # noqa: ANN001
        card = _ORIGINAL_CARD(self, ref, snap, q, bundle, fv)
        if str(getattr(self.cfg, "paper_strategy_version", "")) == SMC_STRATEGY:
            _record_gate_card(ref, card)
        return card

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
                "smc_bar_sec": BAR_MS / 1000.0,
                "smc_event_max_age_sec": EVENT_MAX_AGE_SEC,
                "smc_require_structure": SMC_REQUIRE_STRUCTURE,
                "smc_min_confirmations": SMC_MIN_CONFIRMATIONS,
                "smc_max_oppositions": SMC_MAX_OPPOSITIONS,
                "smc_min_aligned_score": SMC_MIN_ALIGNED_SCORE,
                "smc_components": [
                    "LIQUIDITY_SWEEP",
                    "BOS_CHOCH",
                    "FVG_DISPLACEMENT",
                ],
            }
        )
        data["smc_v3_telemetry"] = smc_gate_telemetry()
        return data

    features.FeatureEngine.update = feature_update_with_smc
    deep_engine._independent_paper_trace = independent_trace_with_smc
    deep_engine.P25Engine._card_p25 = card_with_smc_telemetry
    deep_engine.P25Engine.snapshot = snapshot_with_smc
    _ENABLED = True
