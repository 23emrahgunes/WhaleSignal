"""Central configuration for Direction Engine vNext.

Phase capabilities are explicit and monotonic:

P1   data plumbing only
P2.1 feature generation
P2.2 predictability/regime
P2.3 shadow baseline direction models
P2.4 probability calibration + learned decision thresholds
P2.5 shadow forecast recording + resolved analytics

There is no execution/private-key/order configuration in this service.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


_PHASE_RANKS = {
    "P1": 10,
    "P2.1": 21,
    "P2_1": 21,
    "P21": 21,
    "P2.2": 22,
    "P2_2": 22,
    "P22": 22,
    "P2.3": 23,
    "P2_3": 23,
    "P23": 23,
    "P2.4": 24,
    "P2_4": 24,
    "P24": 24,
    "P2.5": 25,
    "P2_5": 25,
    "P25": 25,
    "P3": 30,
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        env_ignore_empty=True,
    )

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    db_path: str = Field(default="data/direction_engine.sqlite", alias="DB_PATH")

    # ----- Phase/capability lock -----
    phase: str = Field(default="P2.5", alias="PHASE")
    model_training_enabled: bool = Field(default=False, alias="MODEL_TRAINING_ENABLED")
    calibration_enabled: bool = Field(default=False, alias="CALIBRATION_ENABLED")
    forecast_recording_enabled: bool = Field(default=True, alias="FORECAST_RECORDING_ENABLED")

    @property
    def phase_rank(self) -> int:
        return _PHASE_RANKS.get(self.phase.strip().upper(), -1)

    @property
    def feature_only_phase(self) -> bool:
        return self.phase_rank <= 21

    @property
    def feature_engine_active(self) -> bool:
        return self.phase_rank >= 21

    @property
    def predictability_active(self) -> bool:
        return self.phase_rank >= 22

    @property
    def model_inference_active(self) -> bool:
        return self.phase_rank >= 23

    @property
    def calibration_active(self) -> bool:
        return self.phase_rank >= 24 and self.calibration_enabled

    @property
    def forecast_recording_active(self) -> bool:
        return self.phase_rank >= 25 and self.forecast_recording_enabled

    @property
    def training_active(self) -> bool:
        return self.phase_rank >= 23 and self.model_training_enabled

    def enforce_phase_lock(self) -> None:
        if self.phase_rank < 0:
            raise SystemExit(f"FATAL CONFIG ERROR: bilinmeyen PHASE={self.phase!r}")
        if self.model_training_enabled and self.phase_rank < 23:
            raise SystemExit(
                "FATAL CONFIG ERROR: model training yalniz P2.3+ fazinda acilabilir."
            )
        if self.calibration_enabled and self.phase_rank < 24:
            raise SystemExit(
                "FATAL CONFIG ERROR: calibration yalniz P2.4+ fazinda acilabilir."
            )

    # ----- Artifacts/model policy -----
    model_path: str = Field(
        default="models/direction_model.pkl", alias="MODEL_PATH"
    )
    calibration_path: str = Field(
        default="models/calibration_book.pkl", alias="CALIBRATION_PATH"
    )
    model_min_markets_predict: int = Field(
        default=20, alias="MODEL_MIN_MARKETS_PREDICT"
    )
    per_combo_model_min_markets: int = Field(
        default=200, alias="PER_COMBO_MODEL_MIN_MARKETS"
    )
    min_markets_for_stats: int = Field(default=30, alias="MIN_MARKETS_FOR_STATS")

    # Conservative defaults are only used until learned thresholds have enough
    # prequential, resolved forecasts. They are displayed with source=DEFAULT.
    default_threshold_5m: float = Field(default=0.62, alias="DEFAULT_THRESHOLD_5M")
    default_threshold_15m: float = Field(default=0.60, alias="DEFAULT_THRESHOLD_15M")
    default_threshold_1h: float = Field(default=0.58, alias="DEFAULT_THRESHOLD_1H")
    threshold_min_samples: int = Field(default=60, alias="THRESHOLD_MIN_SAMPLES")
    threshold_min_covered: int = Field(default=24, alias="THRESHOLD_MIN_COVERED")
    threshold_target_accuracy: float = Field(
        default=0.56, alias="THRESHOLD_TARGET_ACCURACY"
    )
    calibration_min_samples: int = Field(
        default=50, alias="CALIBRATION_MIN_SAMPLES"
    )
    calibration_min_bin_samples: int = Field(
        default=12, alias="CALIBRATION_MIN_BIN_SAMPLES"
    )
    calibration_prior_strength: float = Field(
        default=20.0, alias="CALIBRATION_PRIOR_STRENGTH"
    )

    predictability_min_5m: float = Field(
        default=0.60, alias="PREDICTABILITY_MIN_5M"
    )
    predictability_min_15m: float = Field(
        default=0.58, alias="PREDICTABILITY_MIN_15M"
    )
    predictability_min_1h: float = Field(
        default=0.56, alias="PREDICTABILITY_MIN_1H"
    )

    def default_probability_threshold(self, horizon: str) -> float:
        return {
            "5m": self.default_threshold_5m,
            "15m": self.default_threshold_15m,
            "1h": self.default_threshold_1h,
        }.get(horizon, 0.60)

    def min_predictability(self, horizon: str) -> float:
        return {
            "5m": self.predictability_min_5m,
            "15m": self.predictability_min_15m,
            "1h": self.predictability_min_1h,
        }.get(horizon, 0.58)

    # ----- Chainlink official reference -----
    chainlink_enabled: bool = Field(default=True, alias="CHAINLINK_ENABLED")
    rtds_ws_url: str = Field(
        default="wss://ws-live-data.polymarket.com", alias="RTDS_WS_URL"
    )
    rtds_ping_sec: float = Field(default=5.0, alias="RTDS_PING_SEC")
    rtds_debug_raw: bool = Field(default=False, alias="RTDS_DEBUG_RAW")
    max_reference_open_alignment_ms: float = Field(
        default=5000.0, alias="MAX_REFERENCE_OPEN_ALIGNMENT_MS"
    )
    max_reference_close_alignment_ms: float = Field(
        default=8000.0, alias="MAX_REFERENCE_CLOSE_ALIGNMENT_MS"
    )
    max_reference_source_age_ms: float = Field(
        default=8000.0, alias="MAX_REFERENCE_SOURCE_AGE_MS"
    )

    # ----- Web -----
    web_enabled: bool = Field(default=True, alias="WEB_ENABLED")
    web_host: str = Field(default="0.0.0.0", alias="WEB_HOST")
    web_port: int = Field(default=8091, alias="WEB_PORT")

    # ----- Scope -----
    assets_csv: str = Field(default="BTC,ETH,SOL,XRP", alias="ASSETS")
    horizons_csv: str = Field(default="5m,15m,1h", alias="HORIZONS")

    # ----- Binance -----
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
    feature_price_ring_max: int = Field(
        default=24000, alias="FEATURE_PRICE_RING_MAX"
    )

    # ----- Polymarket discovery/resolution -----
    gamma_host: str = Field(
        default="https://gamma-api.polymarket.com", alias="GAMMA_HOST"
    )
    gamma_poll_sec: int = Field(default=10, alias="GAMMA_POLL_SEC")
    gamma_event_limit: int = Field(default=200, alias="GAMMA_EVENT_LIMIT")
    resolution_poll_sec: int = Field(default=30, alias="RESOLUTION_POLL_SEC")

    # ----- Polymarket CLOB -----
    clob_ws_url: str = Field(
        default="wss://ws-subscriptions-clob.polymarket.com/ws/market",
        alias="CLOB_WS_URL",
    )
    clob_host: str = Field(default="https://clob.polymarket.com", alias="CLOB_HOST")

    # ----- Reference/PTB -----
    poly_price_host: str = Field(
        default="https://polymarket.com", alias="POLY_PRICE_HOST"
    )
    chainlink_ref_enabled: bool = Field(default=True, alias="CHAINLINK_REF_ENABLED")

    # ----- Recorder -----
    snapshot_checkpoints_csv: str = Field(
        default="240,180,120,90,60,45,30,20,10,5",
        alias="SNAPSHOT_CHECKPOINTS",
    )
    snapshot_loop_ms: int = Field(default=500, alias="SNAPSHOT_LOOP_MS")
    backfill_resolved_markets: int = Field(
        default=0, alias="BACKFILL_RESOLVED_MARKETS"
    )

    checkpoints_5m_csv: str = Field(
        default="240,180,150,120,90,60,45,30,20,10",
        alias="CHECKPOINTS_5M",
    )
    checkpoints_15m_csv: str = Field(
        default="840,720,600,480,360,240,120,60,30",
        alias="CHECKPOINTS_15M",
    )
    checkpoints_1h_csv: str = Field(
        default="3300,3000,2400,1800,1200,900,600,300,120,60",
        alias="CHECKPOINTS_1H",
    )

    # ----- Freshness -----
    max_spot_age_ms: float = Field(default=2500, alias="MAX_SPOT_AGE_MS")
    max_book_age_ms: float = Field(default=3000, alias="MAX_BOOK_AGE_MS")
    max_transport_age_ms: float = Field(
        default=5000, alias="MAX_TRANSPORT_AGE_MS"
    )
    max_source_age_ms: float = Field(default=5000, alias="MAX_SOURCE_AGE_MS")
    max_clob_age_ms: float = Field(default=6000, alias="MAX_CLOB_AGE_MS")
    max_reference_age_ms: float = Field(
        default=8000, alias="MAX_REFERENCE_AGE_MS"
    )
    max_clock_skew_ms: float = Field(default=3000, alias="MAX_CLOCK_SKEW_MS")

    # ----- Reconnect -----
    backoff_base_sec: float = Field(default=1.0, alias="BACKOFF_BASE_SEC")
    backoff_factor: float = Field(default=2.0, alias="BACKOFF_FACTOR")
    backoff_cap_sec: float = Field(default=30.0, alias="BACKOFF_CAP_SEC")
    ws_recv_timeout_sec: float = Field(
        default=30.0, alias="WS_RECV_TIMEOUT_SEC"
    )

    def assets(self) -> list[str]:
        return [a.strip().upper() for a in self.assets_csv.split(",") if a.strip()]

    def horizons(self) -> list[str]:
        return [h.strip().lower() for h in self.horizons_csv.split(",") if h.strip()]

    def snapshot_checkpoints(self) -> list[int]:
        out = [
            int(t.strip())
            for t in self.snapshot_checkpoints_csv.split(",")
            if t.strip().isdigit()
        ]
        return sorted(set(out), reverse=True)

    def checkpoints_for(self, horizon: str) -> list[int]:
        csv_value = {
            "5m": self.checkpoints_5m_csv,
            "15m": self.checkpoints_15m_csv,
            "1h": self.checkpoints_1h_csv,
        }.get(horizon, self.snapshot_checkpoints_csv)
        out = [int(t.strip()) for t in csv_value.split(",") if t.strip().isdigit()]
        return sorted(set(out), reverse=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
