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

    # ----- Chainlink OFFICIAL reference (5m/15m) — Polymarket RTDS Data Stream -----
    # Market rules point to Chainlink Data Streams. The official public RTDS topic is
    # `crypto_prices_chainlink`; Polygon push-feed aggregators are NOT used as official PTB.
    chainlink_enabled: bool = Field(default=True, alias="CHAINLINK_ENABLED")
    rtds_ws_url: str = Field(
        default="wss://ws-live-data.polymarket.com", alias="RTDS_WS_URL"
    )
    rtds_ping_sec: float = Field(default=5.0, alias="RTDS_PING_SEC")
    rtds_debug_raw: bool = Field(default=False, alias="RTDS_DEBUG_RAW")
    max_reference_open_alignment_ms: float = Field(
        default=5000.0, alias="MAX_REFERENCE_OPEN_ALIGNMENT_MS"
    )
    max_reference_source_age_ms: float = Field(
        default=8000.0, alias="MAX_REFERENCE_SOURCE_AGE_MS"
    )

    # ----- Web dashboard -----
    web_enabled: bool = Field(default=True, alias="WEB_ENABLED")
    web_host: str = Field(default="0.0.0.0", alias="WEB_HOST")
    web_port: int = Field(default=8091, alias="WEB_PORT")

    # ----- Kapsam (12 combo) -----
    assets_csv: str = Field(default="BTC,ETH,SOL,XRP", alias="ASSETS")
    horizons_csv: str = Field(default="5m,15m,1h", alias="HORIZONS")

    # ----- Direct Binance WS (hizli kaynak) -----
    binance_ws_base: str = Field(
        default="wss://stream.binance.com:9443/stream", alias="BINANCE_WS_BASE"
    )
    binance_rest_base: str = Field(
        default="https://api.binance.com", alias="BINANCE_REST_BASE"
    )
    binance_depth_ms: int = Field(default=100, alias="BINANCE_DEPTH_MS")
    binance_book_snapshot_limit: int = Field(
        default=1000, alias="BINANCE_BOOK_SNAPSHOT_LIMIT"
    )
    ring_buffer_max: int = Field(default=4000, alias="RING_BUFFER_MAX")

    # ----- Polymarket Gamma (discovery + resolution) -----
    gamma_host: str = Field(
        default="https://gamma-api.polymarket.com", alias="GAMMA_HOST"
    )
    gamma_poll_sec: int = Field(default=10, alias="GAMMA_POLL_SEC")
    gamma_event_limit: int = Field(default=200, alias="GAMMA_EVENT_LIMIT")
    resolution_poll_sec: int = Field(default=30, alias="RESOLUTION_POLL_SEC")

    # ----- Polymarket CLOB (teyit) -----
    clob_ws_url: str = Field(
        default="wss://ws-subscriptions-clob.polymarket.com/ws/market",
        alias="CLOB_WS_URL",
    )
    clob_host: str = Field(default="https://clob.polymarket.com", alias="CLOB_HOST")

    # ----- Reference / PTB -----
    poly_price_host: str = Field(default="https://polymarket.com", alias="POLY_PRICE_HOST")
    chainlink_ref_enabled: bool = Field(default=True, alias="CHAINLINK_REF_ENABLED")

    # ----- Snapshot / recorder -----
    snapshot_checkpoints_csv: str = Field(
        default="240,180,120,90,60,45,30,20,10,5", alias="SNAPSHOT_CHECKPOINTS"
    )
    snapshot_loop_ms: int = Field(default=500, alias="SNAPSHOT_LOOP_MS")

    # ----- Freshness (ms) -----
    max_spot_age_ms: float = Field(default=2500, alias="MAX_SPOT_AGE_MS")
    max_book_age_ms: float = Field(default=3000, alias="MAX_BOOK_AGE_MS")
    max_transport_age_ms: float = Field(default=5000, alias="MAX_TRANSPORT_AGE_MS")
    max_source_age_ms: float = Field(default=5000, alias="MAX_SOURCE_AGE_MS")
    max_clob_age_ms: float = Field(default=6000, alias="MAX_CLOB_AGE_MS")
    max_reference_age_ms: float = Field(default=8000, alias="MAX_REFERENCE_AGE_MS")
    max_clock_skew_ms: float = Field(default=3000, alias="MAX_CLOCK_SKEW_MS")

    # ----- Backfill -----
    backfill_resolved_markets: int = Field(default=0, alias="BACKFILL_RESOLVED_MARKETS")

    # ----- Checkpoint setleri -----
    checkpoints_5m_csv: str = Field(
        default="240,180,150,120,90,60,45,30,20,10", alias="CHECKPOINTS_5M"
    )
    checkpoints_15m_csv: str = Field(
        default="840,720,600,480,360,240,120,60,30", alias="CHECKPOINTS_15M"
    )
    checkpoints_1h_csv: str = Field(
        default="3300,3000,2400,1800,1200,900,600,300,120,60", alias="CHECKPOINTS_1H"
    )

    # ----- Reconnect -----
    backoff_base_sec: float = Field(default=1.0, alias="BACKOFF_BASE_SEC")
    backoff_factor: float = Field(default=2.0, alias="BACKOFF_FACTOR")
    backoff_cap_sec: float = Field(default=30.0, alias="BACKOFF_CAP_SEC")
    ws_recv_timeout_sec: float = Field(default=30.0, alias="WS_RECV_TIMEOUT_SEC")

    # ----- Model (P2) -----
    per_combo_model_min_markets: int = Field(
        default=200, alias="PER_COMBO_MODEL_MIN_MARKETS"
    )
    min_markets_for_stats: int = Field(default=30, alias="MIN_MARKETS_FOR_STATS")

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
        csv = {
            "5m": self.checkpoints_5m_csv,
            "15m": self.checkpoints_15m_csv,
            "1h": self.checkpoints_1h_csv,
        }.get(horizon, self.snapshot_checkpoints_csv)
        out = [int(t.strip()) for t in csv.split(",") if t.strip().isdigit()]
        return sorted(set(out), reverse=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
