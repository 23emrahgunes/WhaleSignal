"""P2.1 feature engine for Direction Engine vNext.

Pure SHADOW feature generation only. No model fitting, calibration or execution.
The engine exposes multi-horizon returns, momentum persistence, aggressive flow,
realized volatility, PTB-relative state, Binance microstructure and Polymarket
CLOB confirmation. Missing history stays missing; long-window returns are never
fabricated from a shorter buffer.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from models import AssetHorizon, Horizon, LocalBook, Trade

BASE_WINDOWS_MS = [100, 250, 500, 1000, 3000, 5000, 15000, 30000, 60000, 120000, 180000]
LONG_WINDOWS_MS = [300000, 600000, 900000, 1800000]
FLOW_WINDOWS_MS = [1000, 3000, 5000, 15000, 30000]
MOMENTUM_WINDOWS_MS = [5000, 15000, 30000, 60000]
VOL_WINDOWS_MS = [5000, 30000, 60000, 180000]
VOL_WARMUP = 30


def windows_for(horizon: Horizon) -> list[int]:
    return BASE_WINDOWS_MS + (LONG_WINDOWS_MS if horizon == Horizon.H1H else [])


def _anchor_tolerance_ms(window_ms: int) -> int:
    """Maximum allowed gap between requested anchor and observed price."""
    return max(100, min(1500, window_ms // 3))


def price_at(prices: list[tuple[int, float]], target_ms: int, max_age_ms: Optional[int] = None) -> Optional[float]:
    """Last observed price at/before target; never substitutes a future sample."""
    for ts, px in reversed(prices):
        if ts <= target_ms:
            if max_age_ms is not None and target_ms - ts > max_age_ms:
                return None
            return px
    return None


def pct_return(prices: list[tuple[int, float]], window_ms: int, now_ms: int) -> Optional[float]:
    if len(prices) < 2:
        return None
    tol = _anchor_tolerance_ms(window_ms)
    p_now = price_at(prices, now_ms, max_age_ms=max(250, tol))
    p_then = price_at(prices, now_ms - window_ms, max_age_ms=tol)
    if p_now is None or p_then is None or p_then <= 0:
        return None
    return p_now / p_then - 1.0


def realized_vol(prices: list[tuple[int, float]], window_ms: int, now_ms: int) -> Optional[float]:
    """Realized log-volatility over the requested window (sqrt sum r^2)."""
    anchor = price_at(prices, now_ms - window_ms, max_age_ms=_anchor_tolerance_ms(window_ms))
    if anchor is None:
        return None
    pts = [(now_ms - window_ms, anchor)] + [(ts, px) for ts, px in prices if now_ms - window_ms < ts <= now_ms]
    if len(pts) < 3:
        return None
    sq = 0.0
    count = 0
    prev = pts[0][1]
    for _, px in pts[1:]:
        if prev > 0 and px > 0:
            r = math.log(px / prev)
            sq += r * r
            count += 1
        prev = px
    return math.sqrt(sq) if count >= 2 else None


def momentum_persistence(prices: list[tuple[int, float]], window_ms: int, now_ms: int, step_ms: int = 1000) -> dict:
    """Directional persistence from non-overlapping sub-window returns."""
    n_steps = max(2, window_ms // step_ms)
    sub: list[float] = []
    start = now_ms - n_steps * step_ms
    for i in range(n_steps):
        end = start + (i + 1) * step_ms
        r = pct_return(prices, step_ms, end)
        if r is not None:
            sub.append(r)
    if len(sub) < 2:
        return {"sign_persistence": 0.0, "flip_rate": 0.0, "run_len": 0.0, "accel": 0.0, "samples": len(sub)}
    net = sum(sub)
    sign = 1 if net > 0 else (-1 if net < 0 else 0)
    directional = [1 if r > 0 else (-1 if r < 0 else 0) for r in sub]
    same = sum(1 for s in directional if sign and s == sign)
    nonzero = sum(1 for s in directional if s)
    persist = same / nonzero if nonzero else 0.0
    nz = [s for s in directional if s]
    flips = sum(1 for i in range(1, len(nz)) if nz[i] != nz[i - 1])
    flip_rate = flips / (len(nz) - 1) if len(nz) > 1 else 0.0
    run = 0
    last = 0
    for s in reversed(directional):
        if s == 0:
            break
        if last == 0 or s == last:
            run += 1
            last = s
        else:
            break
    half = len(sub) // 2
    accel = sum(sub[half:]) - sum(sub[:half]) if half else 0.0
    return {"sign_persistence": persist, "flip_rate": flip_rate, "run_len": float(run * last), "accel": accel, "samples": len(sub)}


def flow_stats(trades: list[Trade], window_ms: int, now_ms: int) -> dict:
    lo = now_ms - window_ms
    buy_qty = sell_qty = buy_notional = sell_notional = 0.0
    count = 0
    for t in trades:
        if lo <= t.ts_ms <= now_ms:
            notion = t.price * t.qty
            if t.signed_qty > 0:
                buy_qty += t.qty
                buy_notional += notion
            else:
                sell_qty += t.qty
                sell_notional += notion
            count += 1
    total_qty = buy_qty + sell_qty
    total_notional = buy_notional + sell_notional
    return {
        "imbalance": ((buy_qty - sell_qty) / total_qty) if total_qty > 0 else None,
        "notional_imbalance": ((buy_notional - sell_notional) / total_notional) if total_notional > 0 else None,
        "buy_notional": buy_notional,
        "sell_notional": sell_notional,
        "trade_count": count,
        "trade_rate": count / max(0.001, window_ms / 1000.0),
    }


def flow_imbalance(trades: list[Trade], window_ms: int, now_ms: int) -> Optional[float]:
    return flow_stats(trades, window_ms, now_ms)["imbalance"]


def order_book_imbalance(book: LocalBook, depth: int = 20) -> Optional[float]:
    bids = book.top_levels("bid", depth)
    asks = book.top_levels("ask", depth)
    bsz = sum(l.size for l in bids)
    asz = sum(l.size for l in asks)
    return (bsz - asz) / (bsz + asz) if bsz + asz > 0 else None


@dataclass
class FeatureVector:
    combo: AssetHorizon
    ts: float
    seconds_remaining: float
    # legacy aliases retained for downstream compatibility
    ret_fast: float = 0.0
    ret_mid: float = 0.0
    ret_slow: float = 0.0
    ret_multi: dict = field(default_factory=dict)
    sign_persistence: float = 0.0
    flip_rate: float = 0.0
    run_len: float = 0.0
    mom_accel: float = 0.0
    momentum_multi: dict = field(default_factory=dict)
    flow_fast: float = 0.0
    flow_mid: float = 0.0
    flow_slow: float = 0.0
    flow_persistence: float = 0.0
    flow_accel: float = 0.0
    flow_multi: dict = field(default_factory=dict)
    flow_notional_5s: float = 0.0
    trade_rate_5s: float = 0.0
    rv_fast: float = 0.0
    rv_slow: float = 0.0
    vol_accel: float = 0.0
    vol_percentile: float = 0.5
    mom_vol_ratio: float = 0.0
    rv_multi: dict = field(default_factory=dict)
    distance_bps: float = 0.0
    distance_slope: float = 0.0
    ptb_z: float = 0.0
    tte_fraction: float = 0.0
    elapsed_fraction: float = 0.0
    obi: float = 0.0
    obi_5: float = 0.0
    obi_20: float = 0.0
    ofi: float = 0.0
    book_flow_agree: float = 0.0
    up_mid: Optional[float] = None
    down_mid: Optional[float] = None
    up_spread: float = 0.0
    down_spread: float = 0.0
    clob_spread: float = 0.0
    clob_complement_residual: float = 0.0
    clob_up_obi: float = 0.0
    clob_down_obi: float = 0.0
    up_mid_vel: float = 0.0
    up_mid_accel: float = 0.0
    clob_spot_agree: float = 0.0
    price_history_span_sec: float = 0.0
    feature_coverage: float = 0.0
    feature_ready: bool = False
    missing_features: list[str] = field(default_factory=list)
    has_reference: bool = False
    has_clob: bool = False

    _BASE_FIELDS = [
        "ret_fast", "ret_mid", "ret_slow", "sign_persistence", "flip_rate", "run_len", "mom_accel",
        "flow_fast", "flow_mid", "flow_slow", "flow_persistence", "flow_accel", "flow_notional_5s", "trade_rate_5s",
        "rv_fast", "rv_slow", "vol_accel", "vol_percentile", "mom_vol_ratio",
        "distance_bps", "distance_slope", "ptb_z", "tte_fraction", "elapsed_fraction",
        "obi_5", "obi_20", "ofi", "book_flow_agree",
    ]
    _CLOB_FIELDS = [
        "up_spread", "down_spread", "clob_spread", "clob_complement_residual",
        "clob_up_obi", "clob_down_obi", "up_mid_vel", "up_mid_accel", "clob_spot_agree",
    ]

    def model_features(self, include_clob: bool) -> tuple[list[str], list[float]]:
        names = list(self._BASE_FIELDS) + (list(self._CLOB_FIELDS) if include_clob else [])
        return names, [float(getattr(self, n) or 0.0) for n in names]

    def to_dict(self) -> dict:
        d = {n: round(float(getattr(self, n) or 0.0), 8) for n in self._BASE_FIELDS + self._CLOB_FIELDS}
        d.update({
            "ret_multi": self.ret_multi,
            "momentum_multi": self.momentum_multi,
            "flow_multi": self.flow_multi,
            "rv_multi": self.rv_multi,
            "up_mid": self.up_mid,
            "down_mid": self.down_mid,
            "has_reference": self.has_reference,
            "has_clob": self.has_clob,
            "price_history_span_sec": round(self.price_history_span_sec, 3),
            "feature_coverage": round(self.feature_coverage, 4),
            "feature_ready": self.feature_ready,
            "missing_features": list(self.missing_features),
        })
        return d

    def dashboard(self) -> dict:
        return {
            "ready": self.feature_ready,
            "coverage": round(self.feature_coverage, 3),
            "history_sec": round(self.price_history_span_sec, 1),
            "ret_1s_bps": round(self.ret_fast * 10000, 2),
            "ret_15s_bps": round(self.ret_mid * 10000, 2),
            "ret_60s_bps": round(self.ret_slow * 10000, 2),
            "momentum_persist": round(self.sign_persistence, 3),
            "flip_rate": round(self.flip_rate, 3),
            "flow_5s": round(self.flow_mid, 3),
            "rv_60s_bps": round(self.rv_slow * 10000, 2),
            "ptb_z": round(self.ptb_z, 3),
            "distance_slope": round(self.distance_slope, 3),
            "obi20": round(self.obi_20, 3),
            "ofi": round(self.ofi, 3),
            "clob_residual": round(self.clob_complement_residual, 4),
            "clob_vel": round(self.up_mid_vel, 4),
            "missing": list(self.missing_features),
        }


@dataclass
class _BookTop:
    bid_px: float
    bid_sz: float
    ask_px: float
    ask_sz: float


class FeatureEngine:
    def __init__(self, combo: AssetHorizon, vol_hist_max: int = 600) -> None:
        self.combo = combo
        self.windows = windows_for(combo.horizon)
        self._vol_hist: deque[float] = deque(maxlen=vol_hist_max)
        self._prev_top: Optional[_BookTop] = None
        self._up_mid_hist: deque[tuple[float, float]] = deque(maxlen=240)
        self._prev_distance_bps: Optional[float] = None
        self._prev_distance_ts: Optional[float] = None

    def on_market_change(self) -> None:
        self._prev_top = None
        self._up_mid_hist.clear()
        self._prev_distance_bps = None
        self._prev_distance_ts = None

    def _compute_ofi(self, book: LocalBook) -> float:
        bb, ba = book.best_bid, book.best_ask
        if bb is None or ba is None:
            return 0.0
        top = _BookTop(bb, book.bids.get(bb, 0.0), ba, book.asks.get(ba, 0.0))
        prev = self._prev_top
        self._prev_top = top
        if prev is None:
            return 0.0
        d_bid = top.bid_sz if top.bid_px > prev.bid_px else (-prev.bid_sz if top.bid_px < prev.bid_px else top.bid_sz - prev.bid_sz)
        d_ask = top.ask_sz if top.ask_px < prev.ask_px else (-prev.ask_sz if top.ask_px > prev.ask_px else top.ask_sz - prev.ask_sz)
        return max(-1.0, min(1.0, (d_bid - d_ask) / (top.bid_sz + top.ask_sz + 1e-9)))

    def _clob_trajectory(self, up_mid: Optional[float], now: float) -> tuple[float, float]:
        if up_mid is None:
            return 0.0, 0.0
        self._up_mid_hist.append((now, up_mid))
        # Prefer a ~1s baseline rather than adjacent 500ms samples only.
        prev = None
        for t, m in reversed(self._up_mid_hist):
            if now - t >= 0.8:
                prev = (t, m)
                break
        if prev is None:
            return 0.0, 0.0
        dt = max(1e-3, now - prev[0])
        vel = (up_mid - prev[1]) / dt
        old_vel = 0.0
        if len(self._up_mid_hist) >= 3:
            t0, m0 = self._up_mid_hist[-3]
            t1, m1 = self._up_mid_hist[-2]
            old_vel = (m1 - m0) / max(1e-3, t1 - t0)
        return vel, (vel - old_vel) / dt

    def update(
        self,
        prices: list[tuple[int, float]],
        trades: list[Trade],
        book: LocalBook,
        reference_price: Optional[float],
        up_mid: Optional[float],
        down_mid: Optional[float],
        seconds_remaining: float,
        now: float,
        *,
        up_bid: Optional[float] = None,
        up_ask: Optional[float] = None,
        down_bid: Optional[float] = None,
        down_ask: Optional[float] = None,
        clob_up_obi: Optional[float] = None,
        clob_down_obi: Optional[float] = None,
    ) -> FeatureVector:
        now_ms = int(now * 1000)
        fv = FeatureVector(combo=self.combo, ts=now, seconds_remaining=seconds_remaining)
        if prices:
            fv.price_history_span_sec = max(0.0, (prices[-1][0] - prices[0][0]) / 1000.0)

        ret_multi = {w: pct_return(prices, w, now_ms) for w in self.windows}
        fv.ret_multi = {str(w): (round(v, 10) if v is not None else None) for w, v in ret_multi.items()}
        fv.ret_fast = ret_multi.get(1000) or 0.0
        fv.ret_mid = ret_multi.get(15000) or 0.0
        fv.ret_slow = ret_multi.get(60000) or 0.0

        mom = {w: momentum_persistence(prices, w, now_ms, step_ms=1000) for w in MOMENTUM_WINDOWS_MS}
        fv.momentum_multi = {str(w): {k: round(float(v), 8) for k, v in x.items()} for w, x in mom.items()}
        m60 = mom[60000]
        fv.sign_persistence, fv.flip_rate, fv.run_len, fv.mom_accel = (
            m60["sign_persistence"], m60["flip_rate"], m60["run_len"], m60["accel"]
        )

        flows = {w: flow_stats(trades, w, now_ms) for w in FLOW_WINDOWS_MS}
        fv.flow_multi = {str(w): {k: (round(v, 8) if isinstance(v, float) else v) for k, v in x.items()} for w, x in flows.items()}
        fv.flow_fast = flows[1000]["imbalance"] or 0.0
        fv.flow_mid = flows[5000]["imbalance"] or 0.0
        fv.flow_slow = flows[30000]["imbalance"] or 0.0
        valid_flows = [flows[w]["imbalance"] for w in FLOW_WINDOWS_MS if flows[w]["imbalance"] is not None]
        if valid_flows:
            net = sum(valid_flows)
            same = sum(1 for x in valid_flows if (x > 0) == (net > 0))
            fv.flow_persistence = same / len(valid_flows)
        fv.flow_accel = fv.flow_fast - fv.flow_slow
        fv.flow_notional_5s = flows[5000]["notional_imbalance"] or 0.0
        fv.trade_rate_5s = float(flows[5000]["trade_rate"])

        vols = {w: realized_vol(prices, w, now_ms) for w in VOL_WINDOWS_MS}
        fv.rv_multi = {str(w): (round(v, 10) if v is not None else None) for w, v in vols.items()}
        fv.rv_fast = vols[5000] or 0.0
        fv.rv_slow = vols[60000] or 0.0
        fv.vol_accel = fv.rv_fast / fv.rv_slow if fv.rv_slow > 0 else 0.0
        if fv.rv_slow > 0:
            self._vol_hist.append(fv.rv_slow)
        if len(self._vol_hist) >= VOL_WARMUP:
            fv.vol_percentile = sum(1 for v in self._vol_hist if v <= fv.rv_slow) / len(self._vol_hist)
        fv.mom_vol_ratio = fv.ret_slow / fv.rv_slow if fv.rv_slow > 0 else 0.0

        if reference_price is not None and reference_price > 0 and prices:
            spot = price_at(prices, now_ms, max_age_ms=2000)
            if spot is not None:
                fv.distance_bps = 10000.0 * math.log(spot / reference_price)
                fv.has_reference = True
                if self._prev_distance_bps is not None and self._prev_distance_ts is not None:
                    fv.distance_slope = (fv.distance_bps - self._prev_distance_bps) / max(1e-3, now - self._prev_distance_ts)
                self._prev_distance_bps, self._prev_distance_ts = fv.distance_bps, now
                rv_bps = fv.rv_slow * 10000.0
                fv.ptb_z = fv.distance_bps / rv_bps if rv_bps > 0 else 0.0

        horizon_sec = float(self.combo.horizon.seconds)
        fv.tte_fraction = max(0.0, min(1.0, seconds_remaining / horizon_sec))
        fv.elapsed_fraction = 1.0 - fv.tte_fraction
        fv.obi_5 = order_book_imbalance(book, 5) or 0.0
        fv.obi_20 = order_book_imbalance(book, 20) or 0.0
        fv.obi = fv.obi_20
        fv.ofi = self._compute_ofi(book)
        if abs(fv.flow_mid) > 1e-12 and abs(fv.obi_20) > 1e-12:
            fv.book_flow_agree = 1.0 if (fv.obi_20 > 0) == (fv.flow_mid > 0) else -1.0

        if up_mid is not None and down_mid is not None:
            fv.up_mid, fv.down_mid, fv.has_clob = up_mid, down_mid, True
            fv.up_mid_vel, fv.up_mid_accel = self._clob_trajectory(up_mid, now)
            fv.clob_complement_residual = up_mid + down_mid - 1.0
            if abs(fv.up_mid_vel) > 1e-12 and abs(fv.ret_fast) > 1e-12:
                fv.clob_spot_agree = 1.0 if (fv.up_mid_vel > 0) == (fv.ret_fast > 0) else -1.0
        if up_bid is not None and up_ask is not None:
            fv.up_spread = max(0.0, up_ask - up_bid)
        if down_bid is not None and down_ask is not None:
            fv.down_spread = max(0.0, down_ask - down_bid)
        fv.clob_spread = fv.up_spread + fv.down_spread
        fv.clob_up_obi = clob_up_obi or 0.0
        fv.clob_down_obi = clob_down_obi or 0.0

        # P2.1 coverage: missing history remains visible instead of zero-imputed readiness.
        expected_returns = windows_for(self.combo.horizon)
        return_ok = sum(1 for w in expected_returns if ret_multi.get(w) is not None)
        checks = [
            (fv.has_reference, "reference"),
            (fv.has_clob, "clob"),
            (book.synced, "binance_book"),
            (ret_multi.get(5000) is not None, "ret_5s"),
            (ret_multi.get(60000) is not None, "ret_60s"),
            (flows[5000]["trade_count"] > 0, "flow_5s"),
            (vols[60000] is not None, "rv_60s"),
        ]
        fv.missing_features = [name for ok, name in checks if not ok]
        core_ratio = sum(1 for ok, _ in checks if ok) / len(checks)
        return_ratio = return_ok / len(expected_returns)
        fv.feature_coverage = 0.7 * core_ratio + 0.3 * return_ratio
        fv.feature_ready = core_ratio == 1.0 and fv.feature_coverage >= 0.80
        return fv
