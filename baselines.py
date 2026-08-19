"""Honest baseline probabilities for P2.3 shadow evaluation.

Baselines are deliberately simple and never fitted on the evaluation row:
- coin flip: 0.50,
- PTB diffusion: probability that spot finishes above the official opening anchor
  under a zero-drift Gaussian approximation using observed 60s realized volatility,
- Polymarket implied: current UP midpoint.

They are analytics comparators, not trading signals and never place orders.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist
from typing import Optional

from features import FeatureVector

BASELINE_VERSION = "P2.3-baselines-v1"
_NORMAL = NormalDist()


def _finite(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _clip_probability(value: Optional[float]) -> Optional[float]:
    value = _finite(value)
    if value is None:
        return None
    return max(1e-6, min(1.0 - 1e-6, value))


def ptb_diffusion_probability(fv: FeatureVector) -> Optional[float]:
    """P(final spot > PTB) under a transparent zero-drift diffusion baseline.

    ``rv_slow`` is 60s realized log-volatility.  It is scaled by square-root time
    to the remaining market horizon.  A small sigma floor prevents false certainty
    during quiet or just-warmed periods.  The baseline is intentionally uncalibrated;
    P2.4 measures and calibrates model probabilities separately.
    """
    if not fv.has_reference:
        return None
    distance_bps = _finite(fv.distance_bps)
    rv_60s = _finite(fv.rv_slow)
    remaining = _finite(fv.seconds_remaining)
    if distance_bps is None or rv_60s is None or remaining is None or rv_60s <= 0:
        return None
    rv_bps_60s = rv_60s * 10_000.0
    remaining_sigma_bps = rv_bps_60s * math.sqrt(max(5.0, remaining) / 60.0)
    remaining_sigma_bps = max(0.75, remaining_sigma_bps)
    z = max(-8.0, min(8.0, distance_bps / remaining_sigma_bps))
    return _clip_probability(_NORMAL.cdf(z))


@dataclass(frozen=True)
class BaselineOutput:
    coinflip: float = 0.5
    ptb_diffusion: Optional[float] = None
    market_implied: Optional[float] = None
    version: str = BASELINE_VERSION

    def to_dict(self) -> dict:
        return {
            "coinflip": round(self.coinflip, 8),
            "ptb_diffusion": (
                round(self.ptb_diffusion, 8) if self.ptb_diffusion is not None else None
            ),
            "market_implied": (
                round(self.market_implied, 8) if self.market_implied is not None else None
            ),
            "version": self.version,
        }


def baseline_probabilities(fv: FeatureVector) -> BaselineOutput:
    market = _clip_probability(fv.up_mid) if fv.has_clob else None
    return BaselineOutput(
        coinflip=0.5,
        ptb_diffusion=ptb_diffusion_probability(fv),
        market_implied=market,
    )
