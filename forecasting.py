"""P2.5 fail-closed SHADOW forecast pipeline.

Ordering is strict:
1. plumbing/data quality,
2. feature warm-up,
3. predictability/regime gate,
4. B2/B1 model inference,
5. probability calibration,
6. evidence-backed decision threshold.

A raw probability may be retained for analytics, but a directional decision is not
emitted unless every gate is ready.  No order, signer or execution code exists.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from baselines import BaselineOutput, baseline_probabilities
from calibration import CalibrationBook, CalibrationOutput, ThresholdOutput
from direction_model import MODEL_VERSION, DirectionModel, ModelOutput
from features import FeatureVector
from models import AbstainReason, AssetHorizon, Decision, Prediction, Regime
from regime import RegimeResult, classify_regime

FORECAST_VERSION = "P2.5-shadow-v1"


@dataclass
class ForecastEnvelope:
    prediction: Prediction
    regime: RegimeResult
    baselines: BaselineOutput
    raw_p_up: Optional[float] = None
    calibrated_p_up: Optional[float] = None
    p_up_no_clob: Optional[float] = None
    model_source: str = "none"
    model_version: str = MODEL_VERSION
    model_schema_hash: Optional[str] = None
    calibration_ready: bool = False
    calibration_source: str = "missing"
    calibration_markets: int = 0
    threshold_ready: bool = False
    threshold_source: str = "insufficient"
    decision_margin: float = 0.0
    data_ready: bool = False
    feature_ready: bool = False
    forecast_version: str = FORECAST_VERSION
    diagnostics: list[str] = field(default_factory=list)

    def to_record(self) -> dict:
        return {
            "forecast_version": self.forecast_version,
            "model_version": self.model_version,
            "model_source": self.model_source,
            "model_schema_hash": self.model_schema_hash,
            "p_up_raw": self.raw_p_up,
            "p_up_calibrated": self.calibrated_p_up,
            "p_up_no_clob": self.p_up_no_clob,
            "baseline_coinflip": self.baselines.coinflip,
            "baseline_ptb": self.baselines.ptb_diffusion,
            "market_implied_up": self.baselines.market_implied,
            "calibration_ready": self.calibration_ready,
            "calibration_source": self.calibration_source,
            "calibration_markets": self.calibration_markets,
            "threshold_ready": self.threshold_ready,
            "threshold_source": self.threshold_source,
            "decision_margin": self.decision_margin,
            "decision": self.prediction.decision.value,
            "abstain_reason": self.prediction.abstain_reason.value,
            "confidence": self.prediction.confidence,
            "predictability": self.prediction.predictability,
            "regime": self.prediction.regime.value,
            "direction_score": self.regime.direction_score,
            "agreement": self.regime.agreement,
            "conflict": self.regime.conflict,
            "data_ready": self.data_ready,
            "feature_ready": self.feature_ready,
            "why": list(self.prediction.reasons),
            "regime_diagnostics": self.regime.to_dict(),
            "diagnostics": list(self.diagnostics),
        }


class ShadowForecaster:
    def __init__(
        self,
        model: DirectionModel,
        calibration: CalibrationBook,
        *,
        inference_enabled: bool = True,
    ) -> None:
        self.model = model
        self.calibration = calibration
        self.inference_enabled = inference_enabled

    @staticmethod
    def _prediction(
        combo: AssetHorizon,
        ts: float,
        market_implied_up: Optional[float],
        *,
        decision: Decision = Decision.ABSTAIN,
        reason: AbstainReason = AbstainReason.INSUFFICIENT_DATA,
        p_up: float = 0.5,
        confidence: float = 0.0,
        predictability: float = 0.0,
        regime: Regime = Regime.UNKNOWN,
        reasons: Optional[list[str]] = None,
    ) -> Prediction:
        return Prediction(
            combo=combo,
            ts=ts,
            p_up=p_up,
            p_down=1.0 - p_up,
            confidence=confidence,
            predictability=predictability,
            regime=regime,
            decision=decision,
            abstain_reason=reason,
            reasons=list(reasons or []),
            market_implied_up=market_implied_up,
        )

    def evaluate(
        self,
        combo: AssetHorizon,
        ts: float,
        fv: Optional[FeatureVector],
        *,
        data_ready: bool,
        data_abstain_reason: AbstainReason = AbstainReason.NONE,
        data_notes: Optional[list[str]] = None,
    ) -> ForecastEnvelope:
        notes = list(data_notes or [])
        regime = classify_regime(fv)
        baselines = baseline_probabilities(fv) if fv is not None else BaselineOutput()
        feature_ready = bool(fv and fv.feature_ready)

        if not data_ready:
            prediction = self._prediction(
                combo, ts, baselines.market_implied,
                reason=(
                    data_abstain_reason
                    if data_abstain_reason != AbstainReason.NONE
                    else AbstainReason.INSUFFICIENT_DATA
                ),
                regime=regime.regime,
                predictability=regime.predictability,
                reasons=notes + ["data_gate_not_ready"],
            )
            return ForecastEnvelope(
                prediction, regime, baselines,
                data_ready=False, feature_ready=feature_ready,
                diagnostics=["data_gate"],
            )

        if not feature_ready:
            prediction = self._prediction(
                combo, ts, baselines.market_implied,
                reason=AbstainReason.INSUFFICIENT_DATA,
                regime=regime.regime,
                predictability=regime.predictability,
                reasons=notes + ["feature_warmup"] + list(regime.reasons),
            )
            return ForecastEnvelope(
                prediction, regime, baselines,
                data_ready=True, feature_ready=False,
                diagnostics=["feature_gate"],
            )

        if regime.abstain:
            prediction = self._prediction(
                combo, ts, baselines.market_implied,
                reason=regime.abstain_reason,
                regime=regime.regime,
                predictability=regime.predictability,
                reasons=notes + list(regime.reasons),
            )
            return ForecastEnvelope(
                prediction, regime, baselines,
                data_ready=True, feature_ready=True,
                diagnostics=["regime_gate"],
            )

        if not self.inference_enabled:
            prediction = self._prediction(
                combo, ts, baselines.market_implied,
                reason=AbstainReason.MODEL_NOT_TRAINED,
                regime=regime.regime,
                predictability=regime.predictability,
                reasons=notes + ["model_inference_disabled"],
            )
            return ForecastEnvelope(
                prediction, regime, baselines,
                data_ready=True, feature_ready=True,
                diagnostics=["model_disabled"],
            )

        model_output: ModelOutput = self.model.predict(combo.key, fv)
        baselines = model_output.baselines
        if not model_output.ready or model_output.p_up is None:
            prediction = self._prediction(
                combo, ts, baselines.market_implied,
                reason=AbstainReason.MODEL_NOT_TRAINED,
                regime=regime.regime,
                predictability=regime.predictability,
                reasons=notes + [f"model_not_ready:{model_output.source}"],
            )
            return ForecastEnvelope(
                prediction, regime, baselines,
                p_up_no_clob=model_output.p_up_no_clob,
                model_source=model_output.source,
                model_schema_hash=model_output.schema_hash,
                data_ready=True, feature_ready=True,
                diagnostics=["model_gate"],
            )

        calibration: CalibrationOutput = self.calibration.calibrate(
            combo.key, model_output.p_up
        )
        threshold: ThresholdOutput = self.calibration.threshold_for(combo.key)
        final_p = calibration.calibrated_p_up or model_output.p_up
        common = dict(
            raw_p_up=model_output.p_up,
            calibrated_p_up=final_p,
            p_up_no_clob=model_output.p_up_no_clob,
            model_source=model_output.source,
            model_schema_hash=model_output.schema_hash,
            calibration_ready=calibration.ready,
            calibration_source=calibration.source,
            calibration_markets=calibration.n_markets,
            threshold_ready=threshold.ready,
            threshold_source=threshold.source,
            decision_margin=threshold.margin,
            data_ready=True,
            feature_ready=True,
        )

        if not calibration.ready or not threshold.ready:
            missing = []
            if not calibration.ready:
                missing.append("calibration_not_ready")
            if not threshold.ready:
                missing.append("threshold_not_ready")
            prediction = self._prediction(
                combo, ts, baselines.market_implied,
                reason=AbstainReason.INSUFFICIENT_DATA,
                p_up=final_p,
                confidence=2.0 * abs(final_p - 0.5) * regime.predictability,
                regime=regime.regime,
                predictability=regime.predictability,
                reasons=notes + list(regime.reasons) + missing,
            )
            return ForecastEnvelope(
                prediction, regime, baselines,
                diagnostics=missing,
                **common,
            )

        if final_p >= 0.5 + threshold.margin:
            decision, reason = Decision.UP, AbstainReason.NONE
        elif final_p <= 0.5 - threshold.margin:
            decision, reason = Decision.DOWN, AbstainReason.NONE
        else:
            decision, reason = Decision.ABSTAIN, AbstainReason.LOW_PREDICTABILITY

        prediction = self._prediction(
            combo, ts, baselines.market_implied,
            decision=decision,
            reason=reason,
            p_up=final_p,
            confidence=min(1.0, 2.0 * abs(final_p - 0.5) * regime.predictability),
            regime=regime.regime,
            predictability=regime.predictability,
            reasons=(
                notes
                + list(regime.reasons)
                + [
                    f"model={model_output.source}",
                    f"calibration={calibration.source}",
                    f"margin={threshold.margin:.3f}",
                ]
            ),
        )
        return ForecastEnvelope(
            prediction, regime, baselines,
            diagnostics=["decision_ready"],
            **common,
        )
