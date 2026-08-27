"""Configuration extension for P2.5 DEEP_VALUE_WATCH paper research.

This remains simulation-only. The extra settings control a tick-level low-price
watch that uses the public P2.6 full-depth book as fill evidence. No credentials,
signing or order submission are introduced.
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
        default=0.10,
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
