"""Pure strategy math for the DUAL40 maker-recovery cohort.

PAPER simulates ordinary equal-share 40-cent BUY limits with relaxed entry controls
and settles unmatched exposure conservatively at market expiry without waiting for
the official outcome. LIVE retains balanced, confirmed, post-only entry. The
strategy uses asset-scoped recovery ladders (5 -> 10 -> 30 shares) and permanently
hard-stops only the affected asset when its realized loss pool can no longer be
recovered by a fully matched 30-share pair.

This module performs no I/O, signing or order submission.  It is intentionally pure
so regime gates, partial-fill PnL and ladder transitions are deterministic and easy
to audit.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence


DUAL40_STRATEGY = "DUAL40_MAKER_RECOVERY_V1"
DEFAULT_LADDER = (5.0, 10.0, 30.0)


@dataclass(frozen=True)
class MidPoint:
    ts_ms: int
    mid: float


@dataclass(frozen=True)
class Dual40Policy:
    price: float = 0.40
    ladder: tuple[float, ...] = DEFAULT_LADDER
    min_market_age_sec: float = 30.0
    min_tte_sec: float = 90.0
    lookback_sec: float = 20.0
    confirm_sec: float = 5.0
    balanced_mid_low: float = 0.44
    balanced_mid_high: float = 0.56
    max_mid_range: float = 0.10
    max_net_drift: float = 0.04
    max_abs_slope_per_sec: float = 0.0030
    max_one_way_ratio: float = 0.72
    max_single_jump: float = 0.06
    max_complement_residual: float = 0.04
    max_spread_each: float = 0.10
    cancel_tte_sec: float = 40.0

    def validate(self) -> None:
        if not 0.01 <= self.price <= 0.49:
            raise ValueError("DUAL40 maker price must be between 0.01 and 0.49")
        if not self.ladder or any(q <= 0 for q in self.ladder):
            raise ValueError("DUAL40 ladder must contain positive shares")
        if tuple(sorted(self.ladder)) != self.ladder:
            raise ValueError("DUAL40 ladder must be ascending")
        if self.min_market_age_sec < self.lookback_sec:
            raise ValueError("market age must cover the regime lookback")
        if self.min_tte_sec <= self.cancel_tte_sec:
            raise ValueError("entry minimum TTE must be above cancel TTE")
        if self.confirm_sec <= 0:
            raise ValueError("confirmation duration must be positive")
        if not 0.0 <= self.max_one_way_ratio <= 1.0:
            raise ValueError("one-way ratio must be in [0,1]")
        if self.balanced_mid_low >= self.balanced_mid_high:
            raise ValueError("balanced mid bounds are invalid")

    @property
    def pair_edge_per_share(self) -> float:
        return 1.0 - 2.0 * float(self.price)

    @property
    def maximum_recoverable_loss(self) -> float:
        return self.pair_edge_per_share * float(self.ladder[-1])

    @property
    def full_ladder_capital(self) -> float:
        # Capital required after losing the 5 and 10 share single-leg rounds, then
        # posting both 30-share legs. At 40 cents this is $2 + $4 + $24 = $30.
        prior_single_leg_losses = sum(float(q) * self.price for q in self.ladder[:-1])
        final_pair_reserve = 2.0 * self.price * float(self.ladder[-1])
        return prior_single_leg_losses + final_pair_reserve


@dataclass(frozen=True)
class RegimeDecision:
    eligible: bool
    reason: str
    score: float
    up_mid: float | None
    down_mid: float | None
    mid_range: float | None
    net_drift: float | None
    slope_per_sec: float | None
    one_way_ratio: float | None
    max_jump: float | None
    complement_residual: float | None
    history_span_sec: float

    def to_dict(self) -> dict:
        return {
            "eligible": self.eligible,
            "reason": self.reason,
            "score": round(float(self.score), 4),
            "up_mid": self.up_mid,
            "down_mid": self.down_mid,
            "mid_range": self.mid_range,
            "net_drift": self.net_drift,
            "slope_per_sec": self.slope_per_sec,
            "one_way_ratio": self.one_way_ratio,
            "max_jump": self.max_jump,
            "complement_residual": self.complement_residual,
            "history_span_sec": round(float(self.history_span_sec), 3),
        }


@dataclass(frozen=True)
class OpeningGateProfile:
    observation_sec: float
    max_mid_range: float
    max_net_drift: float
    max_one_way_ratio: float
    max_spread_each: float
    forecast_min_p_up: float
    forecast_max_p_up: float
    max_abs_slope_per_sec: float = 0.0030
    max_single_jump: float = 0.06
    max_complement_residual: float = 0.04
    max_queue_imbalance: float = 3.0
    min_depth_balance_ratio: float = 0.75

    def validate(self) -> None:
        if self.observation_sec <= 0:
            raise ValueError("opening observation must be positive")
        if min(self.max_mid_range, self.max_net_drift, self.max_spread_each) <= 0:
            raise ValueError("opening range/drift/spread limits must be positive")
        if not 0.0 <= self.max_one_way_ratio <= 1.0:
            raise ValueError("opening one-way ratio must be in [0,1]")
        if not 0.0 <= self.forecast_min_p_up < self.forecast_max_p_up <= 1.0:
            raise ValueError("opening forecast band is invalid")
        if self.max_queue_imbalance < 1.0:
            raise ValueError("opening queue imbalance must be >= 1")
        if not 0.0 <= self.min_depth_balance_ratio <= 1.0:
            raise ValueError("opening depth balance ratio must be in [0,1]")


@dataclass(frozen=True)
class OpeningStabilityDecision:
    eligible: bool
    reason: str
    observation_sec: float
    history_span_sec: float
    mid_range: float | None = None
    net_drift: float | None = None
    slope_per_sec: float | None = None
    one_way_ratio: float | None = None
    max_jump: float | None = None
    complement_residual: float | None = None
    max_spread: float | None = None
    queue_imbalance: float | None = None
    depth_balance_ratio: float | None = None

    def to_dict(self) -> dict:
        return {
            "eligible": self.eligible,
            "reason": self.reason,
            "observation_sec": self.observation_sec,
            "history_span_sec": round(float(self.history_span_sec), 3),
            "mid_range": self.mid_range,
            "net_drift": self.net_drift,
            "slope_per_sec": self.slope_per_sec,
            "one_way_ratio": self.one_way_ratio,
            "max_jump": self.max_jump,
            "complement_residual": self.complement_residual,
            "max_spread": self.max_spread,
            "queue_imbalance": self.queue_imbalance,
            "depth_balance_ratio": self.depth_balance_ratio,
        }


@dataclass(frozen=True)
class LadderDecision:
    level_index: int
    target_shares: float
    loss_pool: float
    hard_stopped: bool
    reason: str

    def to_dict(self) -> dict:
        return {
            "level_index": self.level_index,
            "target_shares": self.target_shares,
            "loss_pool": round(self.loss_pool, 6),
            "hard_stopped": self.hard_stopped,
            "reason": self.reason,
        }


def _clean_points(points: Iterable[MidPoint]) -> list[MidPoint]:
    out: list[MidPoint] = []
    for point in sorted(points, key=lambda item: int(item.ts_ms)):
        try:
            ts = int(point.ts_ms)
            mid = float(point.mid)
        except (TypeError, ValueError):
            continue
        if ts <= 0 or not math.isfinite(mid) or not 0.0 < mid < 1.0:
            continue
        if out and ts == out[-1].ts_ms:
            out[-1] = MidPoint(ts, mid)
        else:
            out.append(MidPoint(ts, mid))
    return out


def _linear_slope(points: Sequence[MidPoint]) -> float:
    if len(points) < 2:
        return 0.0
    origin = points[0].ts_ms
    xs = [(point.ts_ms - origin) / 1000.0 for point in points]
    ys = [point.mid for point in points]
    x_bar = sum(xs) / len(xs)
    y_bar = sum(ys) / len(ys)
    denominator = sum((x - x_bar) ** 2 for x in xs)
    if denominator <= 1e-12:
        return 0.0
    return sum((x - x_bar) * (y - y_bar) for x, y in zip(xs, ys)) / denominator


def _one_way_ratio(values: Sequence[float]) -> float:
    changes = [b - a for a, b in zip(values, values[1:]) if abs(b - a) > 1e-9]
    if not changes:
        return 0.0
    net = values[-1] - values[0]
    if abs(net) <= 1e-9:
        positive = sum(1 for change in changes if change > 0)
        negative = sum(1 for change in changes if change < 0)
        return max(positive, negative) / len(changes)
    sign = 1.0 if net > 0 else -1.0
    aligned = sum(1 for change in changes if change * sign > 0)
    return aligned / len(changes)


def _paired_complement_residual(
    up_points: Sequence[MidPoint],
    down_points: Sequence[MidPoint],
    *,
    max_skew_ms: int = 1000,
) -> float | None:
    if not up_points or not down_points:
        return None
    residuals: list[float] = []
    down_index = 0
    for up in up_points:
        while (
            down_index + 1 < len(down_points)
            and abs(down_points[down_index + 1].ts_ms - up.ts_ms)
            <= abs(down_points[down_index].ts_ms - up.ts_ms)
        ):
            down_index += 1
        down = down_points[down_index]
        if abs(down.ts_ms - up.ts_ms) <= max_skew_ms:
            residuals.append(abs(up.mid + down.mid - 1.0))
    return max(residuals) if residuals else None


def evaluate_opening_stability(
    *,
    profile: OpeningGateProfile,
    up_points: Iterable[MidPoint],
    down_points: Iterable[MidPoint],
    market_age_sec: float,
    current_up_spread: float | None,
    current_down_spread: float | None,
    queue_up_at_40: float,
    queue_down_at_40: float,
    near_depth_up: float,
    near_depth_down: float,
) -> OpeningStabilityDecision:
    """Evaluate a fixed market-opening window without using future observations."""
    profile.validate()
    up = _clean_points(up_points)
    down = _clean_points(down_points)
    up_span = (up[-1].ts_ms - up[0].ts_ms) / 1000.0 if len(up) >= 2 else 0.0
    down_span = (down[-1].ts_ms - down[0].ts_ms) / 1000.0 if len(down) >= 2 else 0.0
    history_span = min(up_span, down_span)

    def result(eligible: bool, reason: str, **metrics: float | None) -> OpeningStabilityDecision:
        return OpeningStabilityDecision(
            eligible=eligible,
            reason=reason,
            observation_sec=float(profile.observation_sec),
            history_span_sec=max(0.0, history_span),
            mid_range=metrics.get("mid_range"),
            net_drift=metrics.get("net_drift"),
            slope_per_sec=metrics.get("slope"),
            one_way_ratio=metrics.get("one_way"),
            max_jump=metrics.get("max_jump"),
            complement_residual=metrics.get("residual"),
            max_spread=metrics.get("spread"),
            queue_imbalance=metrics.get("queue"),
            depth_balance_ratio=metrics.get("depth"),
        )

    if market_age_sec + 1e-9 < profile.observation_sec:
        return result(False, "WAITING_OPENING_WINDOW")
    if len(up) < 2 or len(down) < 2 or history_span + 1.0 < profile.observation_sec:
        return result(False, "OPENING_HISTORY_INSUFFICIENT")

    up_values = [point.mid for point in up]
    down_values = [point.mid for point in down]
    mid_range = max(max(up_values) - min(up_values), max(down_values) - min(down_values))
    net_drift = max(abs(up_values[-1] - up_values[0]), abs(down_values[-1] - down_values[0]))
    slope = max(abs(_linear_slope(up)), abs(_linear_slope(down)))
    one_way = max(_one_way_ratio(up_values), _one_way_ratio(down_values))
    max_jump = max(
        max((abs(b - a) for a, b in zip(up_values, up_values[1:])), default=0.0),
        max((abs(b - a) for a, b in zip(down_values, down_values[1:])), default=0.0),
    )
    residual = _paired_complement_residual(up, down)
    spread_values = [
        float(value)
        for value in (current_up_spread, current_down_spread)
        if value is not None
    ]
    spread = max(spread_values) if len(spread_values) == 2 else None
    queue_min = min(max(0.0, queue_up_at_40), max(0.0, queue_down_at_40))
    queue_max = max(max(0.0, queue_up_at_40), max(0.0, queue_down_at_40))
    queue = 1.0 if queue_max <= 1e-12 else queue_max / max(queue_min, 1e-9)
    depth_min = min(max(0.0, near_depth_up), max(0.0, near_depth_down))
    depth_max = max(max(0.0, near_depth_up), max(0.0, near_depth_down))
    depth = 1.0 if depth_max <= 1e-12 else depth_min / depth_max
    metrics = {
        "mid_range": mid_range,
        "net_drift": net_drift,
        "slope": slope,
        "one_way": one_way,
        "max_jump": max_jump,
        "residual": residual,
        "spread": spread,
        "queue": queue,
        "depth": depth,
    }

    if mid_range > profile.max_mid_range + 1e-12:
        return result(False, "REJECTED_OPENING_RANGE", **metrics)
    if net_drift > profile.max_net_drift + 1e-12:
        return result(False, "REJECTED_OPENING_DRIFT", **metrics)
    if slope > profile.max_abs_slope_per_sec + 1e-12:
        return result(False, "REJECTED_OPENING_SLOPE", **metrics)
    if one_way > profile.max_one_way_ratio + 1e-12:
        return result(False, "REJECTED_OPENING_ONE_WAY_SEQUENCE", **metrics)
    if max_jump > profile.max_single_jump + 1e-12:
        return result(False, "REJECTED_OPENING_JUMP", **metrics)
    if residual is None or residual > profile.max_complement_residual + 1e-12:
        return result(False, "REJECTED_OPENING_COMPLEMENT_RESIDUAL", **metrics)
    if spread is None or spread <= 0 or spread > profile.max_spread_each + 1e-12:
        return result(False, "REJECTED_OPENING_SPREAD_EXPANSION", **metrics)
    if queue > profile.max_queue_imbalance + 1e-12:
        return result(False, "REJECTED_ASYMMETRIC_40C_QUEUE", **metrics)
    if depth + 1e-12 < profile.min_depth_balance_ratio:
        return result(False, "REJECTED_THIN_COUNTER_LEG", **metrics)
    return result(True, "OPENING_STABLE_TWO_WAY", **metrics)


def evaluate_balanced_regime(
    *,
    policy: Dual40Policy,
    up_points: Iterable[MidPoint],
    current_down_mid: float | None,
    current_up_spread: float | None,
    current_down_spread: float | None,
    current_up_ask: float | None,
    current_down_ask: float | None,
    market_age_sec: float,
    tte_sec: float,
    require_balanced_mid: bool = True,
    require_post_only_safe: bool = True,
) -> RegimeDecision:
    """Evaluate the shared CLOB regime with scope-specific entry controls.

    LIVE requires balanced mids and maker-safe asks. PAPER can disable those two
    controls to simulate ordinary 40-cent limit orders, including an immediate full
    virtual fill when an entry-time ask is already at or below the limit.
    """
    policy.validate()
    points = _clean_points(up_points)
    up_mid = points[-1].mid if points else None
    down_mid = None
    try:
        down_mid = float(current_down_mid) if current_down_mid is not None else None
    except (TypeError, ValueError):
        down_mid = None

    history_span = (
        max(0.0, (points[-1].ts_ms - points[0].ts_ms) / 1000.0)
        if len(points) >= 2
        else 0.0
    )

    def reject(reason: str, **metrics) -> RegimeDecision:
        return RegimeDecision(
            False,
            reason,
            0.0,
            up_mid,
            down_mid,
            metrics.get("mid_range"),
            metrics.get("net_drift"),
            metrics.get("slope"),
            metrics.get("one_way"),
            metrics.get("max_jump"),
            metrics.get("residual"),
            history_span,
        )

    if market_age_sec + 1e-9 < policy.min_market_age_sec:
        return reject("MARKET_WARMUP")
    if tte_sec + 1e-9 < policy.min_tte_sec:
        return reject("TTE_TOO_LOW")
    if up_mid is None or down_mid is None:
        return reject("MID_MISSING")
    if len(points) < 2 or history_span + 1e-9 < policy.lookback_sec:
        return reject("REGIME_HISTORY_INSUFFICIENT")
    if current_up_ask is None or current_down_ask is None:
        return reject("ASK_MISSING")
    if require_post_only_safe and (
        float(current_up_ask) <= policy.price + 1e-12
        or float(current_down_ask) <= policy.price + 1e-12
    ):
        return reject("POST_ONLY_WOULD_CROSS")
    if (
        require_balanced_mid
        and not policy.balanced_mid_low <= up_mid <= policy.balanced_mid_high
    ):
        return reject("UP_MID_NOT_BALANCED")
    if (
        require_balanced_mid
        and not policy.balanced_mid_low <= down_mid <= policy.balanced_mid_high
    ):
        return reject("DOWN_MID_NOT_BALANCED")

    up_spread = float(current_up_spread or 0.0)
    down_spread = float(current_down_spread or 0.0)
    if up_spread <= 0 or down_spread <= 0:
        return reject("SPREAD_INVALID")
    if up_spread > policy.max_spread_each or down_spread > policy.max_spread_each:
        return reject("SPREAD_TOO_WIDE")

    values = [point.mid for point in points]
    mid_range = max(values) - min(values)
    net_drift = abs(values[-1] - values[0])
    slope = abs(_linear_slope(points))
    one_way = _one_way_ratio(values)
    max_jump = max((abs(b - a) for a, b in zip(values, values[1:])), default=0.0)
    residual = abs(up_mid + down_mid - 1.0)
    metrics = {
        "mid_range": mid_range,
        "net_drift": net_drift,
        "slope": slope,
        "one_way": one_way,
        "max_jump": max_jump,
        "residual": residual,
    }

    if mid_range > policy.max_mid_range + 1e-12:
        return reject("MID_RANGE_TOO_WIDE", **metrics)
    if net_drift > policy.max_net_drift + 1e-12:
        return reject("NET_DRIFT_TOO_HIGH", **metrics)
    if slope > policy.max_abs_slope_per_sec + 1e-12:
        return reject("ONE_WAY_SLOPE", **metrics)
    if one_way > policy.max_one_way_ratio + 1e-12:
        return reject("ONE_WAY_SEQUENCE", **metrics)
    if max_jump > policy.max_single_jump + 1e-12:
        return reject("SINGLE_JUMP_TOO_LARGE", **metrics)
    if residual > policy.max_complement_residual + 1e-12:
        return reject("COMPLEMENT_RESIDUAL_TOO_HIGH", **metrics)

    # Score is only for selecting the best candidate when several markets pass.
    balance_penalty = abs(up_mid - 0.5) / max(1e-9, policy.balanced_mid_high - 0.5)
    range_penalty = mid_range / max(1e-9, policy.max_mid_range)
    drift_penalty = net_drift / max(1e-9, policy.max_net_drift)
    slope_penalty = slope / max(1e-9, policy.max_abs_slope_per_sec)
    sequence_penalty = one_way / max(1e-9, policy.max_one_way_ratio)
    residual_penalty = residual / max(1e-9, policy.max_complement_residual)
    penalty = (
        0.28 * balance_penalty
        + 0.20 * range_penalty
        + 0.18 * drift_penalty
        + 0.14 * slope_penalty
        + 0.10 * sequence_penalty
        + 0.10 * residual_penalty
    )
    score = max(0.0, min(1.0, 1.0 - penalty))
    reason = (
        "BALANCED_STABLE_TWO_WAY"
        if require_balanced_mid and require_post_only_safe
        else "PAPER_ENTRY_REGIME_ACCEPTED"
    )
    return RegimeDecision(
        True,
        reason,
        score,
        up_mid,
        down_mid,
        mid_range,
        net_drift,
        slope,
        one_way,
        max_jump,
        residual,
        history_span,
    )


def realized_cycle_pnl(
    *,
    price: float,
    up_filled: float,
    down_filled: float,
    official_result: str,
    maker_fees_usdc: float = 0.0,
    up_fill_price: float | None = None,
    down_fill_price: float | None = None,
) -> float:
    """Return settlement PnL from actual filled shares, including partial imbalance."""
    side = str(official_result or "").strip().upper()
    if side not in {"UP", "DOWN"}:
        raise ValueError("official_result must be UP or DOWN")
    up = max(0.0, float(up_filled))
    down = max(0.0, float(down_filled))
    up_price = float(price if up_fill_price is None else up_fill_price)
    down_price = float(price if down_fill_price is None else down_fill_price)
    cost = (up_price * up) + (down_price * down) + max(0.0, float(maker_fees_usdc))
    payout = up if side == "UP" else down
    return payout - cost


def matched_pair_pnl(
    *,
    price: float,
    matched_shares: float,
    maker_fees_usdc: float = 0.0,
    up_fill_price: float | None = None,
    down_fill_price: float | None = None,
) -> float:
    matched = max(0.0, float(matched_shares))
    up_price = float(price if up_fill_price is None else up_fill_price)
    down_price = float(price if down_fill_price is None else down_fill_price)
    return matched * (1.0 - up_price - down_price) - max(0.0, float(maker_fees_usdc))


def next_ladder_state(
    *,
    policy: Dual40Policy,
    loss_pool_before: float,
    cycle_pnl: float,
) -> LadderDecision:
    """Choose the smallest ladder level capable of recovering the realized loss pool.

    No-fill has zero PnL and therefore leaves the current economically required level
    unchanged.  A profitable cycle reduces the pool.  If a 30-share fully matched
    pair cannot repay the remaining pool, the strategy permanently hard-stops; there
    is deliberately no 90/270-share continuation.
    """
    policy.validate()
    pool = max(0.0, float(loss_pool_before) - float(cycle_pnl))
    if pool <= 1e-9:
        return LadderDecision(0, float(policy.ladder[0]), 0.0, False, "LOSS_POOL_CLEARED")

    edge = policy.pair_edge_per_share
    if edge <= 0:
        return LadderDecision(
            len(policy.ladder) - 1,
            float(policy.ladder[-1]),
            pool,
            True,
            "PAIR_EDGE_NON_POSITIVE",
        )

    for index, quantity in enumerate(policy.ladder):
        if float(quantity) * edge + 1e-9 >= pool:
            return LadderDecision(
                index,
                float(quantity),
                pool,
                False,
                f"RECOVERY_LEVEL_{index}",
            )

    return LadderDecision(
        len(policy.ladder) - 1,
        float(policy.ladder[-1]),
        pool,
        True,
        "HARD_STOP_MAX_30_CANNOT_RECOVER",
    )
