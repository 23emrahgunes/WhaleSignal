"""Paylasilan tip tanimlari (dataclass + enum).

Tum modullerin ortak kullandigi veri yapilari burada. Saf veri; is mantigi yok.
`typing` + `dataclasses` ile tip guvenligi saglanir.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Side(str, Enum):
    """Emir yonu (Polymarket CLOB)."""

    BUY = "BUY"
    SELL = "SELL"


class Outcome(str, Enum):
    """Ikili piyasa bacagi."""

    UP = "UP"
    DOWN = "DOWN"


class ExecMode(str, Enum):
    """Yurutme modu.

    SIM  : gercek istemci yok; emirler yalniz simulatorde islenir.
    DRY  : resmi kutuphane emri IMZALAR ama POST etmez (creds/imza dogrulama).
    LIVE : gercek emir CLOB'a gonderilir. Yalniz acik konfig ile; asla otomatik.
    """

    SIM = "SIM"
    DRY = "DRY"
    LIVE = "LIVE"


class LegStatus(str, Enum):
    """Cift-bacak yasam dongusu durumu."""

    IDLE = "IDLE"  # aktif emir yok
    RESTING = "RESTING"  # iki bacak da resting, dolum bekliyor
    ONE_LEG = "ONE_LEG"  # bir bacak doldu, diger bekliyor (adverse-selection riski)
    LOCKED = "LOCKED"  # iki bacak da doldu (box kilitlendi)
    FLATTEN = "FLATTEN"  # tek bacak zamana yenildi -> iptal/hedge/cikis


@dataclass(frozen=True)
class BookLevel:
    """Emir defteri tek seviye (fiyat + toplam boyut)."""

    price: float
    size: float


@dataclass
class OrderBook:
    """Bir token'in canli emir defteri anlik goruntusu."""

    token_id: str
    bids: list[BookLevel] = field(default_factory=list)
    asks: list[BookLevel] = field(default_factory=list)
    ts: float = field(default_factory=time.time)

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def midpoint(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return 0.5 * (self.best_bid + self.best_ask)


@dataclass(frozen=True)
class Candle:
    """OHLCV mum (Binance kline)."""

    open_time: int  # ms
    open: float
    high: float
    low: float
    close: float
    volume: float
    closed: bool = True


@dataclass
class MarketMeta:
    """Gamma API'den market metadata."""

    condition_id: str
    question: str
    up_token_id: str
    down_token_id: str
    start_ts: float  # unix sn
    end_ts: float  # unix sn

    @property
    def duration_sec(self) -> float:
        return max(1.0, self.end_ts - self.start_ts)

    def remaining_sec(self, now: Optional[float] = None) -> float:
        now = time.time() if now is None else now
        return self.end_ts - now


@dataclass
class Analytics:
    """analytics_engine'in urettigi turetilmis olculer (strateji girdisi)."""

    obi: float = 0.0
    atr: float = 0.0
    atr_pct: float = 0.0
    adx: float = 0.0
    bb_squeeze: bool = False
    price_velocity: float = 0.0  # dP/dt (fiyat/sn)
    saturation: bool = False  # dP/dt ~ 0 (post-spike mean-reversion ani)
    implied_vol: float = 0.0  # Deribit DVOL/IV (yuzde)
    ready: bool = False  # yeterli veri var mi


@dataclass
class MarketState:
    """Strateji tick'ine gecen birlesik anlik durum."""

    meta: Optional[MarketMeta]
    book_up: Optional[OrderBook]
    book_down: Optional[OrderBook]
    analytics: Analytics
    now: float = field(default_factory=time.time)
