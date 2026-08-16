"""Analitik motor: saf, yan-etkisiz, test-dostu hesaplamalar.

Tum fonksiyonlar deterministiktir ( agir I/O yok) => `test_suite.py` mock veri ile
matematiksel dogrulugu dogrular. Gostergeler: OBI, ATR(14), Bollinger squeeze,
ADX(14), fiyat hizi/doyum, kalan-sure (time decay) filtresi.
"""
from __future__ import annotations

from typing import Iterable, Sequence

import numpy as np

from models import Analytics, BookLevel, Candle, MarketMeta, OrderBook

# ----------------------------------------------------------------------------
# Order Book Imbalance (OBI)
# ----------------------------------------------------------------------------


def compute_obi(bids: Iterable[BookLevel], asks: Iterable[BookLevel]) -> float:
    """OBI = (Sum(Q_bid) - Sum(Q_ask)) / (Sum(Q_bid) + Sum(Q_ask)).

    Bos/dengeli tahtada 0.0 doner. Sonuc [-1, 1] araligindadir.
    +1 => tamamen alis agirlikli, -1 => tamamen satis agirlikli.
    """
    bid_q = float(sum(max(0.0, lvl.size) for lvl in bids))
    ask_q = float(sum(max(0.0, lvl.size) for lvl in asks))
    total = bid_q + ask_q
    if total <= 0.0:
        return 0.0
    return (bid_q - ask_q) / total


def book_obi(book: OrderBook) -> float:
    return compute_obi(book.bids, book.asks)


# ----------------------------------------------------------------------------
# ATR (Average True Range) + True Range
# ----------------------------------------------------------------------------


def true_ranges(candles: Sequence[Candle]) -> np.ndarray:
    """Her mum icin True Range = max(H-L, |H-Cprev|, |L-Cprev|)."""
    if len(candles) < 2:
        return np.array([], dtype=float)
    highs = np.array([c.high for c in candles], dtype=float)
    lows = np.array([c.low for c in candles], dtype=float)
    closes = np.array([c.close for c in candles], dtype=float)
    prev_close = closes[:-1]
    tr = np.maximum.reduce(
        [
            highs[1:] - lows[1:],
            np.abs(highs[1:] - prev_close),
            np.abs(lows[1:] - prev_close),
        ]
    )
    return tr


def compute_atr(candles: Sequence[Candle], period: int = 14) -> float:
    """Wilder ATR. Yetersiz mum varsa mevcut TR ortalamasina duser (0'a degil)."""
    tr = true_ranges(candles)
    if tr.size == 0:
        return 0.0
    if tr.size < period:
        return float(np.mean(tr))
    # Wilder smoothing: ilk ATR = ilk 'period' TR ortalamasi, sonra RMA.
    atr = float(np.mean(tr[:period]))
    for value in tr[period:]:
        atr = (atr * (period - 1) + float(value)) / period
    return atr


def atr_pct(candles: Sequence[Candle], period: int = 14) -> float:
    """ATR'yi son kapanisa oranla (fiyattan bagimsiz esik icin)."""
    atr = compute_atr(candles, period)
    if not candles or candles[-1].close <= 0:
        return 0.0
    return atr / candles[-1].close


# ----------------------------------------------------------------------------
# Bollinger Bands + Squeeze
# ----------------------------------------------------------------------------


def bollinger_bands(
    candles: Sequence[Candle], period: int = 20, num_std: float = 2.0
) -> tuple[float, float, float]:
    """(alt, orta, ust) Bollinger bandi. Yetersiz veri -> (c, c, c)."""
    closes = np.array([c.close for c in candles], dtype=float)
    if closes.size == 0:
        return 0.0, 0.0, 0.0
    window = closes[-period:] if closes.size >= period else closes
    mid = float(np.mean(window))
    std = float(np.std(window))
    return mid - num_std * std, mid, mid + num_std * std


def bollinger_squeeze(
    candles: Sequence[Candle],
    period: int = 20,
    num_std: float = 2.0,
    squeeze_pct: float = 0.02,
) -> bool:
    """Band genisligi (ust-alt)/orta < squeeze_pct ise volatilite sikismasi (squeeze)."""
    lower, mid, upper = bollinger_bands(candles, period, num_std)
    if mid <= 0:
        return False
    width = (upper - lower) / mid
    return width < squeeze_pct


# ----------------------------------------------------------------------------
# ADX (Average Directional Index) + DI
# ----------------------------------------------------------------------------


