"""Fail-closed validated signal plus always-on P2.5 research forecast.

Two outputs are intentionally separate:

- ``forecast_*``: the best current SHADOW estimate of the eventual UP/DOWN result.
  It is a robust multi-source ensemble and may be PROVISIONAL.
- ``decision``: the statistically validated signal.  It remains ABSTAIN until
  prequential evidence has produced a learned threshold and reliable calibration.

This separation lets the dashboard show an actual forecast immediately without
misrepresenting an immature model candidate as an actionable signal.  No order or
execution code exists here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from models import AbstainReason, Decision, Prediction
from p25_engine import DecisionBundle
from p25_research_forecast import ResearchForecast, build_research_forecast
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


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


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
    """Evaluate whether a raw candidate may be published as a validated signal."""
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
    """Publish a research forecast now and a validated signal only when proven."""

    def _apply_signal_gate(self, bundle: DecisionBundle) -> DecisionBundle:
        prediction = bundle.prediction
        trace = bundle.trace

        if prediction.decision not in (Decision.UP, Decision.DOWN):
            trace.setdefault("candidate_decision", None)
            trace.setdefault("decision_gate", "NOT_APPLICABLE")
            trace.setdefault("validation_ready", False)
            trace.setdefault("signal_decision", prediction.decision.value)
            return bundle

        p_up = trace.get("p_up_calibrated")
        if p_up is None:
            p_up = trace.get("p_up_raw")
        if p_up is None:
            trace.setdefault("signal_decision", prediction.decision.value)
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
            trace["signal_decision"] = prediction.decision.value
            return bundle

        trace["decision"] = Decision.ABSTAIN.value
        trace["signal_decision"] = Decision.ABSTAIN.value
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

    def _research_model_output(self, ref, fv, bundle):  # noqa: ANN001
        output = bundle.model_output
        if output is not None:
            return output
        if (
            fv is None
            or not getattr(fv, "feature_ready", False)
            or not self.cfg.model_inference_active
        ):
            return None
        try:
            return self.model.predict(ref.combo.key, fv)
        except Exception:  # noqa: BLE001
            return None

    def _model_market_count(self, combo_key: str) -> int:
        try:
            stats = self.model.stats()
            b2 = stats.get("b2_full") or {}
            shared = int(b2.get("shared_markets") or 0)
            combo = int(
                ((b2.get("per_combo") or {}).get(combo_key) or {}).get("markets")
                or 0
            )
            return max(shared, combo)
        except Exception:  # noqa: BLE001
            return 0

    def _attach_research_forecast(
        self,
        ref,
        snap,
        fv,
        bundle: DecisionBundle,
    ) -> DecisionBundle:  # noqa: ANN001
        trace = bundle.trace
        output = self._research_model_output(ref, fv, bundle)
        regime = bundle.regime

        model_p = trace.get("p_up_calibrated")
        if model_p is None and output is not None:
            model_p = output.p_up
        external_p = output.p_up_no_clob if output is not None else None
        ptb_model_p = output.p_up_ptb if output is not None else None
        ptb_heuristic_p = (
            output.p_up_ptb_heuristic
            if output is not None and output.p_up_ptb_heuristic is not None
            else trace.get("p_up_ptb_heuristic")
        )
        directional_vote = (
            regime.directional_vote
            if regime is not None
            else float(getattr(fv, "directional_vote", 0.0) or 0.0)
        )
        directional_consensus = (
            regime.directional_consensus
            if regime is not None
            else float(getattr(fv, "directional_consensus", 0.0) or 0.0)
        )
        predictability = float(
            trace.get("predictability")
            or bundle.prediction.predictability
            or 0.0
        )
        conflict_score = float(trace.get("conflict_score") or 0.0)
        validated_signal = (
            bundle.prediction.decision in (Decision.UP, Decision.DOWN)
            and trace.get("decision_gate") == "PASS"
        )

        forecast = build_research_forecast(
            model_p_up=model_p,
            external_p_up=external_p,
            ptb_model_p_up=ptb_model_p,
            ptb_heuristic_p_up=ptb_heuristic_p,
            market_p_up=snap.up_mid,
            directional_vote=directional_vote,
            directional_consensus=directional_consensus,
            predictability=predictability,
            conflict_score=conflict_score,
            model_markets=self._model_market_count(ref.combo.key),
            validated_signal=validated_signal,
            maturity_target_markets=_env_int(
                "RESEARCH_FORECAST_MATURITY_MARKETS",
                120,
            ),
        )
        forecast_dict = forecast.to_dict()
        trace.update(
            {
                "forecast_direction": forecast.direction,
                "forecast_p_up": forecast.p_up,
                "forecast_confidence": forecast.confidence,
                "forecast_grade": forecast.grade,
                "forecast_status": forecast.status,
                "forecast_source": forecast.source,
                "forecast_agreement": forecast.agreement,
                "forecast_model_maturity": forecast.model_maturity,
                "forecast_components": forecast_dict["components"],
                "forecast_reasons": forecast_dict["reasons"],
            }
        )
        return DecisionBundle(
            bundle.prediction,
            trace,
            bundle.regime,
            output or bundle.model_output,
        )

    def decide(self, ref, snap, q, fv) -> DecisionBundle:  # noqa: ANN001
        raw_bundle = super().decide(ref, snap, q, fv)
        gated_bundle = self._apply_signal_gate(raw_bundle)
        return self._attach_research_forecast(ref, snap, fv, gated_bundle)

    def _card_p25(self, ref, snap, q, bundle, fv) -> dict:  # noqa: ANN001
        card = super()._card_p25(ref, snap, q, bundle, fv)
        trace = bundle.trace
        gate = trace.get("decision_gate")
        card["signal_decision"] = bundle.prediction.decision.value
        card["candidate_decision"] = trace.get("candidate_decision")
        card["decision_gate"] = gate
        card["validation_ready"] = bool(trace.get("validation_ready", False))
        card["gate_supporting_votes"] = trace.get("gate_supporting_votes", 0)
        card["gate_opposing_votes"] = trace.get("gate_opposing_votes", 0)
        card["forecast_direction"] = trace.get("forecast_direction")
        card["forecast_p_up"] = trace.get("forecast_p_up")
        card["forecast_confidence"] = trace.get("forecast_confidence")
        card["forecast_grade"] = trace.get("forecast_grade")
        card["forecast_status"] = trace.get("forecast_status")
        card["forecast_source"] = trace.get("forecast_source")
        card["forecast_agreement"] = trace.get("forecast_agreement")
        card["forecast_model_maturity"] = trace.get("forecast_model_maturity")
        card["forecast_components"] = trace.get("forecast_components") or []
        card["forecast_reasons"] = trace.get("forecast_reasons") or []
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
        footer["forecast_up_cards"] = sum(
            1
            for card in cards
            if card.get("active") and card.get("forecast_direction") == "UP"
        )
        footer["forecast_down_cards"] = sum(
            1
            for card in cards
            if card.get("active") and card.get("forecast_direction") == "DOWN"
        )
        footer["forecast_high_grade_cards"] = sum(
            1
            for card in cards
            if card.get("active") and card.get("forecast_grade") == "HIGH"
        )
        footer["forecast_provisional_cards"] = sum(
            1
            for card in cards
            if card.get("active")
            and card.get("forecast_status") in {"PROVISIONAL", "CONFLICTED", "LIMITED"}
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
            "forecast_layer": "ROBUST_ENSEMBLE_V1",
            "forecast_is_actionable": False,
        }
        return data
