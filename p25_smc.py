"""Binance micro-structure confirmation for Direction Engine 5m entries.

This module intentionally does not create a standalone trading signal.  It converts
recent Binance spot ticks into 5-second OHLC micro-bars and detects three structural
Smart Money Concepts (SMC) events:

* liquidity sweep + reclaim,
* BOS/CHOCH-style structure break with displacement,
* three-candle fair-value-gap (FVG) displacement.

The three components are later used as a strict confirmation gate for the independent
PTB+Binance alpha.  They never read Polymarket prices and never submit orders.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Iterable


BAR_MS = 5_000
LOOKBACK_MS = 90_000
EVENT_MAX_AGE_SEC = 20.0
SWING_BARS = 5
MIN_BARS = 12


@dataclass(frozen=True)
class MicroBar:
    start_ms: int
    end_ms: int
    open: float
    high: float
    low: float
    close: float

    @property
    def range(self) -> float:
        return max(0.0, self.high - self.low)

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def body_ratio(self) -> float:
        return self.body / self.range if self.range > 0 else 0.0


@dataclass(frozen=True)
class SMCEvent:
    sign: int = 0
    age_sec: float | None = None
    strength: float = 0.0
    kind: str = "NONE"

    def to_dict(self) -> dict:
        return {
            "sign": int(self.sign),
            "age_sec": round(float(self.age_sec), 3) if self.age_sec is not None else None,
            "strength": round(float(self.strength), 4),
            "kind": self.kind,
        }


@dataclass(frozen=True)
class SMCState:
    ready: bool
    reason: str
    bars: int
    atr_bps: float
    sweep: SMCEvent
    structure: SMCEvent
    fvg: SMCEvent
    score: float
    confirmations_up: int
    confirmations_down: int

    @property
    def direction(self) -> str:
        if self.score > 1e-9:
            return "UP"
        if self.score < -1e-9:
            return "DOWN"
        return "NEUTRAL"

    def to_dict(self) -> dict:
        return {
            "ready": self.ready,
            "reason": self.reason,
            "bars": self.bars,
            "atr_bps": round(float(self.atr_bps), 3),
            "score": round(float(self.score), 4),
            "direction": self.direction,
            "confirmations_up": int(self.confirmations_up),
            "confirmations_down": int(self.confirmations_down),
            "liquidity_sweep": self.sweep.to_dict(),
            "structure": self.structure.to_dict(),
            "fvg": self.fvg.to_dict(),
        }


def build_micro_bars(
    prices: Iterable[tuple[int, float]],
    *,
    now_ms: int,
    bar_ms: int = BAR_MS,
    lookback_ms: int = LOOKBACK_MS,
) -> list[MicroBar]:
    buckets: dict[int, list[tuple[int, float]]] = {}
    cutoff = int(now_ms) - int(lookback_ms)
    for ts, raw_px in prices:
        try:
            ts_i = int(ts)
            px = float(raw_px)
        except (TypeError, ValueError):
            continue
        if ts_i < cutoff or ts_i > now_ms or not math.isfinite(px) or px <= 0:
            continue
        start = (ts_i // int(bar_ms)) * int(bar_ms)
        buckets.setdefault(start, []).append((ts_i, px))

    bars: list[MicroBar] = []
    for start in sorted(buckets):
        pts = sorted(buckets[start], key=lambda item: item[0])
        values = [p for _, p in pts]
        bars.append(
            MicroBar(
                start_ms=start,
                end_ms=min(start + int(bar_ms), int(now_ms)),
                open=values[0],
                high=max(values),
                low=min(values),
                close=values[-1],
            )
        )
    return bars


def _atr_price(bars: list[MicroBar], n: int = 10) -> float:
    if len(bars) < 2:
        return 0.0
    trs: list[float] = []
    for i in range(max(1, len(bars) - n), len(bars)):
        bar = bars[i]
        prev_close = bars[i - 1].close
        trs.append(
            max(
                bar.high - bar.low,
                abs(bar.high - prev_close),
                abs(bar.low - prev_close),
            )
        )
    return float(statistics.median(trs)) if trs else 0.0


def _event_age(now_ms: int, bar: MicroBar) -> float:
    return max(0.0, (int(now_ms) - int(bar.end_ms)) / 1000.0)


def _latest_sweep(bars: list[MicroBar], now_ms: int, atr: float) -> SMCEvent:
    best = SMCEvent()
    for i in range(SWING_BARS, len(bars)):
        bar = bars[i]
        age = _event_age(now_ms, bar)
        if age > EVENT_MAX_AGE_SEC:
            continue
        prev = bars[i - SWING_BARS : i]
        swing_high = max(x.high for x in prev)
        swing_low = min(x.low for x in prev)
        threshold = max(atr * 0.10, bar.close * 0.00004)  # ~0.4bp floor
        rng = max(bar.range, 1e-12)

        bull_excursion = swing_low - bar.low
        bull_reclaim = (bar.close - bar.low) / rng
        bull = (
            bull_excursion > threshold
            and bar.close > swing_low
            and bar.close >= bar.open
            and bull_reclaim >= 0.65
        )

        bear_excursion = bar.high - swing_high
        bear_reclaim = (bar.high - bar.close) / rng
        bear = (
            bear_excursion > threshold
            and bar.close < swing_high
            and bar.close <= bar.open
            and bear_reclaim >= 0.65
        )

        if bull and not bear:
            strength = min(1.0, 0.55 + 0.45 * bull_excursion / max(atr, threshold))
            best = SMCEvent(+1, age, strength, "SELL_SIDE_SWEEP_RECLAIM")
        elif bear and not bull:
            strength = min(1.0, 0.55 + 0.45 * bear_excursion / max(atr, threshold))
            best = SMCEvent(-1, age, strength, "BUY_SIDE_SWEEP_REJECT")
    return best


def _prior_trend_sign(bars: list[MicroBar], i: int) -> int:
    start = max(0, i - 6)
    if i - start < 3:
        return 0
    delta = bars[i - 1].close - bars[start].close
    return 1 if delta > 0 else (-1 if delta < 0 else 0)


def _latest_structure(bars: list[MicroBar], now_ms: int, atr: float) -> SMCEvent:
    best = SMCEvent()
    for i in range(SWING_BARS, len(bars)):
        bar = bars[i]
        age = _event_age(now_ms, bar)
        if age > EVENT_MAX_AGE_SEC:
            continue
        prev = bars[i - SWING_BARS : i]
        swing_high = max(x.high for x in prev)
        swing_low = min(x.low for x in prev)
        threshold = max(atr * 0.08, bar.close * 0.00004)
        displacement = bar.body >= max(atr * 0.45, bar.close * 0.00003)
        quality = bar.body_ratio >= 0.55 and displacement
        trend = _prior_trend_sign(bars, i)

        bull = quality and bar.close > swing_high + threshold
        bear = quality and bar.close < swing_low - threshold
        if bull and not bear:
            kind = "BULL_CHOCH" if trend < 0 else "BULL_BOS"
            strength = min(1.0, 0.55 + 0.45 * bar.body / max(atr, threshold))
            best = SMCEvent(+1, age, strength, kind)
        elif bear and not bull:
            kind = "BEAR_CHOCH" if trend > 0 else "BEAR_BOS"
            strength = min(1.0, 0.55 + 0.45 * bar.body / max(atr, threshold))
            best = SMCEvent(-1, age, strength, kind)
    return best


def _latest_fvg(bars: list[MicroBar], now_ms: int, atr: float) -> SMCEvent:
    best = SMCEvent()
    for i in range(2, len(bars)):
        a, b, c = bars[i - 2], bars[i - 1], bars[i]
        age = _event_age(now_ms, c)
        if age > EVENT_MAX_AGE_SEC:
            continue
        min_gap = max(atr * 0.05, c.close * 0.00002)  # ~0.2bp floor
        displaced = b.body_ratio >= 0.60 and b.body >= max(atr * 0.55, b.close * 0.00004)
        if not displaced:
            continue

        bull_gap = c.low - a.high
        bear_gap = a.low - c.high
        bull = b.close > b.open and bull_gap > min_gap
        bear = b.close < b.open and bear_gap > min_gap
        if bull and not bear:
            strength = min(1.0, 0.55 + 0.45 * bull_gap / max(atr, min_gap))
            best = SMCEvent(+1, age, strength, "BULL_FVG_DISPLACEMENT")
        elif bear and not bull:
            strength = min(1.0, 0.55 + 0.45 * bear_gap / max(atr, min_gap))
            best = SMCEvent(-1, age, strength, "BEAR_FVG_DISPLACEMENT")
    return best


def analyze_smc_bars(bars: list[MicroBar], *, now_ms: int) -> SMCState:
    if len(bars) < MIN_BARS:
        return SMCState(
            ready=False,
            reason=f"SMC_HISTORY_{len(bars)}_LT_{MIN_BARS}",
            bars=len(bars),
            atr_bps=0.0,
            sweep=SMCEvent(),
            structure=SMCEvent(),
            fvg=SMCEvent(),
            score=0.0,
            confirmations_up=0,
            confirmations_down=0,
        )

    atr = _atr_price(bars)
    last = bars[-1].close
    if atr <= 0 or last <= 0:
        return SMCState(
            ready=False,
            reason="SMC_ATR_INVALID",
            bars=len(bars),
            atr_bps=0.0,
            sweep=SMCEvent(),
            structure=SMCEvent(),
            fvg=SMCEvent(),
            score=0.0,
            confirmations_up=0,
            confirmations_down=0,
        )

    sweep = _latest_sweep(bars, now_ms, atr)
    structure = _latest_structure(bars, now_ms, atr)
    fvg = _latest_fvg(bars, now_ms, atr)
    events = (sweep, structure, fvg)
    weights = (0.40, 0.35, 0.25)
    score = sum(weight * event.sign * event.strength for weight, event in zip(weights, events))
    up = sum(1 for event in events if event.sign > 0)
    down = sum(1 for event in events if event.sign < 0)
    return SMCState(
        ready=True,
        reason="OK",
        bars=len(bars),
        atr_bps=10000.0 * atr / last,
        sweep=sweep,
        structure=structure,
        fvg=fvg,
        score=max(-1.0, min(1.0, score)),
        confirmations_up=up,
        confirmations_down=down,
    )


def compute_smc_state(prices: Iterable[tuple[int, float]], *, now_ms: int) -> SMCState:
    bars = build_micro_bars(prices, now_ms=now_ms)
    return analyze_smc_bars(bars, now_ms=now_ms)
