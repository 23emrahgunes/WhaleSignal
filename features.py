"""Feature muhendisligi — ham feed'lerden turetilmis, normalize sinyaller.

Girdiler: direct Binance fiyat/islem ring buffer'lari + senkron local book +
horizon adaptorunun PTB'si + Polymarket CLOB up/down mid. Ciktilar: returns
(cok-horizon), momentum persistence, agresif flow imbalance, realized volatility
(+percentile), PTB mesafe/slope/Z, OBI/OFI (Binance book), CLOB trajectory.

Tasarim:
  - **Hard-code agirlik/ ic-carpim YOK** — ham/normalize olculer; agirligi model ogrenir.
  - `FeatureEngine` combo-bazli DURUM tutar (vol history, onceki book-top -> OFI,
    CLOB mid history -> trajectory). Her tick `update(...)` cagrilir.
  - `FeatureVector.model_features(include_clob)` iki varyant uretir: Binance-only vs
    +CLOB (ablation icin; "model Polymarket'in olasiligini taklit etmesin").
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from models import AssetHorizon, Horizon, LocalBook, Trade

# Tum combo'larda temel return/vol pencereleri (ms)
_BASE_WINDOWS_MS = [500, 1000, 3000, 5000, 15000, 30000, 60000, 120000, 180000]
# 1h icin ek uzun pencereler
_LONG_WINDOWS_MS = [300000, 600000, 900000, 1800000]
_FLOW_WINDOWS_MS = [1000, 3000, 5000, 15000, 30000]
# vol persentili anlamli olana kadar gereken minimum gecmis ornek sayisi
_VOL_WARMUP = 30


def windows_for(horizon: Horizon) -> list[int]:
    if horizon == Horizon.H1H:
        return _BASE_WINDOWS_MS + _LONG_WINDOWS_MS
    return _BASE_WINDOWS_MS


# ---------------------------------------------------------------------------
# Saf yardimcilar (list[(ts_ms, price)] / list[Trade] uzerinde; test edilebilir)
# ---------------------------------------------------------------------------


def price_at(prices: list[tuple[int, float]], target_ms: int) -> Optional[float]:
    """target_ms'e en yakin (>= target) ilk ornekten geriye, <= target son fiyat."""
    if not prices:
        return None
    # sondan geriye: target_ms'ten kucuk/esit ilk ornek
    best: Optional[float] = None
    for ts, px in reversed(prices):
        if ts <= target_ms:
            return px
        best = px
    return best  # hepsi target'tan buyukse en eski


def pct_return(prices: list[tuple[int, float]], window_ms: int, now_ms: int) -> Optional[float]:
    if not prices:
        return None
    p_now = prices[-1][1]
    p_then = price_at(prices, now_ms - window_ms)
    if p_now is None or p_then is None or p_then == 0:
        return None
    return (p_now / p_then) - 1.0


def _samples_in_window(
    prices: list[tuple[int, float]], window_ms: int, now_ms: int
) -> list[float]:
    lo = now_ms - window_ms
    return [px for ts, px in prices if ts >= lo]


def realized_vol(prices: list[tuple[int, float]], window_ms: int, now_ms: int) -> Optional[float]:
    """Pencere icindeki ardisik ornek getirilerinin std'i (birimsiz ~ oran)."""
    pts = [(ts, px) for ts, px in prices if ts >= now_ms - window_ms]
    if len(pts) < 3:
        return None
    rets: list[float] = []
    for i in range(1, len(pts)):
        p0, p1 = pts[i - 1][1], pts[i][1]
        if p0:
            rets.append(p1 / p0 - 1.0)
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(max(0.0, var))


