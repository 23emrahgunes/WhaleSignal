"""Configuration for P3 structural and DUAL40 maker-recovery strategies.

Every process starts DRY. LIVE is separately armed through the authenticated 8093
operator surface. ``P3_STRATEGY_MODE`` selects either the existing immediate
BUY+MERGE engine or the isolated post-only DUAL40 maker-recovery cohort.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


STRUCTURAL_MODE = "STRUCTURAL_BUY_MERGE_V3"
DUAL40_MODE = "DUAL40_MAKER_RECOVERY_V1"


class P3Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env.p3", ".env.p26", ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_ignore_empty=True,
        populate_by_name=True,
    )

    strategy_mode: str = Field(default=STRUCTURAL_MODE, alias="P3_STRATEGY_MODE")
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

    # STRICT DRY policy for the existing immediate complete-set engine.
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

    # -------------------------------------------------------------------------
    # DUAL40 maker-recovery cohort. One global market at a time.
    # -------------------------------------------------------------------------
    dual40_paper_enabled: bool = Field(default=True, alias="P3_DUAL40_PAPER_ENABLED")
    dual40_assets_csv: str = Field(default="BTC,ETH,SOL,XRP", alias="P3_DUAL40_ASSETS")
    dual40_horizon: str = Field(default="5m", alias="P3_DUAL40_HORIZON")
    dual40_price: float = Field(default=0.40, alias="P3_DUAL40_PRICE")
    dual40_ladder_csv: str = Field(default="5,10,30", alias="P3_DUAL40_LADDER")
    dual40_market_age_sec: float = Field(default=30.0, alias="P3_DUAL40_MARKET_AGE_SEC")
    dual40_min_tte_sec: float = Field(default=90.0, alias="P3_DUAL40_MIN_TTE_SEC")
    dual40_cancel_tte_sec: float = Field(default=40.0, alias="P3_DUAL40_CANCEL_TTE_SEC")
    dual40_lookback_sec: float = Field(default=20.0, alias="P3_DUAL40_LOOKBACK_SEC")
    dual40_confirm_sec: float = Field(default=5.0, alias="P3_DUAL40_CONFIRM_SEC")
    dual40_balanced_mid_low: float = Field(default=0.44, alias="P3_DUAL40_BALANCED_MID_LOW")
    dual40_balanced_mid_high: float = Field(default=0.56, alias="P3_DUAL40_BALANCED_MID_HIGH")
    dual40_max_mid_range: float = Field(default=0.10, alias="P3_DUAL40_MAX_MID_RANGE")
    dual40_max_net_drift: float = Field(default=0.04, alias="P3_DUAL40_MAX_NET_DRIFT")
    dual40_max_abs_slope_per_sec: float = Field(
        default=0.0030, alias="P3_DUAL40_MAX_ABS_SLOPE_PER_SEC"
    )
    dual40_max_one_way_ratio: float = Field(
        default=0.72, alias="P3_DUAL40_MAX_ONE_WAY_RATIO"
    )
    dual40_max_single_jump: float = Field(default=0.06, alias="P3_DUAL40_MAX_SINGLE_JUMP")
    dual40_max_complement_residual: float = Field(
        default=0.04, alias="P3_DUAL40_MAX_COMPLEMENT_RESIDUAL"
    )
    dual40_max_spread_each: float = Field(default=0.10, alias="P3_DUAL40_MAX_SPREAD_EACH")
    dual40_near_touch_price: float = Field(default=0.41, alias="P3_DUAL40_NEAR_TOUCH_PRICE")
    dual40_book_fresh_ms: int = Field(default=1500, alias="P3_DUAL40_BOOK_FRESH_MS")
    dual40_heartbeat_sec: float = Field(default=5.0, alias="P3_DUAL40_HEARTBEAT_SEC")
    dual40_balance_poll_sec: float = Field(default=1.0, alias="P3_DUAL40_BALANCE_POLL_SEC")
    dual40_resolution_poll_sec: float = Field(
        default=10.0, alias="P3_DUAL40_RESOLUTION_POLL_SEC"
    )
    dual40_gamma_host: str = Field(
        default="https://gamma-api.polymarket.com", alias="P3_DUAL40_GAMMA_HOST"
    )
    dual40_min_collateral_to_arm_usdc: float = Field(
        default=35.0, alias="P3_DUAL40_MIN_COLLATERAL_TO_ARM_USDC"
    )
    dual40_fill_epsilon: float = Field(default=0.00001, alias="P3_DUAL40_FILL_EPSILON")

    # -------------------------------------------------------------------------
    # Guarded LIVE. Disabled by default and always starts unarmed.
    # -------------------------------------------------------------------------
    live_feature_enabled: bool = Field(default=False, alias="P3_LIVE_FEATURE_ENABLED")
    live_auto_execute_enabled: bool = Field(
        default=False, alias="P3_LIVE_AUTO_EXECUTE_ENABLED"
    )
    live_require_dry_validated: bool = Field(
        default=True, alias="P3_LIVE_REQUIRE_DRY_VALIDATED"
    )
    live_buy_merge_only: bool = Field(default=True, alias="P3_LIVE_BUY_MERGE_ONLY")
    live_target_quantity_shares: float = Field(
        default=5.0, alias="P3_LIVE_TARGET_QUANTITY_SHARES"
    )
    live_max_quantity_shares: float = Field(
        default=10.0, alias="P3_LIVE_MAX_QUANTITY_SHARES"
    )
    live_max_capital_per_cycle_usdc: float = Field(
        default=0.0, alias="P3_LIVE_MAX_CAPITAL_PER_CYCLE_USDC"
    )
    live_min_net_profit_usdc: float = Field(
        default=0.01, alias="P3_LIVE_MIN_NET_PROFIT_USDC"
    )
    live_min_net_roi: float = Field(default=0.0025, alias="P3_LIVE_MIN_NET_ROI")
    live_min_collateral_to_arm_usdc: float = Field(
        default=5.0, alias="P3_LIVE_MIN_COLLATERAL_TO_ARM_USDC"
    )
    live_max_single_leg_notional_usdc: float = Field(
        default=5.25, alias="P3_LIVE_MAX_SINGLE_LEG_NOTIONAL_USDC"
    )
    live_max_projected_unwind_loss_usdc: float = Field(
        default=0.25, alias="P3_LIVE_MAX_PROJECTED_UNWIND_LOSS_USDC"
    )
    live_emergency_unwind_loss_usdc: float = Field(
        default=0.50, alias="P3_LIVE_EMERGENCY_UNWIND_LOSS_USDC"
    )
    live_min_edge_to_unwind_loss_ratio: float = Field(
        default=0.10, alias="P3_LIVE_MIN_EDGE_TO_UNWIND_LOSS_RATIO"
    )
    live_emergency_fak_enabled: bool = Field(
        default=True, alias="P3_LIVE_EMERGENCY_FAK_ENABLED"
    )
    live_halt_after_one_leg: bool = Field(
        default=True, alias="P3_LIVE_HALT_AFTER_ONE_LEG"
    )
    live_rolling_24h_gross_loss_limit_usdc: float = Field(
        default=2.0, alias="P3_LIVE_ROLLING_24H_GROSS_LOSS_LIMIT_USDC"
    )
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

    live_control_enabled: bool = Field(default=False, alias="P3_LIVE_CONTROL_ENABLED")
    live_control_host: str = Field(default="127.0.0.1", alias="P3_LIVE_CONTROL_HOST")
    live_control_port: int = Field(default=8094, alias="P3_LIVE_CONTROL_PORT")

    web_enabled: bool = Field(default=True, alias="P3_WEB_ENABLED")
    web_host: str = Field(default="127.0.0.1", alias="P3_WEB_HOST")
    web_port: int = Field(default=8093, alias="P3_WEB_PORT")
    web_refresh_ms: int = Field(default=3000, alias="P3_WEB_REFRESH_MS")
    web_auth_required: bool = Field(default=False, alias="P3_WEB_AUTH_REQUIRED")
    web_username: str = Field(default="operator", alias="P3_WEB_USERNAME")
    web_password: SecretStr | None = Field(default=None, alias="P3_WEB_PASSWORD")
    web_session_ttl_sec: int = Field(default=43_200, alias="P3_WEB_SESSION_TTL_SEC")
    web_cookie_secure: bool = Field(default=False, alias="P3_WEB_COOKIE_SECURE")
    web_login_max_failures: int = Field(default=5, alias="P3_WEB_LOGIN_MAX_FAILURES")
    web_login_window_sec: int = Field(default=600, alias="P3_WEB_LOGIN_WINDOW_SEC")

    @property
    def dual40_active(self) -> bool:
        return self.strategy_mode.strip().upper() == DUAL40_MODE

    def dual40_assets(self) -> tuple[str, ...]:
        values = tuple(
            asset.strip().upper()
            for asset in self.dual40_assets_csv.split(",")
            if asset.strip()
        )
        if not values:
            raise ValueError("P3_DUAL40_ASSETS cannot be empty")
        return values

    def dual40_ladder(self) -> tuple[float, ...]:
        values = tuple(float(value.strip()) for value in self.dual40_ladder_csv.split(",") if value.strip())
        if not values:
            raise ValueError("P3_DUAL40_LADDER cannot be empty")
        return values

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

    def web_password_value(self) -> str:
        return self.web_password.get_secret_value() if self.web_password is not None else ""

    def validate_research_safety(self) -> None:
        mode = self.strategy_mode.strip().upper()
        if mode not in {STRUCTURAL_MODE, DUAL40_MODE}:
            raise ValueError(f"unsupported P3_STRATEGY_MODE: {self.strategy_mode}")
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
        if self.web_refresh_ms < 250:
            raise ValueError("P3_WEB_REFRESH_MS below 250ms is not supported")
        if not self.web_username.strip():
            raise ValueError("P3_WEB_USERNAME cannot be empty")
        if not 300 <= self.web_session_ttl_sec <= 86_400:
            raise ValueError("P3_WEB_SESSION_TTL_SEC must be between 300 and 86400")
        if not 1 <= self.web_login_max_failures <= 20:
            raise ValueError("P3_WEB_LOGIN_MAX_FAILURES must be between 1 and 20")
        if not 60 <= self.web_login_window_sec <= 3600:
            raise ValueError("P3_WEB_LOGIN_WINDOW_SEC must be between 60 and 3600")
        if self.web_auth_required:
            password = self.web_password_value()
            if len(password) < 12:
                raise ValueError("P3_WEB_PASSWORD must be at least 12 characters when auth is enabled")
        if self.live_feature_enabled and not self.web_auth_required:
            raise ValueError("P3 WEB authentication is required whenever LIVE feature is enabled")

        if self.live_target_quantity_shares <= 0 or self.live_max_quantity_shares <= 0:
            raise ValueError("LIVE target/max share quantities must be positive")
        if self.live_target_quantity_shares > self.live_max_quantity_shares:
            raise ValueError("LIVE target shares cannot exceed LIVE hard max shares")
        if self.live_max_quantity_shares > self.max_quantity_shares:
            raise ValueError("LIVE quantity cap cannot exceed P3 research quantity cap")
        if self.live_max_capital_per_cycle_usdc < 0:
            raise ValueError("deprecated LIVE capital knob cannot be negative")
        if self.live_min_net_profit_usdc < 0 or self.live_min_net_roi < 0:
            raise ValueError("LIVE edge thresholds cannot be negative")
        if self.live_min_collateral_to_arm_usdc < 0:
            raise ValueError("LIVE minimum collateral cannot be negative")
        if self.live_max_single_leg_notional_usdc <= 0:
            raise ValueError("LIVE single-leg notional cap must be positive")
        if self.live_max_projected_unwind_loss_usdc < 0:
            raise ValueError("LIVE projected unwind loss cap cannot be negative")
        if self.live_emergency_unwind_loss_usdc < self.live_max_projected_unwind_loss_usdc:
            raise ValueError("LIVE emergency unwind loss cap must be >= projected unwind loss cap")
        if self.live_min_edge_to_unwind_loss_ratio < 0:
            raise ValueError("LIVE edge/unwind-loss ratio cannot be negative")
        if self.live_rolling_24h_gross_loss_limit_usdc <= 0:
            raise ValueError("LIVE rolling 24h loss limit must be positive")
        if self.live_poll_interval_ms < 20:
            raise ValueError("P3_LIVE_POLL_INTERVAL_MS below 20ms is not supported")
        if self.live_settlement_wait_sec <= 0 or self.live_settlement_poll_sec <= 0:
            raise ValueError("LIVE settlement timings must be positive")
        if self.live_chain_id != 137:
            raise ValueError("P3 LIVE supports Polygon mainnet chain_id=137 only")
        if mode == STRUCTURAL_MODE and not self.live_buy_merge_only:
            raise ValueError("structural LIVE supports BUY+MERGE only")
        if self.live_auto_execute_enabled and not self.live_feature_enabled:
            raise ValueError("LIVE auto execution cannot be enabled while LIVE feature is disabled")

        if self.dual40_active:
            allowed_assets = {"BTC", "ETH", "SOL", "XRP"}
            assets = self.dual40_assets()
            if set(assets) - allowed_assets:
                raise ValueError("DUAL40 assets must be BTC/ETH/SOL/XRP")
            if len(set(assets)) != len(assets):
                raise ValueError("DUAL40 assets cannot contain duplicates")
            if self.dual40_horizon.strip().lower() != "5m":
                raise ValueError("DUAL40 currently supports 5m only")
            ladder = self.dual40_ladder()
            if ladder != (5.0, 10.0, 30.0):
                raise ValueError("DUAL40 recovery ladder is hard-locked to 5,10,30")
            if abs(self.dual40_price - 0.40) > 1e-12:
                raise ValueError("DUAL40 maker price is hard-locked to 0.40")
            if self.dual40_near_touch_price < self.dual40_price:
                raise ValueError("DUAL40 near-touch price cannot be below maker price")
            if self.dual40_market_age_sec < self.dual40_lookback_sec:
                raise ValueError("DUAL40 market age must cover lookback")
            if self.dual40_min_tte_sec <= self.dual40_cancel_tte_sec:
                raise ValueError("DUAL40 min TTE must exceed cancel TTE")
            if self.dual40_confirm_sec <= 0:
                raise ValueError("DUAL40 confirmation must be positive")
            if not 0.0 <= self.dual40_max_one_way_ratio <= 1.0:
                raise ValueError("DUAL40 one-way ratio must be in [0,1]")
            if self.dual40_book_fresh_ms <= 0:
                raise ValueError("DUAL40 book freshness must be positive")
            if self.dual40_heartbeat_sec <= 0 or self.dual40_balance_poll_sec <= 0:
                raise ValueError("DUAL40 heartbeat/balance polling must be positive")
            if self.dual40_resolution_poll_sec <= 0:
                raise ValueError("DUAL40 resolution polling must be positive")
            if self.dual40_min_collateral_to_arm_usdc + 1e-9 < 30.0:
                raise ValueError("DUAL40 LIVE arm collateral cannot be below $30")
            if self.dual40_fill_epsilon <= 0:
                raise ValueError("DUAL40 fill epsilon must be positive")

    def ensure_directories(self) -> None:
        Path(self.p3_db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.reports_dir).mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_p3_settings() -> P3Settings:
    settings = P3Settings()
    settings.validate_research_safety()
    return settings
