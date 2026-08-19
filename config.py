"""Merkezi konfigurasyon (Pydantic v2 + pydantic-settings).

Direction Engine vNext — SHADOW motor. Tum uc noktalar, esik degerleri, snapshot
checkpoint'leri ve freshness limitleri buradan yonetilir; `.env`'den okunur.

ONEMLI: Bu serviste canli emir/imza/gizli-anahtar YOK. CLOB private key vb.
alanlar burada TANIMLI DEGIL — kasitli. Servis yalniz okur/tahmin eder/kaydeder.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        env_ignore_empty=True,
    )

    # ----- Genel -----
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    db_path: str = Field(default="data/direction_engine.sqlite", alias="DB_PATH")

    # ----- FAZ KILIDI (P1: yalniz veri; TRAINING/CALIBRATION YASAK) -----
    phase: str = Field(default="P1", alias="PHASE")
    model_training_enabled: bool = Field(default=False, alias="MODEL_TRAINING_ENABLED")
    calibration_enabled: bool = Field(default=False, alias="CALIBRATION_ENABLED")

    def enforce_phase_lock(self) -> None:
        """PHASE=P1 iken training/calibration TRUE ise FATAL (SystemExit)."""
        if self.phase.strip().upper() == "P1" and (
            self.model_training_enabled or self.calibration_enabled
        ):
            raise SystemExit(
                "FATAL CONFIG ERROR: PHASE=P1 iken MODEL_TRAINING_ENABLED / "
                "CALIBRATION_ENABLED true olamaz (P1 = yalniz veri, training YOK)."
            )

    @property
    def training_active(self) -> bool:
        return self.phase.strip().upper() != "P1" and self.model_training_enabled

    @property
    def calibration_active(self) -> bool:
        return self.phase.strip().upper() != "P1" and self.calibration_enabled

    # ----- Chainlink OFFICIAL reference (5m/15m) — Polygon ON-CHAIN aggregator -----
    # Marketlerin resolve oldugu authoritative Chainlink kaynagi. Public Polygon RPC ile
    # aggregator latestRoundData okunur (Binance proxy DEGIL). Geoblock disi, her yerden calisir.
    chainlink_enabled: bool = Field(default=True, alias="CHAINLINK_ENABLED")
    polygon_rpc_csv: str = Field(
        default="https://polygon-bor-rpc.publicnode.com,https://polygon.drpc.org,https://1rpc.io/matic",
        alias="POLYGON_RPC_URLS",
    )
    chainlink_poll_sec: float = Field(default=6.0, alias="CHAINLINK_POLL_SEC")

    def polygon_rpc_urls(self) -> list[str]:
        return [u.strip() for u in self.polygon_rpc_csv.split(",") if u.strip()]
    # yeni 5m/15m market bu kadar saniyeden gencse acilis referansi RTDS'ten yakalanir
    open_capture_window_sec: float = Field(default=30.0, alias="OPEN_CAPTURE_WINDOW_SEC")
    max_reference_source_age_ms: float = Field(
        default=15000, alias="MAX_REFERENCE_SOURCE_AGE_MS"  # poll(6s)+Chainlink heartbeat kapsar
    )

    # ----- Web dashboard -----
    web_enabled: bool = Field(default=True, alias="WEB_ENABLED")
    web_host: str = Field(default="0.0.0.0", alias="WEB_HOST")
    web_port: int = Field(default=8091, alias="WEB_PORT")

    # ----- Kapsam (12 combo) -----
    # Bos = tum varsayilan varliklar/ufuklar. Alt-kume test icin virgullu liste.
    assets_csv: str = Field(default="BTC,ETH,SOL,XRP", alias="ASSETS")
    horizons_csv: str = Field(default="5m,15m,1h", alias="HORIZONS")

    # ----- Direct Binance WS (hizli kaynak ~124ms) -----
    binance_ws_base: str = Field(
        default="wss://stream.binance.com:9443/stream", alias="BINANCE_WS_BASE"
    )
    binance_rest_base: str = Field(
        default="https://api.binance.com", alias="BINANCE_REST_BASE"
    )
    # diff-depth hizi: @depth@100ms (gercek OFI icin senkron local book)
    binance_depth_ms: int = Field(default=100, alias="BINANCE_DEPTH_MS")
    binance_book_snapshot_limit: int = Field(
        default=1000, alias="BINANCE_BOOK_SNAPSHOT_LIMIT"
    )
    ring_buffer_max: int = Field(default=4000, alias="RING_BUFFER_MAX")  # trade/mid ring

    # ----- Polymarket Gamma (discovery + resolution) -----
    gamma_host: str = Field(
        default="https://gamma-api.polymarket.com", alias="GAMMA_HOST"
    )
    gamma_poll_sec: int = Field(default=10, alias="GAMMA_POLL_SEC")
    # active-event discovery listeleme sayfa boyutu
    gamma_event_limit: int = Field(default=200, alias="GAMMA_EVENT_LIMIT")
    # resolved sonuc icin kapanmis market yoklama araligi
    resolution_poll_sec: int = Field(default=30, alias="RESOLUTION_POLL_SEC")

    # ----- Polymarket CLOB (teyit) -----
    clob_ws_url: str = Field(
        default="wss://ws-subscriptions-clob.polymarket.com/ws/market",
        alias="CLOB_WS_URL",
    )
    clob_host: str = Field(default="https://clob.polymarket.com", alias="CLOB_HOST")

    # ----- Reference / PTB kaynaklari (horizon'a gore adaptor) -----
    # 5m/15m -> Chainlink-oriented; 1h -> Binance-candle-oriented.
    # Chainlink referans fiyati Polymarket'in crypto-price ucundan cekilir (settlement-uyumlu).
    poly_price_host: str = Field(
        default="https://polymarket.com", alias="POLY_PRICE_HOST"
    )
    chainlink_ref_enabled: bool = Field(default=True, alias="CHAINLINK_REF_ENABLED")

    # ----- Snapshot / recorder -----
    # Kapanisa kalan saniyeye gore checkpoint'ler (hard-code DEGIL). Her combo icin
    # bu esiklere ilk kez inildiginde recorder bir row yazar.
    snapshot_checkpoints_csv: str = Field(
        default="240,180,120,90,60,45,30,20,10,5", alias="SNAPSHOT_CHECKPOINTS"
    )
    snapshot_loop_ms: int = Field(default=500, alias="SNAPSHOT_LOOP_MS")

    # ----- Freshness (ms) — AYRISIK olculer -----
    # Feed health = transport (son WS frame) + source event tazeligi. Seyrek trade
    # STALE saydirmaz; bu yuzden spot yerine transport/source esikleri kullanilir.
    max_spot_age_ms: float = Field(default=2500, alias="MAX_SPOT_AGE_MS")
    max_book_age_ms: float = Field(default=3000, alias="MAX_BOOK_AGE_MS")
    max_transport_age_ms: float = Field(default=5000, alias="MAX_TRANSPORT_AGE_MS")
    max_source_age_ms: float = Field(default=5000, alias="MAX_SOURCE_AGE_MS")
    max_clob_age_ms: float = Field(default=6000, alias="MAX_CLOB_AGE_MS")
    max_reference_age_ms: float = Field(default=8000, alias="MAX_REFERENCE_AGE_MS")
    # clock: yerel saat Binance serverTime'dan bu kadar kayarsa ABSTAIN(CLOCK_UNSYNC)
    max_clock_skew_ms: float = Field(default=3000, alias="MAX_CLOCK_SKEW_MS")

    # ----- Backfill (P1 settlement/label testi; snapshot/feature URETMEZ) -----
    backfill_resolved_markets: int = Field(default=0, alias="BACKFILL_RESOLVED_MARKETS")

    # ----- Checkpoint setleri (horizon bazli; edge-crossing) -----
    checkpoints_5m_csv: str = Field(
        default="240,180,150,120,90,60,45,30,20,10", alias="CHECKPOINTS_5M"
    )
    checkpoints_15m_csv: str = Field(
        default="840,720,600,480,360,240,120,60,30", alias="CHECKPOINTS_15M"
    )
    checkpoints_1h_csv: str = Field(
        default="3300,3000,2400,1800,1200,900,600,300,120,60", alias="CHECKPOINTS_1H"
    )

    # ----- Reconnect (exponential backoff) -----
    backoff_base_sec: float = Field(default=1.0, alias="BACKOFF_BASE_SEC")
    backoff_factor: float = Field(default=2.0, alias="BACKOFF_FACTOR")
    backoff_cap_sec: float = Field(default=30.0, alias="BACKOFF_CAP_SEC")
    ws_recv_timeout_sec: float = Field(default=30.0, alias="WS_RECV_TIMEOUT_SEC")

    # ----- Model (P2) -----
    # Yeterli market birikene kadar SHARED model + 12 calibration bucket.
    # Bu esik asilinca 12 AYRI model'e gecilir (offline train karari).
    per_combo_model_min_markets: int = Field(
        default=200, alias="PER_COMBO_MODEL_MIN_MARKETS"
    )
    # n<bu iken dashboard'da ustunluk/winrate YAZMA (uydurma yok).
    min_markets_for_stats: int = Field(default=30, alias="MIN_MARKETS_FOR_STATS")

    # ---- turetilmis yardimcilar ----
    def assets(self) -> list[str]:
        return [a.strip().upper() for a in self.assets_csv.split(",") if a.strip()]

    def horizons(self) -> list[str]:
        return [h.strip().lower() for h in self.horizons_csv.split(",") if h.strip()]

    def snapshot_checkpoints(self) -> list[int]:
        out: list[int] = []
        for tok in self.snapshot_checkpoints_csv.split(","):
            tok = tok.strip()
            if tok.isdigit():
                out.append(int(tok))
        return sorted(set(out), reverse=True)

    def checkpoints_for(self, horizon: str) -> list[int]:
        """Horizon'a gore checkpoint seti (desc). Bilinmeyen -> generic."""
        csv = {
            "5m": self.checkpoints_5m_csv,
            "15m": self.checkpoints_15m_csv,
            "1h": self.checkpoints_1h_csv,
        }.get(horizon, self.snapshot_checkpoints_csv)
        out = [int(t.strip()) for t in csv.split(",") if t.strip().isdigit()]
        return sorted(set(out), reverse=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Surec boyunca tekil ayar nesnesi."""
    return Settings()
