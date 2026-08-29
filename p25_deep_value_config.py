"""Configuration extension for P2.5 DEEP_VALUE_WATCH research and guarded LIVE pilot.

Paper behavior remains the default.  The optional P25 LIVE block is disabled unless
explicitly enabled and armed; when enabled it is hard-locked to the XRP 5m paper
cohort and a one-cycle arm nonce.
"""
from __future__ import annotations

from pydantic import Field

from p25_paper_config import PaperSettings


class DeepValuePaperSettings(PaperSettings):
    paper_entry_mode: str = Field(
        default="CHECKPOINT",
        alias="PAPER_ENTRY_MODE",
    )
    paper_deep_value_min_ask: float = Field(
        default=0.01,
        alias="PAPER_DEEP_VALUE_MIN_ASK",
    )
    paper_deep_value_max_ask: float = Field(
        default=0.25,
        alias="PAPER_DEEP_VALUE_MAX_ASK",
    )
    paper_deep_value_prefilter_buffer: float = Field(
        default=0.03,
        alias="PAPER_DEEP_VALUE_PREFILTER_BUFFER",
    )
    paper_deep_value_min_tte_sec: float = Field(
        default=5.0,
        alias="PAPER_DEEP_VALUE_MIN_TTE_SEC",
    )
    paper_deep_value_p26_db_path: str = Field(
        default="data/p26_research.sqlite",
        alias="PAPER_DEEP_VALUE_P26_DB_PATH",
    )
    paper_deep_value_max_book_age_ms: int = Field(
        default=1500,
        alias="PAPER_DEEP_VALUE_MAX_BOOK_AGE_MS",
    )
    paper_deep_value_require_depth: bool = Field(
        default=True,
        alias="PAPER_DEEP_VALUE_REQUIRE_DEPTH",
    )
    paper_deep_value_require_fee_schedule: bool = Field(
        default=True,
        alias="PAPER_DEEP_VALUE_REQUIRE_FEE_SCHEDULE",
    )
    paper_deep_value_min_value_multiple: float = Field(
        default=1.50,
        alias="PAPER_DEEP_VALUE_MIN_VALUE_MULTIPLE",
    )
    paper_deep_value_horizons_csv: str = Field(
        default="5m,15m,1h",
        alias="PAPER_DEEP_VALUE_HORIZONS",
    )

    # ----- Guarded directional LIVE pilot: disabled + unarmed by default -----
    p25_live_feature_enabled: bool = Field(
        default=False,
        alias="P25_LIVE_FEATURE_ENABLED",
    )
    p25_live_armed: bool = Field(
        default=False,
        alias="P25_LIVE_ARMED",
    )
    p25_live_arm_nonce: str = Field(
        default="",
        alias="P25_LIVE_ARM_NONCE",
    )
    p25_live_asset: str = Field(
        default="XRP",
        alias="P25_LIVE_ASSET",
    )
    p25_live_horizon: str = Field(
        default="5m",
        alias="P25_LIVE_HORIZON",
    )
    p25_live_strategy_version: str = Field(
        default="DEEP_VALUE_25C_5M_DUAL_V1",
        alias="P25_LIVE_STRATEGY_VERSION",
    )
    p25_live_max_stake_usdc: float = Field(
        default=1.0,
        alias="P25_LIVE_MAX_STAKE_USDC",
    )
    p25_live_max_limit_price: float = Field(
        default=0.255,
        alias="P25_LIVE_MAX_LIMIT_PRICE",
    )
    p25_live_ledger_path: str = Field(
        default="data/p25_live_direction.sqlite",
        alias="P25_LIVE_LEDGER_PATH",
    )
    p25_live_clob_host: str = Field(
        default="https://clob.polymarket.com",
        alias="P25_LIVE_CLOB_HOST",
    )
    p25_live_chain_id: int = Field(
        default=137,
        alias="P25_LIVE_CHAIN_ID",
    )
    p25_live_geoblock_url: str = Field(
        default="https://polymarket.com/api/geoblock",
        alias="P25_LIVE_GEOBLOCK_URL",
    )
    p25_live_require_geoblock_clear: bool = Field(
        default=True,
        alias="P25_LIVE_REQUIRE_GEOBLOCK_CLEAR",
    )
    p25_live_settlement_wait_sec: float = Field(
        default=15.0,
        alias="P25_LIVE_SETTLEMENT_WAIT_SEC",
    )
    p25_live_settlement_poll_sec: float = Field(
        default=0.5,
        alias="P25_LIVE_SETTLEMENT_POLL_SEC",
    )

    @property
    def paper_deep_value_enabled(self) -> bool:
        return self.paper_entry_mode.strip().upper() == "DEEP_VALUE_WATCH"

    def paper_deep_value_horizons(self) -> set[str]:
        return {
            value.strip().lower()
            for value in self.paper_deep_value_horizons_csv.split(",")
            if value.strip()
        }

    def enforce_phase_lock(self) -> None:
        super().enforce_phase_lock()
        mode = self.paper_entry_mode.strip().upper()
        if mode not in {"CHECKPOINT", "DEEP_VALUE_WATCH"}:
            raise SystemExit(
                "FATAL CONFIG ERROR: PAPER_ENTRY_MODE CHECKPOINT veya DEEP_VALUE_WATCH olmali."
            )
        if not 0.0 < self.paper_deep_value_min_ask < self.paper_deep_value_max_ask < 1.0:
            raise SystemExit("FATAL CONFIG ERROR: deep-value ask araligi gecersiz.")
        if self.paper_deep_value_prefilter_buffer < 0:
            raise SystemExit("FATAL CONFIG ERROR: deep-value prefilter buffer negatif olamaz.")
        if self.paper_deep_value_min_tte_sec < 0:
            raise SystemExit("FATAL CONFIG ERROR: deep-value minimum TTE negatif olamaz.")
        if self.paper_deep_value_max_book_age_ms < 100:
            raise SystemExit("FATAL CONFIG ERROR: deep-value book age limiti en az 100ms olmali.")
        if self.paper_deep_value_min_value_multiple < 1.0:
            raise SystemExit("FATAL CONFIG ERROR: deep-value value multiple en az 1.0 olmali.")
        allowed_horizons = self.paper_deep_value_horizons()
        if not allowed_horizons or not allowed_horizons.issubset({"5m", "15m", "1h"}):
            raise SystemExit(
                "FATAL CONFIG ERROR: PAPER_DEEP_VALUE_HORIZONS yalniz 5m,15m,1h icerebilir."
            )

        if self.p25_live_feature_enabled:
            if self.p25_live_asset.strip().upper() != "XRP":
                raise SystemExit("FATAL CONFIG ERROR: ilk P25 LIVE pilot yalniz XRP olabilir.")
            if self.p25_live_horizon.strip().lower() != "5m":
                raise SystemExit("FATAL CONFIG ERROR: ilk P25 LIVE pilot yalniz 5m olabilir.")
            if not self.paper_deep_value_enabled:
                raise SystemExit("FATAL CONFIG ERROR: P25 LIVE pilot DEEP_VALUE_WATCH gerektirir.")
            if "5m" not in allowed_horizons:
                raise SystemExit("FATAL CONFIG ERROR: P25 LIVE icin 5m paper horizon acik olmali.")
            if self.paper_strategy_version != self.p25_live_strategy_version:
                raise SystemExit("FATAL CONFIG ERROR: LIVE strategy paper strategy ile birebir ayni olmali.")
            if self.paper_stake_usdc > self.p25_live_max_stake_usdc + 1e-12:
                raise SystemExit("FATAL CONFIG ERROR: paper stake LIVE hard cap'i asiyor.")
            if self.p25_live_max_stake_usdc <= 0:
                raise SystemExit("FATAL CONFIG ERROR: P25 LIVE max stake pozitif olmali.")
            if not 0.0 < self.p25_live_max_limit_price < 1.0:
                raise SystemExit("FATAL CONFIG ERROR: P25 LIVE price cap gecersiz.")
            if self.p25_live_chain_id != 137:
                raise SystemExit("FATAL CONFIG ERROR: P25 LIVE yalniz Polygon chain_id=137 destekler.")
            if self.p25_live_settlement_wait_sec <= 0 or self.p25_live_settlement_poll_sec <= 0:
                raise SystemExit("FATAL CONFIG ERROR: P25 LIVE settlement sureleri pozitif olmali.")
            if self.p25_live_armed and len(self.p25_live_arm_nonce.strip()) < 8:
                raise SystemExit("FATAL CONFIG ERROR: P25 LIVE arm nonce en az 8 karakter olmali.")
