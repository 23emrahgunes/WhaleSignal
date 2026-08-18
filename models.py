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
    # --- P1 plumbing invariant sebepleri ---
    CLOB_MISSING = "CLOB_MISSING"  # gercek UP/DOWN kotasi yok (0.505 fallback YOK)
    PTB_MISSING = "PTB_MISSING"  # reference_open/PTB cozulemedi
    UNSAFE_TIME_METADATA = "UNSAFE_TIME_METADATA"  # canonical TTE/pencere gecersiz
    CLOCK_UNSYNC = "CLOCK_UNSYNC"  # yerel saat kaymis
    MODEL_NOT_TRAINED = "MODEL_NOT_TRAINED"  # P1: model yok -> tahmin degil


class ResolutionType(str, Enum):
    """Marketin nasil resolve oldugu (zorunlu metadata)."""

    CHAINLINK = "CHAINLINK"  # Chainlink referans (window/anlik)
    CHAINLINK_TWAP = "CHAINLINK_TWAP"  # Chainlink TWAP (pencere+gozlem ani)
    BINANCE_CANDLE = "BINANCE_CANDLE"
    UMA = "UMA"
    UNKNOWN = "UNKNOWN"


class TimeStatus(str, Enum):
    """Canonical market zamaninin gecerliligi."""

    OK = "OK"
    UNSAFE_TIME_METADATA = "UNSAFE_TIME_METADATA"


class DiscoveryStatus(str, Enum):
    """Bir combo icin market kesif durumu (generic 'YOK' yerine)."""

    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    DISCOVERY_ERROR = "DISCOVERY_ERROR"
    AMBIGUOUS = "AMBIGUOUS"


class LabelStatus(str, Enum):
    """Settlement etiket durumu."""

    UNKNOWN = "UNKNOWN"  # explicit official yok -> labeled sayma
    MATCH = "MATCH"  # official == computed/sanity
    MISMATCH = "MISMATCH"  # celiski -> training-disi


class QStatus(str, Enum):
    """Tek quality boyutu durumu."""

    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"
    WAITING = "WAITING"  # veri henuz gelmedi (kotali degil)


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
    # start_ts/end_ts = HAM metadata (Gamma endDate/startDate). Cross-check icin.
    start_ts: float
    end_ts: float
    # --- ZORUNLU resolution metadata ---
    resolution_source: str  # ham kaynak metni (Gamma resolutionSource / rules)
    resolution_type: ResolutionType
    # --- CANONICAL zaman (5m/15m slug-unix'ten; otoriter). None -> post_init doldurur ---
    market_start_ts: Optional[float] = None
    market_end_ts: Optional[float] = None
    time_status: TimeStatus = TimeStatus.OK
    # --- reference/PTB (resolution-tipine gore; generic openPrice DEGIL) ---
    reference_open: Optional[float] = None
    reference_current: Optional[float] = None
    reference_updated_at: Optional[float] = None
    twap_window_sec: Optional[int] = None  # Chainlink TWAP ise
    twap_observation_ts: Optional[float] = None
    # --- discovery + settlement ---
    discovery_status: DiscoveryStatus = DiscoveryStatus.FOUND
    resolved: bool = False
    resolved_outcome: Optional[Decision] = None  # official (explicit metadata)
    official_result: Optional[Decision] = None  # explicit official (birincil label)
    computed_result: Optional[Decision] = None  # yerel audit (spot vs reference)
    label_status: LabelStatus = LabelStatus.UNKNOWN
    discovered_ts: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        # canonical zaman verilmediyse ham metadata'dan turet (geriye uyum)
        if self.market_start_ts is None:
            self.market_start_ts = self.start_ts
        if self.market_end_ts is None:
            self.market_end_ts = self.end_ts

    @property
    def market_id(self) -> str:
        """Canonical runtime state anahtari. condition_id yoksa slug."""
        return self.condition_id or self.slug

    @property
    def duration_sec(self) -> float:
        return max(1.0, (self.market_end_ts or self.end_ts) - (self.market_start_ts or self.start_ts))

    def remaining_sec(self, now: Optional[float] = None) -> float:
        now = time.time() if now is None else now
        return (self.market_end_ts if self.market_end_ts is not None else self.end_ts) - now

    def market_age_sec(self, now: Optional[float] = None) -> float:
        now = time.time() if now is None else now
        return now - (self.market_start_ts if self.market_start_ts is not None else self.start_ts)

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
    seconds_remaining: float  # canonical TTE
    # canonical zaman
    market_start: Optional[float] = None
    market_end: Optional[float] = None
    tte_sec: Optional[float] = None
    # fiyat / referans
    spot_price: Optional[float] = None  # direct Binance current
    reference_price: Optional[float] = None  # PTB (resolution-tipine gore)
    distance_usd: Optional[float] = None
    distance_bps: Optional[float] = None
    # CLOB (up + down, bid/ask/mid; 0.505 fallback YOK)
    up_bid: Optional[float] = None
    up_ask: Optional[float] = None
    up_mid: Optional[float] = None
    down_bid: Optional[float] = None
    down_ask: Optional[float] = None
    down_mid: Optional[float] = None
    clob_spread: Optional[float] = None
    # veri sagligi (ms) — AYRISIK
    spot_age_ms: Optional[float] = None
    book_age_ms: Optional[float] = None
    transport_age_ms: Optional[float] = None
    source_age_ms: Optional[float] = None
    clob_age_ms: Optional[float] = None
    reference_age_ms: Optional[float] = None
    # quality
    quality_status: str = "UNKNOWN"
    prediction_ready: bool = False
    # P2 genislemesi icin serbest alan
    extra: dict = field(default_factory=dict)


@dataclass
class QualityReport:
    """7 boyutlu quality + prediction_ready (her kart icin ayri gosterilir)."""

    time: "QStatus" = None  # type: ignore[assignment]
    market: "QStatus" = None  # type: ignore[assignment]
    tokens: "QStatus" = None  # type: ignore[assignment]
    clob: "QStatus" = None  # type: ignore[assignment]
    reference: "QStatus" = None  # type: ignore[assignment]
    clock: "QStatus" = None  # type: ignore[assignment]
    model: "QStatus" = None  # type: ignore[assignment]
    prediction_ready: bool = False
    snapshot_recordable: bool = False  # time+market+tokens OK -> ham row yaz
    abstain_reason: AbstainReason = AbstainReason.NONE
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        for name in ("time", "market", "tokens", "clob", "reference", "clock", "model"):
            if getattr(self, name) is None:
                setattr(self, name, QStatus.OK)

    def dims(self) -> dict:
        return {
            "time": self.time.value,
            "market": self.market.value,
            "tokens": self.tokens.value,
            "clob": self.clob.value,
            "reference": self.reference.value,
            "clock": self.clock.value,
            "model": self.model.value,
        }


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
