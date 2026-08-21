"""Configuration for the isolated P2.6 research stack.

P2.6 is deliberately separate from the production-like P2.5 SHADOW/PAPER
runtime.  It reads P2.5 data, writes its own research database and never loads
credentials, signs payloads or submits orders.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class P26Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env.p26", ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_ignore_empty=True,
        populate_by_name=True,
    )

    # Isolation / storage
    p25_db_path: str = Field(
        default="data/direction_engine.sqlite", alias="P26_P25_DB_PATH"
    )
    p26_db_path: str = Field(
        default="data/p26_research.sqlite", alias="P26_DB_PATH"
    )
    backup_root: str = Field(
        default="data/backups", alias="P26_BACKUP_ROOT"
    )
    model_dir: str = Field(default="models/p26", alias="P26_MODEL_DIR")
    reports_dir: str = Field(default="reports/p26", alias="P26_REPORTS_DIR")

    # Baseline freeze
    p25_model_path: str = Field(
        default="models/direction_model.pkl", alias="P26_P25_MODEL_PATH"
    )
    p25_calibration_path: str = Field(
        default="models/calibration_book.pkl", alias="P26_P25_CALIBRATION_PATH"
    )
    p25_state_url: str = Field(
        default="http://127.0.0.1:8091/api/state", alias="P26_P25_STATE_URL"
    )
    p25_paper_summary_url: str = Field(
        default="http://127.0.0.1:8091/api/paper-summary",
        alias="P26_P25_PAPER_SUMMARY_URL",
    )
    baseline_require_clean_git: bool = Field(
        default=True, alias="P26_BASELINE_REQUIRE_CLEAN_GIT"
    )
    baseline_http_timeout_sec: float = Field(
        default=8.0, alias="P26_BASELINE_HTTP_TIMEOUT_SEC"
    )

    # Oracle sidecar
    rtds_ws_url: str = Field(
        default="wss://ws-live-data.polymarket.com", alias="P26_RTDS_WS_URL"
    )
    rtds_ping_sec: float = Field(default=5.0, alias="P26_RTDS_PING_SEC")
    rtds_recv_timeout_sec: float = Field(
        default=30.0, alias="P26_RTDS_RECV_TIMEOUT_SEC"
    )
    oracle_queue_max: int = Field(default=20_000, alias="P26_ORACLE_QUEUE_MAX")
    oracle_batch_size: int = Field(default=200, alias="P26_ORACLE_BATCH_SIZE")
    oracle_flush_interval_ms: int = Field(
        default=250, alias="P26_ORACLE_FLUSH_INTERVAL_MS"
    )
    oracle_retention_days: int = Field(
        default=30, alias="P26_ORACLE_RETENTION_DAYS"
    )
    oracle_prune_batch: int = Field(
        default=10_000, alias="P26_ORACLE_PRUNE_BATCH"
    )
    oracle_rehydrate_minutes: int = Field(
        default=90, alias="P26_ORACLE_REHYDRATE_MINUTES"
    )

    # Canonical dataset
    canonical_max_lag_ms: int = Field(
        default=2_000, alias="P26_CANONICAL_MAX_LAG_MS"
    )
    canonical_checkpoints_5m: int = Field(
        default=60, alias="P26_CANONICAL_CHECKPOINT_5M"
    )
    canonical_checkpoints_15m: int = Field(
        default=240, alias="P26_CANONICAL_CHECKPOINT_15M"
    )
    canonical_checkpoints_1h: int = Field(
        default=600, alias="P26_CANONICAL_CHECKPOINT_1H"
    )
    extraction_policy_version: str = Field(
        default="P26_CANONICAL_V1", alias="P26_EXTRACTION_POLICY_VERSION"
    )
    feature_schema_version: str = Field(
        default="P26_EXTERNAL_FEATURES_V1", alias="P26_FEATURE_SCHEMA_VERSION"
    )
    dataset_label_sync_interval_sec: int = Field(
        default=60, alias="P26_DATASET_LABEL_SYNC_INTERVAL_SEC"
    )
    dataset_max_snapshot_batch: int = Field(
        default=5_000, alias="P26_DATASET_MAX_SNAPSHOT_BATCH"
    )

    # Frozen independent fair-value model. These are pre-registered initial
    # research policies, not claimed optimal thresholds.
    model_artifact_version: str = Field(
        default="P26_FAIR_VALUE_V1", alias="P26_MODEL_ARTIFACT_VERSION"
    )
    model_min_train_markets: int = Field(
        default=150, alias="P26_MODEL_MIN_TRAIN_MARKETS"
    )
    model_min_class_markets: int = Field(
        default=50, alias="P26_MODEL_MIN_CLASS_MARKETS"
    )
    model_regularization_c: float = Field(
        default=1.0, alias="P26_MODEL_REGULARIZATION_C"
    )
    model_random_seed: int = Field(default=26, alias="P26_MODEL_RANDOM_SEED")

    # Purged nested walk-forward
    walkforward_embargo_ms: int = Field(
        default=3_600_000, alias="P26_WALKFORWARD_EMBARGO_MS"
    )
    walkforward_outer_test_fraction: float = Field(
        default=0.15, alias="P26_WALKFORWARD_OUTER_TEST_FRACTION"
    )
    walkforward_inner_validation_fraction: float = Field(
        default=0.20, alias="P26_WALKFORWARD_INNER_VALIDATION_FRACTION"
    )

    # Calibration / uncertainty
    calibration_bucket_width: float = Field(
        default=0.05, alias="P26_CALIBRATION_BUCKET_WIDTH"
    )
    calibration_min_bucket_n: int = Field(
        default=30, alias="P26_CALIBRATION_MIN_BUCKET_N"
    )
    calibration_z: float = Field(default=1.96, alias="P26_CALIBRATION_Z")

    # Time alignment / latency
    max_source_skew_ms: int = Field(
        default=1_000, alias="P26_MAX_SOURCE_SKEW_MS"
    )
    max_decision_data_lag_ms: int = Field(
        default=1_000, alias="P26_MAX_DECISION_DATA_LAG_MS"
    )
    max_quote_age_at_fill_ms: int = Field(
        default=500, alias="P26_MAX_QUOTE_AGE_AT_FILL_MS"
    )
    max_forecast_age_ms: int = Field(
        default=2_000, alias="P26_MAX_FORECAST_AGE_MS"
    )

    # Public read-only Polymarket CLOB V2 market/book data.
    clob_http_url: str = Field(
        default="https://clob.polymarket.com", alias="P26_CLOB_HTTP_URL"
    )
    clob_ws_url: str = Field(
        default="wss://ws-subscriptions-clob.polymarket.com/ws/market",
        alias="P26_CLOB_WS_URL",
    )
    book_market_refresh_sec: int = Field(
        default=30, alias="P26_BOOK_MARKET_REFRESH_SEC"
    )
    book_persist_min_interval_ms: int = Field(
        default=100, alias="P26_BOOK_PERSIST_MIN_INTERVAL_MS"
    )
    book_history_retention_hours: int = Field(
        default=72, alias="P26_BOOK_HISTORY_RETENTION_HOURS"
    )

    # Paper V2 / execution. Hypotheses are intentionally configurable and must be
    # selected on validation data before untouched test evaluation.
    paper_v2_strategy_version: str = Field(
        default="RESEARCH_PAPER_V2", alias="P26_PAPER_V2_STRATEGY_VERSION"
    )
    paper_v2_stake_usdc: float = Field(
        default=2.50, alias="P26_PAPER_V2_STAKE_USDC"
    )
    paper_v2_min_net_edge: float = Field(
        default=0.02, alias="P26_PAPER_V2_MIN_NET_EDGE"
    )
    paper_v2_safety_buffer: float = Field(
        default=0.005, alias="P26_PAPER_V2_SAFETY_BUFFER"
    )
    paper_v2_fee_bps: float = Field(
        default=0.0, alias="P26_PAPER_V2_FEE_BPS"
    )
    paper_v2_min_fill_fraction: float = Field(
        default=1.0, alias="P26_PAPER_V2_MIN_FILL_FRACTION"
    )
    paper_v2_max_spread: float = Field(
        default=0.15, alias="P26_PAPER_V2_MAX_SPREAD"
    )
    paper_v2_min_depth_persistence_ms: int = Field(
        default=250, alias="P26_PAPER_V2_MIN_DEPTH_PERSISTENCE_MS"
    )
    paper_v2_max_flicker_rate: float = Field(
        default=0.85, alias="P26_PAPER_V2_MAX_FLICKER_RATE"
    )
    paper_v2_max_cancel_to_add_ratio: float = Field(
        default=8.0, alias="P26_PAPER_V2_MAX_CANCEL_TO_ADD_RATIO"
    )
    paper_v2_enabled: bool = Field(
        default=False, alias="P26_PAPER_V2_ENABLED"
    )
    paper_v2_model_manifest: str = Field(
        default="models/p26/fair_value_v1.manifest.json",
        alias="P26_PAPER_V2_MODEL_MANIFEST",
    )
    paper_v2_alpha_artifact: str = Field(
        default="models/p26/alpha_profile_v1.json",
        alias="P26_PAPER_V2_ALPHA_ARTIFACT",
    )
    paper_v2_alpha_min_samples: int = Field(
        default=30, alias="P26_PAPER_V2_ALPHA_MIN_SAMPLES"
    )
    paper_v2_alpha_ttl_quantile: float = Field(
        default=0.20, alias="P26_PAPER_V2_ALPHA_TTL_QUANTILE"
    )
    paper_v2_fill_delay_ms: int = Field(
        default=100, alias="P26_PAPER_V2_FILL_DELAY_MS"
    )
    paper_v2_approved_calibration_scopes: str = Field(
        default="PER_COMBO", alias="P26_PAPER_V2_APPROVED_CALIBRATION_SCOPES"
    )
    paper_v2_approved_alpha_scopes: str = Field(
        default="PER_COMBO", alias="P26_PAPER_V2_APPROVED_ALPHA_SCOPES"
    )
    paper_v2_initial_equity_usdc: float = Field(
        default=1_000.0, alias="P26_PAPER_V2_INITIAL_EQUITY_USDC"
    )
    paper_v2_max_open_positions_total: int = Field(
        default=4, alias="P26_PAPER_V2_MAX_OPEN_POSITIONS_TOTAL"
    )
    paper_v2_max_open_exposure_usdc: float = Field(
        default=10.0, alias="P26_PAPER_V2_MAX_OPEN_EXPOSURE_USDC"
    )
    paper_v2_max_exposure_per_asset_usdc: float = Field(
        default=3.0, alias="P26_PAPER_V2_MAX_EXPOSURE_PER_ASSET_USDC"
    )
    paper_v2_max_exposure_per_horizon_usdc: float = Field(
        default=5.0, alias="P26_PAPER_V2_MAX_EXPOSURE_PER_HORIZON_USDC"
    )
    paper_v2_max_overlapping_positions_per_asset: int = Field(
        default=1, alias="P26_PAPER_V2_MAX_OVERLAPPING_POSITIONS_PER_ASSET"
    )
    paper_v2_max_crypto_cluster_exposure_usdc: float = Field(
        default=10.0, alias="P26_PAPER_V2_MAX_CRYPTO_CLUSTER_EXPOSURE_USDC"
    )
    paper_v2_daily_loss_limit_usdc: float = Field(
        default=25.0, alias="P26_PAPER_V2_DAILY_LOSS_LIMIT_USDC"
    )
    paper_v2_max_drawdown_fraction: float = Field(
        default=0.10, alias="P26_PAPER_V2_MAX_DRAWDOWN_FRACTION"
    )
    paper_v2_consecutive_loss_limit: int = Field(
        default=3, alias="P26_PAPER_V2_CONSECUTIVE_LOSS_LIMIT"
    )
    paper_v2_cooldown_sec: int = Field(
        default=900, alias="P26_PAPER_V2_COOLDOWN_SEC"
    )
    paper_v2_global_kill_switch: bool = Field(
        default=False, alias="P26_PAPER_V2_GLOBAL_KILL_SWITCH"
    )

    # Promotion (paper-only). These are pre-registered initial policies.
    promotion_min_oos_markets: int = Field(
        default=300, alias="P26_PROMOTION_MIN_OOS_MARKETS"
    )
    promotion_min_oos_class_markets: int = Field(
        default=75, alias="P26_PROMOTION_MIN_OOS_CLASS_MARKETS"
    )
    promotion_bootstrap_blocks: int = Field(
        default=2_000, alias="P26_PROMOTION_BOOTSTRAP_BLOCKS"
    )
    promotion_block_hours: int = Field(
        default=6, alias="P26_PROMOTION_BLOCK_HOURS"
    )
    promotion_min_paper_trades: int = Field(
        default=100, alias="P26_PROMOTION_MIN_PAPER_TRADES"
    )
    promotion_min_positive_fold_fraction: float = Field(
        default=0.60, alias="P26_PROMOTION_MIN_POSITIVE_FOLD_FRACTION"
    )
    promotion_max_asset_concentration: float = Field(
        default=0.80, alias="P26_PROMOTION_MAX_ASSET_CONCENTRATION"
    )
    promotion_max_horizon_concentration: float = Field(
        default=0.80, alias="P26_PROMOTION_MAX_HORIZON_CONCENTRATION"
    )
    promotion_max_drawdown_fraction: float = Field(
        default=0.25, alias="P26_PROMOTION_MAX_DRAWDOWN_FRACTION"
    )
    promotion_random_seed: int = Field(
        default=2606, alias="P26_PROMOTION_RANDOM_SEED"
    )

    def canonical_checkpoint(self, horizon: str) -> int:
        return {
            "5m": self.canonical_checkpoints_5m,
            "15m": self.canonical_checkpoints_15m,
            "1h": self.canonical_checkpoints_1h,
        }[horizon]

    def ensure_directories(self) -> None:
        for raw in (self.backup_root, self.model_dir, self.reports_dir):
            Path(raw).mkdir(parents=True, exist_ok=True)
        Path(self.p26_db_path).parent.mkdir(parents=True, exist_ok=True)

    def validate_research_safety(self) -> None:
        if self.p25_db_path == self.p26_db_path:
            raise ValueError("P26_DB_PATH must be separate from the P2.5 database")
        if self.oracle_queue_max < self.oracle_batch_size:
            raise ValueError("oracle queue must be >= batch size")
        if self.canonical_max_lag_ms < 0:
            raise ValueError("canonical lag cannot be negative")
        if self.dataset_label_sync_interval_sec < 1:
            raise ValueError("dataset label sync interval must be positive")
        if self.dataset_max_snapshot_batch < 1:
            raise ValueError("dataset snapshot batch must be positive")
        if not 0.0 < self.calibration_bucket_width <= 0.25:
            raise ValueError("calibration bucket width must be in (0, 0.25]")
        if not 0.0 <= self.paper_v2_fee_bps:
            raise ValueError("paper fee cannot be negative")
        if not 0.0 < self.paper_v2_min_fill_fraction <= 1.0:
            raise ValueError("paper fill fraction must be in (0,1]")
        if self.paper_v2_initial_equity_usdc <= 0:
            raise ValueError("paper V2 initial equity must be positive")
        if self.book_persist_min_interval_ms < 0:
            raise ValueError("book persist interval cannot be negative")
        if self.paper_v2_alpha_min_samples < 1:
            raise ValueError("alpha minimum samples must be positive")
        if not 0 < self.paper_v2_alpha_ttl_quantile <= 1:
            raise ValueError("alpha TTL quantile must be in (0,1]")

    def approved_calibration_scopes(self) -> tuple[str, ...]:
        return tuple(
            item.strip().upper()
            for item in self.paper_v2_approved_calibration_scopes.split(",")
            if item.strip()
        )

    def approved_alpha_scopes(self) -> tuple[str, ...]:
        return tuple(
            item.strip().upper()
            for item in self.paper_v2_approved_alpha_scopes.split(",")
            if item.strip()
        )


@lru_cache(maxsize=1)
def get_p26_settings() -> P26Settings:
    settings = P26Settings()
    settings.validate_research_safety()
    return settings
