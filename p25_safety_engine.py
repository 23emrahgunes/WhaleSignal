"""Fail-closed decision gate for the P2.5 SHADOW model.

The statistical model may emit raw probabilities as soon as its training warmup is
complete.  Those probabilities are still useful for prequential evaluation, but
they must not become visible UP/DOWN decisions until out-of-sample selective
performance has produced a learned threshold.  This module keeps recording the raw
candidate while converting unsafe public decisions to ABSTAIN.

It also rejects a model direction that sharply contradicts independent, strong
signals (official PTB model, Polymarket implied probability and the deterministic
feature-consensus vote).  No order/execution code exists here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from models import AbstainReason, Decision, Prediction
from p25_engine import DecisionBundle
from p25_runtime_engine import P25Engine as _RuntimeP25Engine

_LEARNED_THRESHOLD_SOURCES = {"PER_COMBO_LEARNED", "OVERALL_LEARNED"}
_RELIABLE_CALIBRATION_SOURCES = {
    "PER_COMBO_RELIABILITY",
    "OVERALL_RELIABILITY",
}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _sign_probability(value: Optional[float], margin: float) -> int:
    if value is None:
        return 0
    value = float(value)
    if value >= 0.5 + margin:
        return 1
    if value <= 0.5 - margin:
        return -1
    return 0


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    reason: str
    details: tuple[str, ...]
    candidate_decision: str
    validation_ready: bool
    supporting_votes: int = 0
    opposing_votes: int = 0


def evaluate_decision_gate(
    *,
    p_up: float,
    threshold_source: str,
    calibration_source: str,
    calibration_n: int,
    market_up: Optional[float],
    ptb_model_up: Optional[float],
    directional_vote: float,
    directional_consensus: float,
    calibration_enabled: bool,
    require_learned_threshold: bool = True,
) -> GateResult:
    """Evaluate whether a raw candidate may be published as UP/DOWN.

    ``p_up`` is still recorded even when the gate rejects the candidate, so the
    model can accumulate honest prequential Brier/log-loss evidence.
    """
    model_sign = 1 if float(p_up) >= 0.5 else -1
    candidate = Decision.UP.value if model_sign > 0 else Decision.DOWN.value
    threshold_source = str(threshold_source or "")
    calibration_source = str(calibration_source or "")
    validation_ready = threshold_source in _LEARNED_THRESHOLD_SOURCES

    if require_learned_threshold and not validation_ready:
        return GateResult(
            False,
            "MODEL_UNVALIDATED",
            (
                f"threshold_source={threshold_source or 'NONE'}",
                f"calibration_source={calibration_source or 'NONE'}",
                "raw candidate is recorded for OOS scoring only",
            ),
            candidate,
            False,
        )

    if calibration_enabled and calibration_source not in _RELIABLE_CALIBRATION_SOURCES:
        return GateResult(
            False,
            "MODEL_UNCALIBRATED_BIN",
            (
                f"calibration_source={calibration_source or 'NONE'}",
                f"calibration_bin_n={int(calibration_n or 0)}",
            ),
            candidate,
            validation_ready,
        )

    votes: list[tuple[str, int]] = []
    market_sign = _sign_probability(market_up, 0.20)
    if market_sign:
        votes.append(("market", market_sign))
    ptb_sign = _sign_probability(ptb_model_up, 0.15)
    if ptb_sign:
        votes.append(("ptb_model", ptb_sign))
    if directional_consensus >= 0.60 and abs(directional_vote) >= 0.25:
        votes.append(("feature_consensus", 1 if directional_vote > 0 else -1))

    supporting = sum(1 for _, sign in votes if sign == model_sign)
    opposing = sum(1 for _, sign in votes if sign == -model_sign)

    # A nearly-settled market pointing the other way is not automatically proof
    # that the model is wrong, but with a young shared model it is a hard safety
    # conflict unless per-combo validation has already produced its own threshold.
    extreme_market_conflict = (
        market_up is not None
        and abs(float(market_up) - 0.5) >= 0.40
        and _sign_probability(market_up, 0.40) == -model_sign
        and abs(float(p_up) - float(market_up)) >= 0.50
    )
    per_combo_validated = threshold_source == "PER_COMBO_LEARNED"
    if extreme_market_conflict and not per_combo_validated:
        return GateResult(
            False,
            "MODEL_MARKET_CONFLICT",
            (
                f"model_p_up={float(p_up):.4f}",
                f"market_p_up={float(market_up):.4f}",
                "contrarian decision requires per-combo validation",
            ),
            candidate,
            validation_ready,
            supporting,
            opposing,
        )

    if opposing >= 2 and opposing > supporting:
        names = ",".join(name for name, sign in votes if sign == -model_sign)
        return GateResult(
            False,
            "MODEL_BASELINE_CONFLICT",
            (
                f"opposing_votes={names}",
                f"supporting={supporting} opposing={opposing}",
            ),
            candidate,
            validation_ready,
            supporting,
            opposing,
        )

    return GateResult(
        True,
        "PASS",
        (f"supporting={supporting} opposing={opposing}",),
        candidate,
        validation_ready,
        supporting,
        opposing,
    )


class P25Engine(_RuntimeP25Engine):
    """Runtime engine that publishes only statistically validated decisions."""

    def decide(self, ref, snap, q, fv) -> DecisionBundle:  # noqa: ANN001
        bundle = super().decide(ref, snap, q, fv)
        prediction = bundle.prediction
        trace = bundle.trace

        if prediction.decision not in (Decision.UP, Decision.DOWN):
            trace.setdefault("candidate_decision", None)
            trace.setdefault("decision_gate", "NOT_APPLICABLE")
            trace.setdefault("validation_ready", False)
            return bundle

        p_up = trace.get("p_up_calibrated")
        if p_up is None:
            p_up = trace.get("p_up_raw")
        if p_up is None:
            return bundle

        regime = bundle.regime
        output = bundle.model_output
        gate = evaluate_decision_gate(
            p_up=float(p_up),
            threshold_source=str(trace.get("threshold_source") or ""),
            calibration_source=str(trace.get("calibration_source") or ""),
            calibration_n=int(trace.get("calibration_n") or 0),
            market_up=trace.get("p_up_market"),
            ptb_model_up=(output.p_up_ptb if output is not None else None),
            directional_vote=(regime.directional_vote if regime is not None else 0.0),
            directional_consensus=(
                regime.directional_consensus if regime is not None else 0.0
            ),
            calibration_enabled=bool(self.cfg.calibration_active),
            require_learned_threshold=_env_bool(
                "DECISION_REQUIRES_LEARNED_THRESHOLD",
                True,
            ),
        )
        trace.update(
            {
                "candidate_decision": gate.candidate_decision,
                "decision_gate": gate.reason,
                "validation_ready": gate.validation_ready,
                "gate_supporting_votes": gate.supporting_votes,
                "gate_opposing_votes": gate.opposing_votes,
            }
        )
        if gate.allowed:
            trace["decision_gate"] = "PASS"
            return bundle

        # Preserve the probability for scoring, but never publish it as an active
        # signal while the gate is closed.
        trace["decision"] = Decision.ABSTAIN.value
        trace["abstain_reason"] = gate.reason
        reasons = list(prediction.reasons) + [gate.reason, *gate.details]
        enum_reason = (
            AbstainReason.FEATURE_CONFLICT
            if "CONFLICT" in gate.reason
            else AbstainReason.INSUFFICIENT_DATA
        )
        safe_prediction = Prediction(
            combo=prediction.combo,
            ts=prediction.ts,
            p_up=prediction.p_up,
            p_down=prediction.p_down,
            confidence=0.0,
            predictability=prediction.predictability,
            regime=prediction.regime,
            decision=Decision.ABSTAIN,
            abstain_reason=enum_reason,
            reasons=reasons,
            market_implied_up=prediction.market_implied_up,
        )
        return DecisionBundle(
            safe_prediction,
            trace,
            bundle.regime,
            bundle.model_output,
        )

    def _card_p25(self, ref, snap, q, bundle, fv) -> dict:  # noqa: ANN001
        card = super()._card_p25(ref, snap, q, bundle, fv)
        gate = bundle.trace.get("decision_gate")
        card["candidate_decision"] = bundle.trace.get("candidate_decision")
        card["decision_gate"] = gate
        card["validation_ready"] = bool(
            bundle.trace.get("validation_ready", False)
        )
        card["gate_supporting_votes"] = bundle.trace.get(
            "gate_supporting_votes", 0
        )
        card["gate_opposing_votes"] = bundle.trace.get(
            "gate_opposing_votes", 0
        )
        if gate and gate not in {"PASS", "NOT_APPLICABLE"}:
            card["abstain_reason"] = gate
        return card

    def snapshot(self) -> dict:
        data = super().snapshot()
        cards = data.get("cards") or []
        footer = data.setdefault("footer", {})
        footer["validated_decision_cards"] = sum(
            1
            for card in cards
            if card.get("active") and card.get("decision_gate") == "PASS"
        )
        footer["unvalidated_candidate_cards"] = sum(
            1
            for card in cards
            if card.get("active")
            and card.get("candidate_decision") in {"UP", "DOWN"}
            and card.get("decision_gate") != "PASS"
        )
        data["model_safety"] = {
            "requires_learned_threshold": _env_bool(
                "DECISION_REQUIRES_LEARNED_THRESHOLD",
                True,
            ),
            "learned_threshold_sources": sorted(_LEARNED_THRESHOLD_SOURCES),
            "reliable_calibration_sources": sorted(
                _RELIABLE_CALIBRATION_SOURCES
            ),
        }
        return data
