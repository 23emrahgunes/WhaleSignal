"""P2.2 predictability and market-regime engine.

The direction model is deliberately downstream of this module. This module first
answers whether the current market state is sufficiently coherent to forecast.
All inputs are normalized P2.1 features; missing history stays visible and causes a
fail-closed ABSTAIN rather than zero-imputed confidence.

No fitted weights, PnL claims, execution or private-key code lives here. The
heuristic component weights are explicit, versioned and returned in diagnostics so
P3 can later replace them with out-of-sample estimates.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from features import FeatureVector
from models import AbstainReason, Horizon, Regime

POLICY_VERSION = "P2.2-regime-v1"

MIN_HISTORY_SEC = {
    Horizon.H5M: 60.0,
    Horizon.H15M: 120.0,
    Horizon.H1H: 300.0,
}
MIN_PREDICTABILITY = {
    Horizon.H5M: 0.58,
    Horizon.H15M: 0.56,
    Horizon.H1H: 0.54,
}

PREDICTABILITY_WEIGHTS = {
    "coverage": 0.18,
    "agreement": 0.22,
    "direction_strength": 0.14,
    "momentum_persistence": 0.13,
    "flow_persistence": 0.10,
    "volatility_suitability": 0.10,
    "book_flow_quality": 0.07,
    "clob_quality": 0.06,
}


@dataclass
class RegimeResult:
    regime: Regime
    predictability: float
    abstain: bool
    abstain_reason: AbstainReason
    reasons: list[str] = field(default_factory=list)
    direction_score: float = 0.0
    agreement: float = 0.0
    conflict: float = 0.0
    components: dict[str, float] = field(default_factory=dict)
    available_groups: list[str] = field(default_factory=list)
    policy_version: str = POLICY_VERSION

    def to_dict(self) -> dict:
        return {
            "regime": self.regime.value,
            "predictability": round(self.predictability, 6),
            "abstain": self.abstain,
            "abstain_reason": self.abstain_reason.value,
            "direction_score": round(self.direction_score, 6),
            "agreement": round(self.agreement, 6),
            "conflict": round(self.conflict, 6),
            "components": {k: round(v, 6) for k, v in self.components.items()},
            "available_groups": list(self.available_groups),
            "reasons": list(self.reasons),
            "policy_version": self.policy_version,
        }


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _signed_tanh(value: Optional[float], scale: float) -> Optional[float]:
    if value is None or not math.isfinite(value):
        return None
    if scale <= 0:
        return 0.0
    return math.tanh(value / scale)


def _ret(fv: FeatureVector, window_ms: int) -> Optional[float]:
    raw = fv.ret_multi.get(str(window_ms)) if fv.ret_multi else None
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _mean(values: list[Optional[float]]) -> Optional[float]:
    good = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    return sum(good) / len(good) if good else None


def _direction_groups(fv: FeatureVector) -> dict[str, float]:
    """Build independent directional evidence groups in the common [-1, 1] scale."""
    ret5 = _ret(fv, 5_000)
    ret15 = _ret(fv, 15_000)
    ret60 = _ret(fv, 60_000)
    momentum = _mean([
        _signed_tanh(None if ret5 is None else ret5 * 10_000.0, 1.5),
        _signed_tanh(None if ret15 is None else ret15 * 10_000.0, 2.5),
        _signed_tanh(None if ret60 is None else ret60 * 10_000.0, 4.0),
    ])
    if momentum is not None:
        momentum *= 0.35 + 0.65 * _clip(fv.sign_persistence)

    flow = _mean([
        _signed_tanh(fv.flow_mid, 0.35),
        _signed_tanh(fv.flow_notional_5s, 0.35),
    ])
    if flow is not None:
        flow *= 0.40 + 0.60 * _clip(fv.flow_persistence)

    ptb = None
    if fv.has_reference:
        ptb = _mean([
            _signed_tanh(fv.distance_bps, 4.0),
            _signed_tanh(fv.distance_slope, 0.8),
        ])

    book = _mean([
        _signed_tanh(fv.obi_20, 0.35),
        _signed_tanh(fv.ofi, 0.35),
    ])

    clob = None
    if fv.has_clob and fv.up_mid is not None:
        clob = _mean([
            _signed_tanh((fv.up_mid - 0.5) * 2.0, 0.45),
            _signed_tanh(fv.up_mid_vel, 0.015),
        ])

    values = {
        "momentum": momentum,
        "flow": flow,
        "ptb": ptb,
        "book": book,
        "clob": clob,
    }
    return {k: _clip(float(v), -1.0, 1.0) for k, v in values.items() if v is not None}


def _coherence(groups: dict[str, float]) -> tuple[float, float, float]:
    active = {k: v for k, v in groups.items() if abs(v) >= 0.05}
    if not active:
        return 0.0, 0.0, 0.0
    total = sum(abs(v) for v in active.values())
    signed = sum(active.values())
    direction = _clip(signed / total, -1.0, 1.0) if total > 0 else 0.0
    majority_sign = 1.0 if signed >= 0 else -1.0
    aligned = sum(abs(v) for v in active.values() if v * majority_sign > 0)
    opposed = total - aligned
    agreement = aligned / total if total else 0.0
    conflict = _clip((2.0 * opposed) / total) if total else 0.0
    return direction, agreement, conflict


def _clob_quality(fv: FeatureVector) -> float:
    if not fv.has_clob:
        return 0.0
    residual_penalty = _clip(abs(fv.clob_complement_residual) / 0.08)
    spread_penalty = _clip(fv.clob_spread / 0.20)
    return _clip(1.0 - 0.55 * residual_penalty - 0.45 * spread_penalty)


def _volatility_suitability(fv: FeatureVector) -> float:
    percentile_fit = 1.0 - _clip(abs(fv.vol_percentile - 0.55) / 0.45)
    accel_fit = 1.0 - _clip(max(0.0, fv.vol_accel - 1.0) / 3.0)
    return _clip(0.65 * percentile_fit + 0.35 * accel_fit)


def _unsafe_result(fv: FeatureVector, reasons: list[str]) -> RegimeResult:
    return RegimeResult(
        regime=Regime.UNSAFE,
        predictability=0.0,
        abstain=True,
        abstain_reason=AbstainReason.INSUFFICIENT_DATA,
        reasons=reasons,
        components={"coverage": _clip(fv.feature_coverage)},
    )


def classify_regime(fv: Optional[FeatureVector]) -> RegimeResult:
    """Classify regime and decide whether direction inference is allowed."""
    if fv is None:
        return RegimeResult(
            Regime.UNSAFE, 0.0, True, AbstainReason.INSUFFICIENT_DATA,
            ["feature_vector_missing"],
        )

    min_history = MIN_HISTORY_SEC[fv.combo.horizon]
    missing: list[str] = []
    if fv.price_history_span_sec < min_history:
        missing.append(f"history<{min_history:.0f}s")
    if fv.feature_coverage < 0.65:
        missing.append(f"coverage={fv.feature_coverage:.2f}")
    if not fv.has_reference:
        missing.append("reference_missing")
    if not fv.has_clob:
        missing.append("clob_missing")
    if fv.rv_slow <= 0.0:
        missing.append("rv_60s_missing")
    if missing:
        return _unsafe_result(fv, missing)

    if fv.vol_percentile >= 0.97 or fv.vol_accel >= 4.0:
        return RegimeResult(
            Regime.HIGH_VOL, 0.05, True, AbstainReason.HIGH_VOL,
            [f"vol_pct={fv.vol_percentile:.2f}", f"vol_accel={fv.vol_accel:.2f}"],
        )

    clob_quality = _clob_quality(fv)
    if (
        (fv.flip_rate >= 0.58 and fv.vol_accel >= 1.8)
        or abs(fv.clob_complement_residual) >= 0.10
        or fv.clob_spread >= 0.24
    ):
        return RegimeResult(
            Regime.CHAOTIC, 0.08, True, AbstainReason.CHAOTIC,
            [
                f"flip={fv.flip_rate:.2f}",
                f"vol_accel={fv.vol_accel:.2f}",
                f"clob_residual={fv.clob_complement_residual:+.3f}",
                f"clob_spread={fv.clob_spread:.3f}",
            ],
        )

    groups = _direction_groups(fv)
    direction, agreement, conflict = _coherence(groups)
    available = sorted(groups)
    if len(available) < 3:
        return RegimeResult(
            Regime.UNSAFE, 0.0, True, AbstainReason.INSUFFICIENT_DATA,
            [f"direction_groups={len(available)}<3"],
            direction_score=direction,
            agreement=agreement,
            conflict=conflict,
            available_groups=available,
        )

    if conflict >= 0.55:
        return RegimeResult(
            Regime.CHOP, 0.15, True, AbstainReason.FEATURE_CONFLICT,
            [f"conflict={conflict:.2f}", f"groups={groups}"],
            direction_score=direction,
            agreement=agreement,
            conflict=conflict,
            available_groups=available,
        )

    trend = (
        abs(direction) >= 0.30
        and agreement >= 0.64
        and fv.sign_persistence >= 0.58
        and fv.flip_rate <= 0.48
    )
    if trend:
        regime = Regime.TREND_UP if direction > 0 else Regime.TREND_DOWN
    else:
        regime = Regime.CHOP

    components = {
        "coverage": _clip(fv.feature_coverage),
        "agreement": _clip(agreement),
        "direction_strength": _clip(abs(direction)),
        "momentum_persistence": _clip(fv.sign_persistence),
        "flow_persistence": _clip(fv.flow_persistence),
        "volatility_suitability": _volatility_suitability(fv),
        "book_flow_quality": _clip(0.5 + 0.5 * fv.book_flow_agree),
        "clob_quality": clob_quality,
    }
    predictability = sum(
        components[name] * weight for name, weight in PREDICTABILITY_WEIGHTS.items()
    )
    predictability = _clip(predictability)

    reasons = [
        f"direction={direction:+.2f}",
        f"agreement={agreement:.2f}",
        f"conflict={conflict:.2f}",
        f"coverage={fv.feature_coverage:.2f}",
    ]
    threshold = MIN_PREDICTABILITY[fv.combo.horizon]
    if predictability < threshold:
        reasons.append(f"predictability={predictability:.2f}<{threshold:.2f}")
        return RegimeResult(
            regime, predictability, True, AbstainReason.LOW_PREDICTABILITY,
            reasons, direction, agreement, conflict, components, available,
        )

    return RegimeResult(
        regime, predictability, False, AbstainReason.NONE,
        reasons, direction, agreement, conflict, components, available,
    )
