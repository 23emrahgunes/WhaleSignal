"""P2.2 predictability and market-regime engine.

The engine answers "is this market predictable now?" before any direction model is
allowed to speak. It is deterministic, auditable, horizon-aware and uses only
features available at the current timestamp. No future labels or model weights are
used here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from features import FeatureVector
from models import AbstainReason, Horizon, Regime


@dataclass
class RegimeResult:
    regime: Regime
    predictability: float
    abstain: bool
    abstain_reason: AbstainReason
    reasons: list[str] = field(default_factory=list)
    conflict_score: float = 0.0
    directional_consensus: float = 0.0
    directional_vote: float = 0.0
    components: dict[str, float] = field(default_factory=dict)
    active_signals: int = 0
    version: str = "PREDICTABILITY_V2"


@dataclass(frozen=True)
class _Policy:
    min_predictability: float
    high_vol_percentile: float
    chaos_flip: float
    chaos_vol_accel: float
    conflict_limit: float
    trend_persistence: float
    trend_max_flip: float


_POLICIES = {
    Horizon.H5M: _Policy(0.60, 0.96, 0.64, 2.20, 0.46, 0.64, 0.42),
    Horizon.H15M: _Policy(0.58, 0.965, 0.66, 2.35, 0.48, 0.62, 0.44),
    Horizon.H1H: _Policy(0.56, 0.975, 0.70, 2.60, 0.52, 0.60, 0.46),
}


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _sign(x: float, deadband: float = 0.0) -> int:
    if x > deadband:
        return 1
    if x < -deadband:
        return -1
    return 0


def _tanh_strength(x: float, scale: float) -> float:
    if scale <= 0:
        return 0.0
    return _clip01(math.tanh(abs(x) / scale))


def _directional_signals(fv: FeatureVector) -> list[tuple[str, int, float]]:
    """Return (name, sign, strength) using current-time features only."""
    out: list[tuple[str, int, float]] = []

    mom_signal = fv.mom_vol_ratio if abs(fv.mom_vol_ratio) > 1e-12 else fv.ret_slow * 1000.0
    mom_sign = _sign(mom_signal, 0.02)
    if mom_sign:
        strength = _tanh_strength(mom_signal, 1.5)
        strength *= 0.45 + 0.55 * _clip01(fv.sign_persistence)
        out.append(("momentum", mom_sign, strength))

    flow_sign = _sign(fv.flow_mid, 0.08)
    if flow_sign:
        strength = _tanh_strength(fv.flow_mid, 0.45)
        strength *= 0.55 + 0.45 * _clip01(fv.flow_persistence)
        out.append(("aggressive_flow", flow_sign, strength))

    ptb_signal = fv.ptb_z if abs(fv.ptb_z) > 1e-12 else fv.distance_bps / 5.0
    ptb_sign = _sign(ptb_signal, 0.08)
    if ptb_sign:
        out.append(("ptb", ptb_sign, _tanh_strength(ptb_signal, 1.25)))

    book_signal = 0.65 * fv.obi_20 + 0.35 * fv.ofi
    book_sign = _sign(book_signal, 0.06)
    if book_sign:
        out.append(("binance_book", book_sign, _tanh_strength(book_signal, 0.40)))

    clob_sign = _sign(fv.up_mid_vel, 0.001)
    if clob_sign:
        out.append(("clob_velocity", clob_sign, _tanh_strength(fv.up_mid_vel, 0.025)))

    return [(name, sign, max(0.05, strength)) for name, sign, strength in out]


def _consensus(signals: list[tuple[str, int, float]]) -> tuple[float, float, float]:
    if not signals:
        return 0.0, 0.0, 0.0
    total = sum(weight for _, _, weight in signals)
    vote = sum(sign * weight for _, sign, weight in signals) / max(total, 1e-12)
    consensus = abs(vote)
    pos = sum(weight for _, sign, weight in signals if sign > 0)
    neg = sum(weight for _, sign, weight in signals if sign < 0)
    conflict = 0.0 if pos == 0 or neg == 0 else 2.0 * min(pos, neg) / max(total, 1e-12)
    return vote, _clip01(consensus), _clip01(conflict)


def _data_sufficient(fv: FeatureVector) -> tuple[bool, list[str]]:
    missing = list(getattr(fv, "missing_features", []) or [])
    coverage = float(getattr(fv, "feature_coverage", 0.0) or 0.0)
    if missing:
        return False, [f"missing={','.join(missing)}"]
    if coverage > 0 and coverage < 0.65:
        return False, [f"feature_coverage={coverage:.2f}"]

    numeric_empty = (
        fv.rv_slow <= 0
        and fv.rv_fast <= 0
        and fv.ret_slow == 0.0
        and fv.flow_mid == 0.0
        and fv.distance_bps == 0.0
        and fv.up_mid_vel == 0.0
    )
    if numeric_empty:
        return False, ["ham veri yetersiz"]
    return True, []


def classify_regime(
    fv: FeatureVector,
    min_predictability: float | None = None,
) -> RegimeResult:
    policy = _POLICIES.get(fv.combo.horizon, _POLICIES[Horizon.H15M])
    min_pred = policy.min_predictability if min_predictability is None else min_predictability
    reasons: list[str] = []

    sufficient, insuff_reasons = _data_sufficient(fv)
    if not sufficient:
        result = RegimeResult(
            Regime.UNSAFE,
            0.0,
            True,
            AbstainReason.INSUFFICIENT_DATA,
            insuff_reasons,
        )
        _attach(fv, result)
        return result

    signals = _directional_signals(fv)
    vote, consensus, conflict = _consensus(signals)

    coverage = float(getattr(fv, "feature_coverage", 0.0) or 0.0)
    if coverage <= 0:
        coverage = 1.0
    persistence = _clip01(
        0.58 * fv.sign_persistence + 0.42 * (1.0 - _clip01(fv.flip_rate))
    )
    flow_quality = _clip01(
        0.58 * fv.flow_persistence + 0.42 * abs(fv.flow_mid)
    )
    book_agreement = _clip01(0.5 + 0.5 * fv.book_flow_agree)
    clob_agreement = (
        _clip01(0.5 + 0.5 * fv.clob_spot_agree) if fv.has_clob else 0.5
    )
    spread_quality = 1.0 - _clip01(max(0.0, fv.clob_spread) / 0.14)
    complement_quality = 1.0 - _clip01(
        abs(fv.clob_complement_residual) / 0.08
    )
    vol_percentile = _clip01(fv.vol_percentile)
    if vol_percentile <= 0.82:
        volatility_quality = 1.0 - 0.30 * abs(vol_percentile - 0.52) / 0.52
    else:
        volatility_quality = max(0.0, 1.0 - (vol_percentile - 0.82) / 0.18)
    volatility_quality = _clip01(volatility_quality)

    components = {
        "coverage": _clip01(coverage),
        "persistence": persistence,
        "flow_quality": flow_quality,
        "directional_consensus": consensus,
        "book_agreement": book_agreement,
        "clob_agreement": clob_agreement,
        "spread_quality": spread_quality,
        "complement_quality": complement_quality,
        "volatility_quality": volatility_quality,
    }

    predictability = _clip01(
        0.16 * components["coverage"]
        + 0.16 * persistence
        + 0.12 * flow_quality
        + 0.20 * consensus
        + 0.08 * book_agreement
        + 0.08 * clob_agreement
        + 0.07 * spread_quality
        + 0.05 * complement_quality
        + 0.08 * volatility_quality
    )

    if (
        vol_percentile >= policy.high_vol_percentile
        or (
            fv.vol_accel >= policy.chaos_vol_accel
            and vol_percentile >= 0.90
        )
    ):
        reasons.append(
            f"high_vol pct={vol_percentile:.2f} accel={fv.vol_accel:.2f}"
        )
        result = RegimeResult(
            Regime.HIGH_VOL,
            min(predictability, 0.20),
            True,
            AbstainReason.HIGH_VOL,
            reasons,
            conflict,
            consensus,
            vote,
            components,
            len(signals),
        )
        _attach(fv, result)
        return result

    if (
        fv.flip_rate >= policy.chaos_flip
        and (fv.vol_accel >= 1.35 or consensus < 0.32)
    ):
        reasons.append(
            f"chaotic flip={fv.flip_rate:.2f} consensus={consensus:.2f}"
        )
        result = RegimeResult(
            Regime.CHAOTIC,
            min(predictability, 0.18),
            True,
            AbstainReason.CHAOTIC,
            reasons,
            conflict,
            consensus,
            vote,
            components,
            len(signals),
        )
        _attach(fv, result)
        return result

    if len(signals) >= 2 and conflict >= policy.conflict_limit:
        reasons.append(
            "feature conflict "
            + ",".join(f"{n}:{'+' if s > 0 else '-'}" for n, s, _ in signals)
        )
        result = RegimeResult(
            Regime.CHOP,
            min(predictability, 0.30),
            True,
            AbstainReason.FEATURE_CONFLICT,
            reasons,
            conflict,
            consensus,
            vote,
            components,
            len(signals),
        )
        _attach(fv, result)
        return result

    is_trend = (
        fv.sign_persistence >= policy.trend_persistence
        and fv.flip_rate <= policy.trend_max_flip
        and consensus >= 0.35
    )
    if is_trend and vote != 0:
        regime = Regime.TREND_UP if vote > 0 else Regime.TREND_DOWN
        reasons.append(
            f"trend vote={vote:+.2f} persist={fv.sign_persistence:.2f} "
            f"flip={fv.flip_rate:.2f}"
        )
    else:
        regime = Regime.CHOP
        reasons.append(
            f"chop consensus={consensus:.2f} persist={fv.sign_persistence:.2f}"
        )

    if predictability < min_pred:
        reasons.append(f"predictability={predictability:.2f}<{min_pred:.2f}")
        result = RegimeResult(
            regime,
            predictability,
            True,
            AbstainReason.LOW_PREDICTABILITY,
            reasons,
            conflict,
            consensus,
            vote,
            components,
            len(signals),
        )
        _attach(fv, result)
        return result

    result = RegimeResult(
        regime,
        predictability,
        False,
        AbstainReason.NONE,
        reasons,
        conflict,
        consensus,
        vote,
        components,
        len(signals),
    )
    _attach(fv, result)
    return result


def _attach(fv: FeatureVector, result: RegimeResult) -> None:
    """Attach an auditable trace for recorder/dashboard without changing schema."""
    fv.predictability_score = result.predictability
    fv.predictability_version = result.version
    fv.predictability_components = dict(result.components)
    fv.conflict_score = result.conflict_score
    fv.directional_consensus = result.directional_consensus
    fv.directional_vote = result.directional_vote
    fv.regime_name = result.regime.value
    fv.regime_abstain = result.abstain
    fv.regime_abstain_reason = result.abstain_reason.value
    fv.regime_reasons = list(result.reasons)