def compute_adx(candles: Sequence[Candle], period: int = 14) -> float:
    """Standart Wilder ADX. Yetersiz veri -> 0.0 (yani 'trend yok/bilinmiyor').

    +DM/-DM ve TR Wilder yontemiyle yumusatilir; DX = 100*|DI+ - DI-|/(DI+ + DI-);
    ADX = DX'in Wilder ortalamasi.
    """
    n = len(candles)
    if n < period + 1:
        return 0.0
    highs = np.array([c.high for c in candles], dtype=float)
    lows = np.array([c.low for c in candles], dtype=float)
    closes = np.array([c.close for c in candles], dtype=float)

    up_move = highs[1:] - highs[:-1]
    down_move = lows[:-1] - lows[1:]
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = true_ranges(candles)  # uzunluk n-1, plus/minus_dm ile hizali

    def _wilder_rma(values: np.ndarray, length: int) -> np.ndarray:
        out = np.zeros_like(values, dtype=float)
        if values.size < length:
            return out
        out[length - 1] = float(np.sum(values[:length]))
        for i in range(length, values.size):
            out[i] = out[i - 1] - (out[i - 1] / length) + float(values[i])
        return out

    tr_s = _wilder_rma(tr, period)
    plus_s = _wilder_rma(plus_dm, period)
    minus_s = _wilder_rma(minus_dm, period)

    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * np.where(tr_s > 0, plus_s / tr_s, 0.0)
        minus_di = 100.0 * np.where(tr_s > 0, minus_s / tr_s, 0.0)
        di_sum = plus_di + minus_di
        dx = 100.0 * np.where(di_sum > 0, np.abs(plus_di - minus_di) / di_sum, 0.0)

    valid_dx = dx[period - 1 :]
    if valid_dx.size == 0:
        return 0.0
    if valid_dx.size < period:
        return float(np.mean(valid_dx))
    adx = float(np.mean(valid_dx[:period]))
    for value in valid_dx[period:]:
        adx = (adx * (period - 1) + float(value)) / period
    return adx


# ----------------------------------------------------------------------------
# Fiyat hizi (dP/dt) ve doyum (saturation / post-spike mean-reversion)
# ----------------------------------------------------------------------------


def price_velocity(candles: Sequence[Candle], lookback: int = 3) -> float:
    """Son 'lookback' mum kapanisinin dogrusal egimi (fiyat / mum).

    Pozitif -> yukari ivme, ~0 -> yatay (doyum), negatif -> asagi.
    """
    closes = np.array([c.close for c in candles], dtype=float)
    if closes.size < 2:
        return 0.0
    window = closes[-lookback:] if closes.size >= lookback else closes
    if window.size < 2:
        return 0.0
    x = np.arange(window.size, dtype=float)
    slope = float(np.polyfit(x, window, 1)[0])
    return slope


def is_saturation(
    candles: Sequence[Candle], eps: float, lookback: int = 3
) -> bool:
    """|dP/dt| son kapanisa oranla 'eps' altindaysa fiyat doymus (yatay) demektir.

    Post-spike mean-reversion: sert hareket sonrasi ivmenin sifira yaklastigi an.
    """
    if not candles or candles[-1].close <= 0:
        return False
    vel = price_velocity(candles, lookback)
    return abs(vel) / candles[-1].close < eps


# ----------------------------------------------------------------------------
# Kalan sure (Time Decay) filtresi
# ----------------------------------------------------------------------------


def time_decay_ok(
    end_ts: float, now: float, duration: float, pct: float = 0.10
) -> bool:
    """Kalan sure, toplam surenin 'pct' oranindan FAZLA ise True (islem guvenli).

    Vadeye son %pct kala False doner (giris yapma). duration <= 0 ise guvenli
    sayilmaz.
    """
    if duration <= 0:
        return False
    remaining = end_ts - now
    if remaining <= 0:
        return False
    return (remaining / duration) > pct


# ----------------------------------------------------------------------------
# Birlesik olcum (strateji girdisi)
# ----------------------------------------------------------------------------


def build_analytics(
    book_up: OrderBook | None,
    candles: Sequence[Candle],
    implied_vol: float,
    *,
    atr_period: int = 14,
    adx_period: int = 14,
    saturation_eps: float = 0.02,
    squeeze_pct: float = 0.02,
) -> Analytics:
    """Ham veriden turetilmis olculeri toplar. Yeterli veri yoksa ready=False."""
    obi = book_obi(book_up) if book_up is not None else 0.0
    have_candles = len(candles) >= adx_period + 1
    a = Analytics(
        obi=obi,
        atr=compute_atr(candles, atr_period),
        atr_pct=atr_pct(candles, atr_period),
        adx=compute_adx(candles, adx_period),
        bb_squeeze=bollinger_squeeze(candles, squeeze_pct=squeeze_pct),
        price_velocity=price_velocity(candles),
        saturation=is_saturation(candles, saturation_eps),
        implied_vol=implied_vol,
        ready=bool(book_up is not None and have_candles),
    )
    return a


def market_time_decay_ok(meta: MarketMeta, now: float, pct: float) -> bool:
    return time_decay_ok(meta.end_ts, now, meta.duration_sec, pct)