def momentum_persistence(
    prices: list[tuple[int, float]], window_ms: int, now_ms: int, step_ms: int = 1000
) -> dict:
    """Pencereyi step'lere bol; alt-getiri isaretlerinin tutarliligi.

    sign_persistence: net yonle ayni isaretli alt-getiri orani.
    flip_rate: isaret degisim orani. run_len: son ardisik ayni-isaret sayisi (signed).
    accel: son yari vs onceki yari getiri farki (ivme).
    """
    n_steps = max(2, window_ms // step_ms)
    sub: list[float] = []
    for i in range(n_steps):
        t_end = now_ms - i * step_ms
        r = pct_return(prices, step_ms, t_end)
        if r is not None:
            sub.append(r)
    sub.reverse()  # eskiden yeniye
    if len(sub) < 2:
        return {"sign_persistence": 0.0, "flip_rate": 0.0, "run_len": 0.0, "accel": 0.0}
    net = sum(sub)
    net_sign = 1.0 if net > 0 else (-1.0 if net < 0 else 0.0)
    same = sum(1 for r in sub if (r > 0) == (net > 0) and r != 0)
    sign_persistence = same / len(sub)
    flips = sum(1 for i in range(1, len(sub)) if (sub[i] > 0) != (sub[i - 1] > 0))
    flip_rate = flips / (len(sub) - 1)
    # run length (signed)
    run = 0
    last_sign = 0.0
    for r in reversed(sub):
        s = 1.0 if r > 0 else (-1.0 if r < 0 else 0.0)
        if s == 0:
            break
        if last_sign == 0.0 or s == last_sign:
            run += 1
            last_sign = s
        else:
            break
    run_len = run * (last_sign if last_sign else 0.0)
    half = len(sub) // 2
    accel = sum(sub[half:]) - sum(sub[:half]) if half > 0 else 0.0
    return {
        "sign_persistence": sign_persistence,
        "flip_rate": flip_rate,
        "run_len": float(run_len),
        "accel": accel,
    }


def flow_imbalance(trades: list[Trade], window_ms: int, now_ms: int) -> Optional[float]:
    """FI = sum(signed_qty) / sum(|qty|) pencere icinde. [-1,1]."""
    lo = now_ms - window_ms
    signed = 0.0
    total = 0.0
    for t in trades:
        if t.ts_ms >= lo:
            signed += t.signed_qty
            total += abs(t.qty)
    if total <= 0:
        return None
    return signed / total


def order_book_imbalance(book: LocalBook, depth: int = 20) -> Optional[float]:
    """OBI = (bid_size - ask_size) / (bid_size + ask_size) top-N. [-1,1]."""
    bids = book.top_levels("bid", depth)
    asks = book.top_levels("ask", depth)
    bsz = sum(l.size for l in bids)
    asz = sum(l.size for l in asks)
    if bsz + asz <= 0:
        return None
    return (bsz - asz) / (bsz + asz)


# ---------------------------------------------------------------------------
# FeatureVector
# ---------------------------------------------------------------------------


@dataclass
class FeatureVector:
    combo: AssetHorizon
    ts: float
    seconds_remaining: float
    # returns
    ret_fast: float = 0.0  # ~1s
    ret_mid: float = 0.0  # ~15s
    ret_slow: float = 0.0  # ~60s
    ret_multi: dict = field(default_factory=dict)  # window_ms -> pct
    # momentum persistence
    sign_persistence: float = 0.0
    flip_rate: float = 0.0
    run_len: float = 0.0
    mom_accel: float = 0.0
    # flow
    flow_fast: float = 0.0  # ~1s
    flow_mid: float = 0.0  # ~5s
    flow_slow: float = 0.0  # ~30s
    flow_persistence: float = 0.0
    flow_accel: float = 0.0
    # volatility
    rv_fast: float = 0.0
    rv_slow: float = 0.0
    vol_accel: float = 0.0
    vol_percentile: float = 0.5
    mom_vol_ratio: float = 0.0
    # PTB
    distance_bps: float = 0.0
    distance_slope: float = 0.0  # bps / s
    ptb_z: float = 0.0
    # Binance book microstructure
    obi: float = 0.0
    ofi: float = 0.0
    book_flow_agree: float = 0.0
    # CLOB (Polymarket) — ablation icin ayri
    up_mid: Optional[float] = None
    clob_spread: float = 0.0
    up_mid_vel: float = 0.0
    up_mid_accel: float = 0.0
    clob_spot_agree: float = 0.0
    # meta
    has_reference: bool = False
    has_clob: bool = False

    # --- model girdisi (ablation: Binance-only vs +CLOB) ---
    _BASE_FIELDS = [
        "ret_fast", "ret_mid", "ret_slow",
        "sign_persistence", "flip_rate", "run_len", "mom_accel",
        "flow_fast", "flow_mid", "flow_slow", "flow_persistence", "flow_accel",
        "rv_fast", "rv_slow", "vol_accel", "vol_percentile", "mom_vol_ratio",
        "distance_bps", "distance_slope", "ptb_z",
        "obi", "ofi", "book_flow_agree",
    ]
    _CLOB_FIELDS = ["clob_spread", "up_mid_vel", "up_mid_accel", "clob_spot_agree"]

    def model_features(self, include_clob: bool) -> tuple[list[str], list[float]]:
        names = list(self._BASE_FIELDS)
        if include_clob:
            names = names + self._CLOB_FIELDS
        vals = [float(getattr(self, n) or 0.0) for n in names]
        return names, vals

    def to_dict(self) -> dict:
        d = {n: round(float(getattr(self, n) or 0.0), 6) for n in self._BASE_FIELDS}
        d.update({n: round(float(getattr(self, n) or 0.0), 6) for n in self._CLOB_FIELDS})
        d["up_mid"] = self.up_mid
        d["has_reference"] = self.has_reference
        d["has_clob"] = self.has_clob
        return d


# ---------------------------------------------------------------------------
# FeatureEngine — combo-bazli durumlu hesaplayici
# ---------------------------------------------------------------------------


@dataclass
class _BookTop:
    bid_px: float
    bid_sz: float
    ask_px: float
    ask_sz: float


class FeatureEngine:
    """Bir combo icin feature'lari uretir; slope/OFI/trajectory icin durum tutar."""

    def __init__(self, combo: AssetHorizon, vol_hist_max: int = 600) -> None:
        self.combo = combo
        self.windows = windows_for(combo.horizon)
        self._vol_hist: deque[float] = deque(maxlen=vol_hist_max)
        self._prev_top: Optional[_BookTop] = None
        self._up_mid_hist: deque[tuple[float, float]] = deque(maxlen=120)  # (ts, mid)
        self._obi_hist: deque[float] = deque(maxlen=60)
        self._prev_distance_bps: Optional[float] = None
        self._prev_distance_ts: Optional[float] = None

    def on_market_change(self) -> None:
        """Yeni markete gecince market-bazli durumu sifirla (PTB slope / CLOB trajectory /
        book-top). Volatilite gecmisi (asset dinamigi) korunur."""
        self._prev_top = None
        self._up_mid_hist.clear()
        self._prev_distance_bps = None
        self._prev_distance_ts = None

    def _compute_ofi(self, book: LocalBook) -> float:
        """Best-level boyut degisiminden OFI (Cont/Kukanov tarzi basit hal)."""
        bb, ba = book.best_bid, book.best_ask
        if bb is None or ba is None:
            return 0.0
        top = _BookTop(bb, book.bids.get(bb, 0.0), ba, book.asks.get(ba, 0.0))
        prev = self._prev_top
        self._prev_top = top
        if prev is None:
            return 0.0
        # bid tarafi katkisi
        if top.bid_px > prev.bid_px:
            d_bid = top.bid_sz
        elif top.bid_px < prev.bid_px:
            d_bid = -prev.bid_sz
        else:
            d_bid = top.bid_sz - prev.bid_sz
        # ask tarafi katkisi
        if top.ask_px < prev.ask_px:
            d_ask = top.ask_sz
        elif top.ask_px > prev.ask_px:
            d_ask = -prev.ask_sz
        else:
            d_ask = top.ask_sz - prev.ask_sz
        ofi = d_bid - d_ask
        denom = top.bid_sz + top.ask_sz + 1e-9
        return max(-1.0, min(1.0, ofi / denom))

    def _clob_trajectory(self, up_mid: Optional[float], now: float) -> tuple[float, float]:
        if up_mid is None:
            return 0.0, 0.0
        self._up_mid_hist.append((now, up_mid))
        if len(self._up_mid_hist) < 3:
            return 0.0, 0.0
        (t0, m0), (t1, m1), (t2, m2) = (
            self._up_mid_hist[-3],
            self._up_mid_hist[-2],
            self._up_mid_hist[-1],
        )
        dt1 = max(1e-3, t1 - t0)
        dt2 = max(1e-3, t2 - t1)
        v1 = (m1 - m0) / dt1
        v2 = (m2 - m1) / dt2
        vel = v2
        accel = (v2 - v1) / max(1e-3, dt2)
        return vel, accel

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
    ) -> FeatureVector:
        now_ms = int(now * 1000)
        fv = FeatureVector(combo=self.combo, ts=now, seconds_remaining=seconds_remaining)

        # returns
        ret_multi = {w: pct_return(prices, w, now_ms) for w in self.windows}
        fv.ret_multi = {str(w): (round(v, 8) if v is not None else None) for w, v in ret_multi.items()}
        fv.ret_fast = ret_multi.get(1000) or 0.0
        fv.ret_mid = ret_multi.get(15000) or 0.0
        fv.ret_slow = ret_multi.get(60000) or 0.0

        # momentum persistence (60s pencere, 1s adim)
        mp = momentum_persistence(prices, 60000, now_ms, step_ms=1000)
        fv.sign_persistence = mp["sign_persistence"]
        fv.flip_rate = mp["flip_rate"]
        fv.run_len = mp["run_len"]
        fv.mom_accel = mp["accel"]

        # flow
        fv.flow_fast = flow_imbalance(trades, 1000, now_ms) or 0.0
        fv.flow_mid = flow_imbalance(trades, 5000, now_ms) or 0.0
        fv.flow_slow = flow_imbalance(trades, 30000, now_ms) or 0.0
        sub_flows = [flow_imbalance(trades, w, now_ms) for w in _FLOW_WINDOWS_MS]
        sub_flows = [f for f in sub_flows if f is not None]
        if sub_flows:
            net = sum(sub_flows)
            same = sum(1 for f in sub_flows if (f > 0) == (net > 0))
            fv.flow_persistence = same / len(sub_flows)
            fv.flow_accel = fv.flow_fast - fv.flow_slow

        # volatility
        fv.rv_fast = realized_vol(prices, 5000, now_ms) or 0.0
        fv.rv_slow = realized_vol(prices, 60000, now_ms) or 0.0
        fv.vol_accel = (fv.rv_fast / fv.rv_slow) if fv.rv_slow > 0 else 0.0
        if fv.rv_fast > 0:
            self._vol_hist.append(fv.rv_fast)
        # persentil ancak yeterli gecmis isinmisken anlamli; oncesinde notr 0.5
        # (aksi halde ilk orneklerde 1.00'a saplanip SAHTE HIGH_VOL uretir)
        if len(self._vol_hist) >= _VOL_WARMUP:
            below = sum(1 for v in self._vol_hist if v <= fv.rv_fast)
            fv.vol_percentile = below / len(self._vol_hist)
        else:
            fv.vol_percentile = 0.5
        fv.mom_vol_ratio = (fv.ret_slow / fv.rv_slow) if fv.rv_slow > 0 else 0.0

        # PTB
        if reference_price and prices:
            spot = prices[-1][1]
            distance_bps = (spot - reference_price) / reference_price * 10000.0
            fv.distance_bps = distance_bps
            fv.has_reference = True
            if self._prev_distance_bps is not None and self._prev_distance_ts is not None:
                dt = max(1e-3, now - self._prev_distance_ts)
                fv.distance_slope = (distance_bps - self._prev_distance_bps) / dt
            self._prev_distance_bps = distance_bps
            self._prev_distance_ts = now
            rv_bps = fv.rv_slow * 10000.0
            fv.ptb_z = (distance_bps / rv_bps) if rv_bps > 0 else 0.0

        # Binance book microstructure
        fv.obi = order_book_imbalance(book) or 0.0
        self._obi_hist.append(fv.obi)
        fv.ofi = self._compute_ofi(book)
        fv.book_flow_agree = 1.0 if (fv.obi > 0) == (fv.flow_mid > 0) else -1.0

        # CLOB (Polymarket)
        if up_mid is not None:
            fv.up_mid = up_mid
            fv.has_clob = True
            fv.up_mid_vel, fv.up_mid_accel = self._clob_trajectory(up_mid, now)
            fv.clob_spot_agree = 1.0 if (fv.up_mid_vel > 0) == (fv.ret_fast > 0) else -1.0

        return fv
