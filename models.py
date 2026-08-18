"""Paylasilan tip tanimlari (dataclass + enum).

Direction Engine vNext'in tum modullerinin ortak kullandigi veri yapilari.
Saf veri; is mantigi yok. `dataclasses` + `enum` ile tip guvenligi.

SHADOW motor: burada emir/imza/execution tipi YOK. Sadece market referansi,
feature snapshot, tahmin ve kayit tipleri.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Asset(str, Enum):
    """Izlenen varliklar (4 adet)."""

    BTC = "BTC"
    ETH = "ETH"
    SOL = "SOL"
    XRP = "XRP"


class Horizon(str, Enum):
    """Tahmin ufuklari (3 adet)."""

    H5M = "5m"
    H15M = "15m"
    H1H = "1h"

    @property
    def seconds(self) -> int:
        return {"5m": 300, "15m": 900, "1h": 3600}[self.value]


class Decision(str, Enum):
    """Yon karari. Model "bilmiyorum" diyebilmeli -> ABSTAIN."""

    UP = "UP"
    DOWN = "DOWN"
    ABSTAIN = "ABSTAIN"


class Regime(str, Enum):
    """Volatilite/mikroyapi rejimi (predictability girdisi)."""

    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    CHOP = "CHOP"
    CHAOTIC = "CHAOTIC"
    HIGH_VOL = "HIGH_VOL"
    UNSAFE = "UNSAFE"
    UNKNOWN = "UNKNOWN"


class AbstainReason(str, Enum):
    """ABSTAIN gerekcesi (dashboard WHY + kayit)."""

    NONE = "NONE"
    STALE_DATA = "STALE_DATA"  # feed bayat/kopuk
    NO_MARKET = "NO_MARKET"  # combo Polymarket'te yok
    LOW_PREDICTABILITY = "LOW_PREDICTABILITY"
    CHAOTIC = "CHAOTIC"
    HIGH_VOL = "HIGH_VOL"
    UNSAFE = "UNSAFE"
    FEATURE_CONFLICT = "FEATURE_CONFLICT"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"  # warmup / az ornek
    NO_RESOLUTION_META = "NO_RESOLUTION_META"  # resolution_source/type cozulemedi


class ResolutionType(str, Enum):
    """Marketin nasil resolve oldugu (zorunlu metadata)."""

    CHAINLINK = "CHAINLINK"
    BINANCE_CANDLE = "BINANCE_CANDLE"
    UMA = "UMA"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class AssetHorizon:
    """12 kombinasyondan biri (BTC×5m ... XRP×1h). Sozluk/anahtar guvenli."""

    asset: Asset
    horizon: Horizon

    @property
    def key(self) -> str:
        return f"{self.asset.value}:{self.horizon.value}"

    @property
    def binance_symbol(self) -> str:
        return f"{self.asset.value}USDT"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.key


def all_combos() -> list[AssetHorizon]:
    """12 kombinasyonun sabit sirali listesi."""
    return [AssetHorizon(a, h) for a in Asset for h in Horizon]


@dataclass
class MarketRef:
    """Bir aktif Polymarket up/down marketinin referansi (discovery ciktisi).

    resolution_source ve resolution_type ZORUNLU metadata'dir. Cozulemezse
    market egitim-disi/ABSTAIN isaretlenir (recorder label uretemez).
    """

    combo: AssetHorizon
    condition_id: str
    slug: str
    question: str
    up_token_id: str
    down_token_id: str
    start_ts: float
    end_ts: float
    # --- ZORUNLU resolution metadata ---
    resolution_source: str  # ham kaynak metni (Gamma resolutionSource / rules)
    resolution_type: ResolutionType
    # --- resmi resolved sonuc (kapanistan sonra doldurulur) ---
    resolved: bool = False
    resolved_outcome: Optional[Decision] = None  # UP | DOWN (resmi)
    discovered_ts: float = field(default_factory=time.time)

    @property
    def duration_sec(self) -> float:
        return max(1.0, self.end_ts - self.start_ts)

    def remaining_sec(self, now: Optional[float] = None) -> float:
        now = time.time() if now is None else now
        return self.end_ts - now

    @property
    def has_resolution_meta(self) -> bool:
        return self.resolution_type != ResolutionType.UNKNOWN and bool(
            self.resolution_source
        )


@dataclass(frozen=True)
class BookLevel:
    """Emir defteri tek seviye (fiyat + boyut)."""

    price: float
    size: float


@dataclass
class LocalBook:
    """Diff-depth ile senkronize edilmis yerel emir defteri (gercek OFI icin).

    Binance depth diff akisi + REST snapshot ile tutarli tutulur. best_bid/ask
    ve toplam derinlik OFI/OBI hesabina beslenir.
    """

    symbol: str
    bids: dict[float, float] = field(default_factory=dict)  # price -> size
    asks: dict[float, float] = field(default_factory=dict)
    last_update_id: int = 0
    ts: float = field(default_factory=time.time)
    synced: bool = False  # REST snapshot ile ilk senkron tamamlandi mi

    @property
    def best_bid(self) -> Optional[float]:
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return min(self.asks) if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        b, a = self.best_bid, self.best_ask
        if b is None or a is None:
            return None
        return 0.5 * (b + a)

    def top_levels(self, side: str, n: int = 20) -> list[BookLevel]:
        if side == "bid":
            prices = sorted(self.bids, reverse=True)[:n]
            return [BookLevel(p, self.bids[p]) for p in prices]
        prices = sorted(self.asks)[:n]
        return [BookLevel(p, self.asks[p]) for p in prices]


@dataclass(frozen=True)
class Trade:
    """Tek agresif islem (Binance @trade). is_buyer_maker=True -> agresif SATIS."""

    price: float
    qty: float
    ts_ms: int
    is_buyer_maker: bool

    @property
    def signed_qty(self) -> float:
        # agresif ALIS (+), agresif SATIS (-). taker yonu.
        return -self.qty if self.is_buyer_maker else self.qty


@dataclass(frozen=True)
class ClobQuote:
    """Bir up/down token'inin CLOB anlik kotasi."""

    token_id: str
    best_bid: Optional[float]
    best_ask: Optional[float]
    ts: float = field(default_factory=time.time)

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return 0.5 * (self.best_bid + self.best_ask)


