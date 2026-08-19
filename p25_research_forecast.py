"""Always-on, non-actionable directional forecast for P2.5 SHADOW.

The validated signal gate answers whether a model decision is safe enough to publish
as a signal.  This module answers a different question: "given the information we
have now, which side is the best research forecast?"  It therefore emits an UP/DOWN
forecast even while the actionable signal remains ABSTAIN.

The forecast is deliberately robust to a young, overconfident statistical model.  It
pools independent evidence from the official PTB baseline, Polymarket implied price,
feature consensus, the external-only model and the full model.  Young/unvalidated
model probabilities receive little weight and every component is clipped before
pooling, so a raw 0.1% probability cannot overwhelm three strong opposing sources.

This is SHADOW analytics only.  It contains no order, signing or execution logic.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


def _probability_score(p_up: float, cap: float) -> float:
    """Map probability to [-cap,+cap], preventing 0/1 domination."""
    return _clip(2.0 * (_clip(p_up, 0.0, 1.0) - 0.5), -cap, cap)


@dataclass(frozen=True)
class ForecastComponent:
    name: str
    p_up: Optional[float]
    score: float
    weight: float

    @property
    def contribution(self) -> float:
        return self.score * self.weight

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "p_up": round(self.p_up, 6) if self.p_up is not None else None,
            "score": round(self.score, 6),
            "weight": round(self.weight, 6),
            "contribution": round(self.contribution, 6),
        }


@dataclass(frozen=True)
class ResearchForecast:
    direction: str
    p_up: float
    confidence: float
    grade: str
    status: str
    source: str
    agreement: float
    model_maturity: float
    components: tuple[ForecastComponent, ...]
    reasons: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "p_up": round(self.p_up, 6),
            "confidence": round(self.confidence, 6),
            "grade": self.grade,
            "status": self.status,
            "source": self.source,
            "agreement": round(self.agreement, 6),
            "model_maturity": round(self.model_maturity, 6),
            "components": [component.to_dict() for component in self.components],
            "reasons": list(self.reasons),
        }


def build_research_forecast(
    *,
    model_p_up: Optional[float],
    external_p_up: Optional[float],
    ptb_model_p_up: Optional[float],
    ptb_heuristic_p_up: Optional[float],
    market_p_up: Optional[float],
    directional_vote: float,
    directional_consensus: float,
    predictability: float,
    conflict_score: float,
    model_markets: int,
    validated_signal: bool,
    maturity_target_markets: int = 120,
) -> ResearchForecast:
    """Build a robust research forecast, separate from the validated signal.

    Component weights are intentionally conservative:

    - Polymarket implied probability: 0.30
    - official PTB model/heuristic: up to 0.32 combined
    - deterministic feature consensus: 0.28
    - external-only model: at most 0.10
    - full B2 model: 0.04..0.20 while provisional, 0.28 when validated

    The values are ensemble policy weights, not claimed optimal parameters.  Their
    performance is recorded and must be evaluated out of sample before promotion.
    """
    target = max(1, int(maturity_target_markets))
    maturity = _clip(int(model_markets or 0) / target, 0.0, 1.0)
    components: list[ForecastComponent] = []

    def add_probability(name: str, value: Optional[float], weight: float, cap: float) -> None:
        if value is None or weight <= 0:
            return
        p = _clip(float(value), 0.0, 1.0)
        components.append(
            ForecastComponent(name=name, p_up=p, score=_probability_score(p, cap), weight=weight)
        )

    # The market is an honest baseline and a powerful late-window summary, but is
    # clipped so that it cannot single-handedly define the research forecast.
    add_probability("polymarket", market_p_up, 0.30, 0.90)

    # Prefer the trained PTB model when present, while retaining a smaller
    # deterministic PTB heuristic as an independent audit component.
    if ptb_model_p_up is not None:
        add_probability("ptb_model", ptb_model_p_up, 0.22, 0.85)
        add_probability("ptb_heuristic", ptb_heuristic_p_up, 0.10, 0.75)
    else:
        add_probability("ptb_heuristic", ptb_heuristic_p_up, 0.30, 0.80)

    vote = _clip(float(directional_vote or 0.0), -1.0, 1.0)
    consensus = _clip(float(directional_consensus or 0.0), 0.0, 1.0)
    if abs(vote) >= 0.02 and consensus > 0.0:
        feature_score = _clip(vote * max(0.25, consensus), -0.90, 0.90)
        components.append(
            ForecastComponent(
                name="feature_consensus",
                p_up=_clip(0.5 + 0.5 * feature_score, 0.0, 1.0),
                score=feature_score,
                weight=0.28,
            )
        )

    # Statistical models only gain influence gradually.  This is the key guard
    # against the observed young shared-model 0%/100% instability.
    model_age_factor = 0.25 + 0.75 * maturity
    add_probability(
        "external_model",
        external_p_up,
        0.10 * model_age_factor,
        0.70 if not validated_signal else 0.85,
    )
    full_model_weight = 0.28 if validated_signal else 0.04 + 0.16 * maturity
    add_probability(
        "full_model",
        model_p_up,
        full_model_weight,
        0.65 if not validated_signal else 0.88,
    )

    total_weight = sum(component.weight for component in components)
    if total_weight <= 0:
        return ResearchForecast(
            direction="NEUTRAL",
            p_up=0.5,
            confidence=0.0,
            grade="LOW",
            status="NO_DATA",
            source="ROBUST_ENSEMBLE_V1",
            agreement=0.0,
            model_maturity=maturity,
            components=tuple(),
            reasons=("no usable forecast component",),
        )

    pooled_score = sum(component.contribution for component in components) / total_weight
    pooled_score = _clip(pooled_score, -1.0, 1.0)
    if abs(pooled_score) < 1e-9:
        direction = "NEUTRAL"
    else:
        direction = "UP" if pooled_score > 0 else "DOWN"
    sign = 1 if pooled_score > 0 else (-1 if pooled_score < 0 else 0)

    directional_components = [component for component in components if abs(component.score) >= 0.08]
    directional_weight = sum(component.weight for component in directional_components)
    supporting_weight = sum(
        component.weight
        for component in directional_components
        if sign and component.score * sign > 0
    )
    agreement = supporting_weight / directional_weight if directional_weight > 0 else 0.0

    predictability = _clip(float(predictability or 0.0), 0.0, 1.0)
    conflict = _clip(float(conflict_score or 0.0), 0.0, 1.0)
    confidence = _clip(
        abs(pooled_score)
        * (0.40 + 0.60 * agreement)
        * (0.45 + 0.55 * predictability)
        * (1.0 - 0.55 * conflict),
        0.0,
        1.0,
    )

    # Convert the pooled directional score to a deliberately non-extreme
    # probability range.  Research forecasts never publish 0% or 100%.
    p_up = _clip(0.5 + 0.45 * pooled_score, 0.05, 0.95)

    if confidence >= 0.65 and agreement >= 0.72 and predictability >= 0.62:
        grade = "HIGH"
    elif confidence >= 0.34 and agreement >= 0.55:
        grade = "MEDIUM"
    else:
        grade = "LOW"

    if validated_signal:
        status = "VALIDATED"
    elif len(components) < 2:
        status = "LIMITED"
    elif conflict >= 0.65 or agreement < 0.50:
        status = "CONFLICTED"
        grade = "LOW"
    else:
        status = "PROVISIONAL"

    ordered = sorted(components, key=lambda item: abs(item.contribution), reverse=True)
    reasons = (
        f"pooled_score={pooled_score:+.3f}",
        f"agreement={agreement:.2f}",
        f"predictability={predictability:.2f}",
        f"model_maturity={maturity:.2f}",
        "top=" + ",".join(
            f"{item.name}:{item.contribution:+.3f}" for item in ordered[:4]
        ),
    )
    return ResearchForecast(
        direction=direction,
        p_up=p_up,
        confidence=confidence,
        grade=grade,
        status=status,
        source="ROBUST_ENSEMBLE_V1",
        agreement=agreement,
        model_maturity=maturity,
        components=tuple(components),
        reasons=reasons,
    )
