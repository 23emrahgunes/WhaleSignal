"""Configuration for P3 structural arbitrage DRY research and guarded LIVE mode.

DRY remains the default. LIVE is a separately armed capability with conservative
caps, localhost-only control and explicit environment gates. Secret key material is
never modeled as a Pydantic setting; live adapters read it lazily from the process
environment only when a connection/preflight or execution action is requested.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class P3Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env.p3", ".env.p26", ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_ignore_empty=True,
        populate_by_name=True,
    )

    p26_db_path: str = Field(default="data/p26_research.sqlite", alias="P3_P26_DB_PATH")
    p3_db_path: str = Field(default="data/p3_arbitrage.sqlite", alias="P3_DB_PATH")
    reports_dir: str = Field(default="reports/p3", alias="P3_REPORTS_DIR")

    scanner_enabled: bool = Field(default=True, alias="P3_SCANNER_ENABLED")
    scan_interval_ms: int = Field(default=250, alias="P3_SCAN_INTERVAL_MS")
    max_book_age_ms: int = Field(default=750, alias="P3_MAX_BOOK_AGE_MS")
    max_source_skew_ms: int = Field(default=500, alias="P3_MAX_SOURCE_SKEW_MS")
    min_net_profit_usdc: float = Field(default=0.0, alias="P3_MIN_NET_PROFIT_USDC")
    min_net_roi: float = Field(default=0.0, alias="P3_MIN_NET_ROI")
    execution_buffer_per_share: float = Field(
        default=0.0, alias="P3_EXECUTION_BUFFER_PER_SHARE"
    )
    max_quantity_shares: float = Field(default=500.0, alias="P3_MAX_QUANTITY_SHARES")
    max_capital_per_cycle_usdc: float = Field(
        default=20.0, alias="P3_MAX_CAPITAL_PER_CYCLE_USDC"
    )
    window_grace_ms: int = Field(default=500, alias="P3_WINDOW_GRACE_MS")

    replay_delays_ms: str = Field(
        default="10,25,50,100,200,500", alias="P3_REPLAY_DELAYS_MS"
    )
    replay_snapshot_tolerance_ms: int = Field(
        default=250, alias="P3_REPLAY_SNAPSHOT_TOLERANCE_MS"
    )
    replay_batch_size: int = Field(default=200, alias="P3_REPLAY_BATCH_SIZE")
    replay_runtime_batch_size: int = Field(
        default=20, alias="P3_REPLAY_RUNTIME_BATCH_SIZE"
    )

    # STRICT DRY policy: one independent simulated attempt per opportunity window.
    dry_enabled: bool = Field(default=True, alias="P3_DRY_ENABLED")
    dry_latency_ms: int = Field(default=100, alias="P3_DRY_LATENCY_MS")
    dry_entry_confirm_ms: int = Field(default=250, alias="P3_DRY_ENTRY_CONFIRM_MS")
    dry_confirm_max_gap_ms: int = Field(default=400, alias="P3_DRY_CONFIRM_MAX_GAP_MS")
    dry_survival_delays_ms: str = Field(
        default="0,50,100,200,250,500", alias="P3_DRY_SURVIVAL_DELAYS_MS"
    )
    dry_start_bankroll_usdc: float = Field(default=100.0, alias="P3_DRY_START_BANKROLL_USDC")
    dry_min_net_profit_usdc: float = Field(default=0.01, alias="P3_DRY_MIN_NET_PROFIT_USDC")
    dry_min_net_roi: float = Field(default=0.0025, alias="P3_DRY_MIN_NET_ROI")

    # Research promotion gates. These are also the default LIVE arming gates.
    readiness_min_windows: int = Field(default=100, alias="P3_READINESS_MIN_WINDOWS")
    readiness_min_pair_completion: float = Field(
        default=0.97, alias="P3_READINESS_MIN_PAIR_COMPLETION"
    )
    readiness_min_pair_wilson_lower: float = Field(
        default=0.90, alias="P3_READINESS_MIN_PAIR_WILSON_LOWER"
    )
    readiness_max_one_leg_rate: float = Field(
        default=0.03, alias="P3_READINESS_MAX_ONE_LEG_RATE"
    )
    readiness_max_drawdown_usdc: float = Field(
        default=5.0, alias="P3_READINESS_MAX_DRAWDOWN_USDC"
    )

    # Guarded LIVE capability. Everything is disabled by default.
    live_feature_enabled: bool = Field(default=False, alias="P3_LIVE_FEATURE_ENABLED")
    live_auto_execute_enabled: bool = Field(
        default=False, alias="P3_LIVE_AUTO_EXECUTE_ENABLED"
    )
    live_require_dry_validated: bool = Field(
        default=True, alias="P3_LIVE_REQUIRE_DRY_VALIDATED"
    )
    live_buy_merge_only: bool = Field(default=True, alias="P3_LIVE_BUY_MERGE_ONLY")
    live_max_capital_per_cycle_usdc: float = Field(
        default=1.0, alias="P3_LIVE_MAX_CAPITAL_PER_CYCLE_USDC"
    )
    live_max_quantity_shares: float = Field(
        default=10.0, alias="P3_LIVE_MAX_QUANTITY_SHARES"
    )
    live_min_net_profit_usdc: float = Field(
        default=0.01, alias="P3_LIVE_MIN_NET_PROFIT_USDC"
    )
    live_min_net_roi: float = Field(default=0.0025, alias="P3_LIVE_MIN_NET_ROI")
    live_poll_interval_ms: int = Field(default=100, alias="P3_LIVE_POLL_INTERVAL_MS")
    live_settlement_wait_sec: float = Field(
        default=15.0, alias="P3_LIVE_SETTLEMENT_WAIT_SEC"
    )
    live_settlement_poll_sec: float = Field(
        default=0.5, alias="P3_LIVE_SETTLEMENT_POLL_SEC"
    )
    live_clob_host: str = Field(
        default="https://clob.polymarket.com", alias="P3_LIVE_CLOB_HOST"
    )
    live_chain_id: int = Field(default=137, alias="P3_LIVE_CHAIN_ID")
    live_geoblock_url: str = Field(
        default="https://polymarket.com/api/geoblock", alias="P3_LIVE_GEOBLOCK_URL"
    )
    live_require_geoblock_clear: bool = Field(
        default=True, alias="P3_LIVE_REQUIRE_GEOBLOCK_CLEAR"
    )
    live_control_enabled: bool = Field(default=True, alias="P3_LIVE_CONTROL_ENABLED")
    live_control_host: str = Field(default="127.0.0.1", alias="P3_LIVE_CONTROL_HOST")
    live_control_port: int = Field(default=8094, alias="P3_LIVE_CONTROL_PORT")

    # Read-only public/main analytics dashboard.
    web_enabled: bool = Field(default=True, alias="P3_WEB_ENABLED")
    web_host: str = Field(default="127.0.0.1", alias="P3_WEB_HOST")
    web_port: int = Field(default=8093, alias="P3_WEB_PORT")
    web_refresh_ms: int = Field(default=3000, alias="P3_WEB_REFRESH_MS")

    @staticmethod
    def _parse_nonnegative_ms(raw: str, *, name: str) -> tuple[int, ...]:
        values: list[int] = []
        for part in str(raw).split(","):
            part = part.strip()
            if not part:
                continue
            value = int(part)
            if value < 0:
                raise ValueError(f"{name} cannot contain negative values")
            values.append(value)
        if not values:
            raise ValueError(f"{name} must contain at least one value")
        return tuple(sorted(set(values)))

    def replay_delays(self) -> tuple[int, ...]:
        return self._parse_nonnegative_ms(self.replay_delays_ms, name="replay delays")

    def dry_survival_delays(self) -> tuple[int, ...]:
        return self._parse_nonnegative_ms(
            self.dry_survival_delays_ms,
            name="dry survival delays",
        )

    def validate_research_safety(self) -> None:
        if Path(self.p26_db_path).resolve() == Path(self.p3_db_path).resolve():
            raise ValueError("P3_DB_PATH must be separate from P2.6 database")
        if self.scan_interval_ms < 20:
            raise ValueError("P3 scan interval below 20ms is not supported")
        if self.max_book_age_ms < 0 or self.max_source_skew_ms < 0:
            raise ValueError("book age/skew limits cannot be negative")
        if self.min_net_profit_usdc < 0 or self.min_net_roi < 0:
            raise ValueError("minimum profit/ROI cannot be negative")
        if self.execution_buffer_per_share < 0:
            raise ValueError("execution buffer cannot be negative")
        if self.max_quantity_shares <= 0 or self.max_capital_per_cycle_usdc <= 0:
            raise ValueError("quantity and capital limits must be positive")
        if self.replay_batch_size < 1 or self.replay_runtime_batch_size < 1:
            raise ValueError("replay batch sizes must be positive")
        if self.dry_latency_ms not in self.replay_delays():
            raise ValueError("P3_DRY_LATENCY_MS must exist in P3_REPLAY_DELAYS_MS")
        if self.dry_entry_confirm_ms < 0:
            raise ValueError("P3_DRY_ENTRY_CONFIRM_MS cannot be negative")
        if self.dry_confirm_max_gap_ms < self.scan_interval_ms:
            raise ValueError("P3_DRY_CONFIRM_MAX_GAP_MS must be >= P3_SCAN_INTERVAL_MS")
        self.dry_survival_delays()
        if self.dry_start_bankroll_usdc <= 0:
            raise ValueError("dry bankroll must be positive")
        if self.dry_min_net_profit_usdc < 0 or self.dry_min_net_roi < 0:
            raise ValueError("dry thresholds cannot be negative")
        if self.readiness_min_windows < 1:
            raise ValueError("readiness_min_windows must be positive")
        for value in (
            self.readiness_min_pair_completion,
            self.readiness_min_pair_wilson_lower,
            self.readiness_max_one_leg_rate,
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError("readiness rates must be in [0,1]")
        if self.readiness_max_drawdown_usdc < 0:
            raise ValueError("readiness drawdown cannot be negative")
        if not 1 <= self.web_port <= 65535:
            raise ValueError("invalid web port")

        # LIVE safety contract. These checks are structural and run even while LIVE
        # is disabled, so a later env change cannot silently create an unsafe config.
        if self.live_max_capital_per_cycle_usdc <= 0 or self.live_max_quantity_shares <= 0:
            raise ValueError("LIVE quantity/capital limits must be positive")
        if self.live_max_capital_per_cycle_usdc > self.max_capital_per_cycle_usdc:
            raise ValueError("LIVE capital cap cannot exceed P3 research capital cap")
        if self.live_max_quantity_shares > self.max_quantity_shares:
            raise ValueError("LIVE quantity cap cannot exceed P3 research quantity cap")
        if self.live_min_net_profit_usdc < 0 or self.live_min_net_roi < 0:
            raise ValueError("LIVE edge thresholds cannot be negative")
        if self.live_poll_interval_ms < 20:
            raise ValueError("P3_LIVE_POLL_INTERVAL_MS below 20ms is not supported")
        if self.live_settlement_wait_sec <= 0 or self.live_settlement_poll_sec <= 0:
            raise ValueError("LIVE settlement timings must be positive")
        if self.live_chain_id != 137:
            raise ValueError("P3 LIVE v1 supports Polygon mainnet chain_id=137 only")
        if self.live_control_host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("P3 LIVE control panel must bind to loopback only")
        if not 1 <= self.live_control_port <= 65535:
            raise ValueError("invalid LIVE control port")
        if self.live_control_port == self.web_port and self.live_control_host == self.web_host:
            raise ValueError("LIVE control port must be separate from read-only dashboard")
        if not self.live_buy_merge_only:
            raise ValueError("P3 LIVE v1 only supports BUY+MERGE; SPLIT+SELL stays disabled")
        if self.live_auto_execute_enabled and not self.live_feature_enabled:
            raise ValueError("LIVE auto execution cannot be enabled while LIVE feature is disabled")

    def ensure_directories(self) -> None:
        Path(self.p3_db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.reports_dir).mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_p3_settings() -> P3Settings:
    settings = P3Settings()
    settings.validate_research_safety()
    return settings