@dataclass
class FeatureSnapshot:
    """Bir combo icin bir checkpoint'te uretilen ham/turetilmis feature seti.

    P1'de yalniz temel alanlar dolar (fiyat/PTB/CLOB/freshness). P2'de returns,
    momentum persistence, flow, volatility, OFI eklenir. dict tabanli genisleyebilir.
    """

    combo: AssetHorizon
    ts: float
    seconds_remaining: float
    # fiyat / referans
    spot_price: Optional[float] = None  # direct Binance current
    reference_price: Optional[float] = None  # PTB (horizon adaptorune gore)
    distance_usd: Optional[float] = None
    distance_bps: Optional[float] = None
    # CLOB
    up_mid: Optional[float] = None
    down_mid: Optional[float] = None
    clob_spread: Optional[float] = None
    # veri sagligi (ms)
    spot_age_ms: Optional[float] = None
    book_age_ms: Optional[float] = None
    clob_age_ms: Optional[float] = None
    reference_age_ms: Optional[float] = None
    # P2 genislemesi icin serbest alan
    extra: dict = field(default_factory=dict)


@dataclass
class Prediction:
    """Model ciktisi: olasilik + guven + rejim + karar + gerekce."""

    combo: AssetHorizon
    ts: float
    p_up: float = 0.5
    p_down: float = 0.5
    confidence: float = 0.0  # 0..1
    predictability: float = 0.0  # 0..1
    regime: Regime = Regime.UNKNOWN
    decision: Decision = Decision.ABSTAIN
    abstain_reason: AbstainReason = AbstainReason.INSUFFICIENT_DATA
    reasons: list[str] = field(default_factory=list)  # WHY (dashboard)
    # kiyas: Polymarket implied (up_mid) — analytics-only
    market_implied_up: Optional[float] = None

    @property
    def price_edge(self) -> Optional[float]:
        """P_model(UP) - P_market(UP). Yalniz analitik; canli emir YOK."""
        if self.market_implied_up is None:
            return None
        return self.p_up - self.market_implied_up
