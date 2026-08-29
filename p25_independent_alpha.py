"""Independent PTB + Binance alpha for the 5m paper experiment.

The alpha is deliberately independent from Polymarket pricing. Official opening PTB
and, when fresh, the current official reference define where the market stands.
Binance is used only to estimate the incremental move over the remaining seconds.
If the current official reference is unavailable, a basis-adjusted Binance proxy is
allowed only when the opening basis is known and sane.

This module does not submit orders and does not read Polymarket CLOB prices.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(float(z) / math.sqrt(2.0)))


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _rv(fv, window_ms: int) -> float | None:  # noqa: ANN001
    raw = (getattr(fv, "rv_multi", {}) or {}).get(str(window_ms))
    value = _safe_float(raw)
    return value if value is not None and value > 0 else None


def _ret_bps(fv, window_ms: int) -> float:  # noqa: ANN001
    raw = (getattr(fv, "ret_multi", {}) or {}).get(str(window_ms))
    value = _safe_float(raw)
    return 10000.0 * value if value is not None else 0.0


def _remaining_sigma_bps(fv, tte_sec: float) -> float | None:  # noqa: ANN001
    """Forecast remaining integrated volatility from short/medium realized vol.

    features.realized_vol returns sqrt(sum(log-return^2)) over each window. Convert
    each observation to a per-sqrt-second rate, blend with more weight on the recent
    window, then scale by sqrt(T_rem). No future data is used.
    """
    observations: list[tuple[float, float]] = []
    for window_ms, weight in ((5000, 0.45), (30000, 0.35), (60000, 0.20)):
        rv = _rv(fv, window_ms)
        if rv is None:
            continue
        window_sec = window_ms / 1000.0
        observations.append((rv / math.sqrt(window_sec), weight))
    if not observations:
        return None
    total_weight = sum(weight for _, weight in observations)
    sigma_per_sqrt_sec = sum(rate * weight for rate, weight in observations) / total_weight
    remaining = sigma_per_sqrt_sec * math.sqrt(max(1.0, float(tte_sec)))
    return max(1e-6, remaining * 10000.0)


def _binance_pressure(fv, sigma_remaining_bps: float, max_sigma_shift: float) -> tuple[float, float, float]:  # noqa: ANN001,E501
    """Return bounded expected-move correction and its momentum/flow sub-scores."""
    r5 = _ret_bps(fv, 5000)
    r15 = _ret_bps(fv, 15000)
    r30 = _ret_bps(fv, 30000)
    momentum_bps = 0.50 * r5 + 0.30 * r15 + 0.20 * r30
    momentum_score = math.tanh(momentum_bps / max(1.0, sigma_remaining_bps))

    flow_score = _clip(
        0.18 * float(getattr(fv, "flow_fast", 0.0) or 0.0)
        + 0.30 * float(getattr(fv, "flow_mid", 0.0) or 0.0)
        + 0.14 * float(getattr(fv, "flow_slow", 0.0) or 0.0)
        + 0.20 * float(getattr(fv, "obi_20", 0.0) or 0.0)
        + 0.18 * float(getattr(fv, "ofi", 0.0) or 0.0),
        -1.0,
        1.0,
    )
    pressure_score = _clip(0.60 * momentum_score + 0.40 * flow_score, -1.0, 1.0)
    correction_bps = float(max_sigma_shift) * sigma_remaining_bps * pressure_score
    return correction_bps, momentum_score, flow_score


@dataclass(frozen=True)
class IndependentAlpha:
    ready: bool
    reason: str
    source: str
    direction: str
    p_up: float | None
    confidence: float
    grade: str
    anchor_source: str | None
    official_ptb: float | None
    current_equivalent: float | None
    basis_bps: float | None
    distance_bps: float | None
    sigma_remaining_bps: float | None
    binance_correction_bps: float | None
    expected_end_distance_bps: float | None
    z_terminal: float | None
    momentum_score: float | None
    flow_score: float | None

    def to_dict(self) -> dict[str, Any]:
        def rounded(value: float | None, digits: int = 6):
            return round(float(value), digits) if value is not None else None

        return {
            "ready": self.ready,
            "reason": self.reason,
            "source": self.source,
            "direction": self.direction,
            "p_up": rounded(self.p_up),
            "confidence": rounded(self.confidence),
            "grade": self.grade,
            "anchor_source": self.anchor_source,
            "official_ptb": rounded(self.official_ptb),
            "current_equivalent": rounded(self.current_equivalent),
            "basis_bps": rounded(self.basis_bps, 3),
            "distance_bps": rounded(self.distance_bps, 3),
            "sigma_remaining_bps": rounded(self.sigma_remaining_bps, 3),
            "binance_correction_bps": rounded(self.binance_correction_bps, 3),
            "expected_end_distance_bps": rounded(self.expected_end_distance_bps, 3),
            "z_terminal": rounded(self.z_terminal, 4),
            "momentum_score": rounded(self.momentum_score, 4),
            "flow_score": rounded(self.flow_score, 4),
        }


def _not_ready(reason: str, *, official_ptb: float | None = None) -> IndependentAlpha:
    return IndependentAlpha(
        ready=False,
        reason=reason,
        source="INDEPENDENT_PTB_BINANCE_V1",
        direction="ABSTAIN",
        p_up=None,
        confidence=0.0,
        grade="LOW",
        anchor_source=None,
        official_ptb=official_ptb,
        current_equivalent=None,
        basis_bps=None,
        distance_bps=None,
        sigma_remaining_bps=None,
        binance_correction_bps=None,
        expected_end_distance_bps=None,
        z_terminal=None,
        momentum_score=None,
        flow_score=None,
    )


def build_independent_alpha(*, ref, snap, fv, cfg) -> IndependentAlpha:  # noqa: ANN001
    """Build a 5m independent terminal-above-PTB probability.

    Polymarket up/down prices are intentionally never referenced here. Readiness is
    based only on the Binance features this alpha actually consumes; the generic
    FeatureVector.feature_ready flag is deliberately NOT used because that flag also
    requires Polymarket CLOB availability.
    """
    if str(ref.combo.horizon.value).lower() != "5m":
        return _not_ready("HORIZON_NOT_5M")
    if fv is None:
        return _not_ready("FEATURES_MISSING")
    missing = set(getattr(fv, "missing_features", []) or [])
    required = {"binance_book", "ret_5s", "ret_60s", "flow_5s", "rv_60s"}
    blocking = sorted(missing.intersection(required))
    if blocking:
        return _not_ready("BINANCE_FEATURES_MISSING_" + "_".join(blocking).upper())

    tte_raw = snap.tte_sec if snap.tte_sec is not None else snap.seconds_remaining
    tte = _safe_float(tte_raw)
    if tte is None or tte <= 0:
        return _not_ready("TTE_INVALID")

    official_ptb = _safe_float(getattr(ref, "official_reference_open", None))
    if official_ptb is None or official_ptb <= 0:
        return _not_ready("OFFICIAL_PTB_MISSING")

    current: float | None = None
    anchor_source: str | None = None
    basis_bps: float | None = None

    official_current = _safe_float(getattr(ref, "reference_current", None))
    official_age_ms = _safe_float(getattr(ref, "reference_current_age_ms", None))
    max_reference_age_ms = float(getattr(cfg, "max_reference_age_ms", 8000.0))
    if (
        official_current is not None
        and official_current > 0
        and official_age_ms is not None
        and official_age_ms <= max_reference_age_ms
    ):
        current = official_current
        anchor_source = "OFFICIAL_CURRENT"
    else:
        spot = _safe_float(getattr(snap, "spot_price", None))
        spot_age_ms = _safe_float(getattr(snap, "spot_age_ms", None))
        proxy_open = _safe_float(getattr(ref, "proxy_reference_open", None))
        official_open_time = _safe_float(getattr(ref, "official_reference_open_time", None))
        proxy_open_time = _safe_float(getattr(ref, "proxy_reference_open_time", None))
        max_spot_age_ms = float(getattr(cfg, "max_spot_age_ms", 2500.0))
        max_gap_ms = float(getattr(cfg, "paper_independent_max_basis_open_gap_ms", 5000.0))
        max_basis_bps = float(getattr(cfg, "paper_independent_max_basis_bps", 50.0))
        if spot is None or spot <= 0 or spot_age_ms is None or spot_age_ms > max_spot_age_ms:
            return _not_ready("CURRENT_REFERENCE_AND_FRESH_SPOT_MISSING", official_ptb=official_ptb)
        if proxy_open is None or proxy_open <= 0:
            return _not_ready("PROXY_OPEN_MISSING", official_ptb=official_ptb)
        if official_open_time is None or proxy_open_time is None:
            return _not_ready("BASIS_OPEN_TIME_MISSING", official_ptb=official_ptb)
        open_gap_ms = abs(official_open_time - proxy_open_time) * 1000.0
        if open_gap_ms > max_gap_ms:
            return _not_ready("BASIS_OPEN_TIME_GAP", official_ptb=official_ptb)
        basis_bps = 10000.0 * math.log(proxy_open / official_ptb)
        if abs(basis_bps) > max_basis_bps:
            return _not_ready("BASIS_TOO_LARGE", official_ptb=official_ptb)
        current = spot * (official_ptb / proxy_open)
        anchor_source = "BINANCE_BASIS_ADJUSTED"

    sigma_remaining_bps = _remaining_sigma_bps(fv, tte)
    if sigma_remaining_bps is None or sigma_remaining_bps <= 0:
        return _not_ready("REMAINING_VOL_MISSING", official_ptb=official_ptb)

    distance_bps = 10000.0 * math.log(current / official_ptb)
    max_sigma_shift = float(getattr(cfg, "paper_independent_binance_max_sigma_shift", 0.35))
    correction_bps, momentum_score, flow_score = _binance_pressure(
        fv,
        sigma_remaining_bps,
        max_sigma_shift,
    )
    expected_end_distance_bps = distance_bps + correction_bps
    z_terminal = expected_end_distance_bps / max(sigma_remaining_bps, 1e-9)
    p_up = _clip(_normal_cdf(z_terminal), 0.01, 0.99)

    low = float(getattr(cfg, "paper_independent_deadzone_low", 0.42))
    high = float(getattr(cfg, "paper_independent_deadzone_high", 0.58))
    if p_up < low:
        direction = "DOWN"
    elif p_up > high:
        direction = "UP"
    else:
        direction = "NEUTRAL"

    confidence = _clip(2.0 * abs(p_up - 0.5), 0.0, 1.0)
    if confidence >= 0.65:
        grade = "HIGH"
    elif confidence >= 0.34:
        grade = "MEDIUM"
    else:
        grade = "LOW"

    return IndependentAlpha(
        ready=True,
        reason="OK" if direction != "NEUTRAL" else "DEADZONE_NEUTRAL",
        source="INDEPENDENT_PTB_BINANCE_V1",
        direction=direction,
        p_up=p_up,
        confidence=confidence,
        grade=grade,
        anchor_source=anchor_source,
        official_ptb=official_ptb,
        current_equivalent=current,
        basis_bps=basis_bps,
        distance_bps=distance_bps,
        sigma_remaining_bps=sigma_remaining_bps,
        binance_correction_bps=correction_bps,
        expected_end_distance_bps=expected_end_distance_bps,
        z_terminal=z_terminal,
        momentum_score=momentum_score,
        flow_score=flow_score,
    )
