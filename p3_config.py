"""Configuration for the isolated P3 structural-arbitrage research lab.

P3 is strictly SHADOW/PAPER. It reads public P2.6 CLOB/fee data, writes a
separate SQLite database and never loads credentials, signs payloads or submits
orders. Dry-run settings model one independent attempt per opportunity window.
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

    # DRY policy: one independent simulated attempt per opportunity window.
    # Entry confirmation rejects the toxic first-print region: an opportunity must
    # still exist at/after opened_ts + confirmation before it becomes a DRY attempt.
    dry_enabled: bool = Field(default=True, alias="P3_DRY_ENABLED")
    dry_latency_ms: int = Field(default=100, alias="P3_DRY_LATENCY_MS")
    dry_entry_confirm_ms: int = Field(default=250, alias="P3_DRY_ENTRY_CONFIRM_MS")
    dry_survival_delays_ms: str = Field(
        default="0,50,100,200,250,500", alias="P3_DRY_SURVIVAL_DELAYS_MS"
    )
    dry_start_bankroll_usdc: float = Field(default=100.0, alias="P3_DRY_START_BANKROLL_USDC")
    dry_min_net_profit_usdc: float = Field(default=0.01, alias="P3_DRY_MIN_NET_PROFIT_USDC")
    dry_min_net_roi: float = Field(default=0.0025, alias="P3_DRY_MIN_NET_ROI")

    # Research promotion gates. They only report readiness; they never enable execution.
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
        if self.dry_latency_ms not in self.replay_delays():
            raise ValueError("P3_DRY_LATENCY_MS must exist in P3_REPLAY_DELAYS_MS")
        if self.dry_entry_confirm_ms < 0:
            raise ValueError("P3_DRY_ENTRY_CONFIRM_MS cannot be negative")
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

    def ensure_directories(self) -> None:
        Path(self.p3_db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.reports_dir).mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_p3_settings() -> P3Settings:
    settings = P3Settings()
    settings.validate_research_safety()
    return settings
