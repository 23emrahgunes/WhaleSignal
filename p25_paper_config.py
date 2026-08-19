"""Configuration for the P2.5 paper-trading and scorecard layer.

Paper trading is a deterministic simulation only.  It never loads credentials,
signs an order or calls an execution endpoint.  One canonical paper entry is
attempted per market so market-level hit rate and PnL are not inflated by the many
forecast checkpoints recorded for the same market.
"""
from __future__ import annotations

from pydantic import Field

from p25_config import Settings


class PaperSettings(Settings):
    paper_trading_enabled: bool = Field(
        default=True,
        alias="PAPER_TRADING_ENABLED",
    )
    paper_strategy_version: str = Field(
        default="RESEARCH_PAPER_V1",
        alias="PAPER_STRATEGY_VERSION",
    )
    paper_starting_bankroll_usdc: float = Field(
        default=1000.0,
        alias="PAPER_STARTING_BANKROLL_USDC",
    )
    paper_stake_usdc: float = Field(
        default=2.50,
        alias="PAPER_STAKE_USDC",
    )
    paper_entry_checkpoint_5m: int = Field(
        default=60,
        alias="PAPER_ENTRY_CHECKPOINT_5M",
    )
    paper_entry_checkpoint_15m: int = Field(
        default=240,
        alias="PAPER_ENTRY_CHECKPOINT_15M",
    )
    paper_entry_checkpoint_1h: int = Field(
        default=600,
        alias="PAPER_ENTRY_CHECKPOINT_1H",
    )
    paper_min_confidence: float = Field(
        default=0.05,
        alias="PAPER_MIN_CONFIDENCE",
    )
    paper_min_agreement: float = Field(
        default=0.50,
        alias="PAPER_MIN_AGREEMENT",
    )
    paper_min_edge: float = Field(
        default=0.0,
        alias="PAPER_MIN_EDGE",
    )
    paper_min_price: float = Field(
        default=0.05,
        alias="PAPER_MIN_PRICE",
    )
    paper_max_price: float = Field(
        default=0.95,
        alias="PAPER_MAX_PRICE",
    )
    paper_slippage: float = Field(
        default=0.005,
        alias="PAPER_SLIPPAGE",
    )
    paper_fee_bps: float = Field(
        default=0.0,
        alias="PAPER_FEE_BPS",
    )
    paper_allowed_statuses_csv: str = Field(
        default="PROVISIONAL,VALIDATED",
        alias="PAPER_ALLOWED_STATUSES",
    )
    paper_allowed_grades_csv: str = Field(
        default="LOW,MEDIUM,HIGH",
        alias="PAPER_ALLOWED_GRADES",
    )
    paper_recent_limit: int = Field(
        default=50,
        alias="PAPER_RECENT_LIMIT",
    )

    def paper_entry_checkpoint(self, horizon: str) -> int:
        return {
            "5m": self.paper_entry_checkpoint_5m,
            "15m": self.paper_entry_checkpoint_15m,
            "1h": self.paper_entry_checkpoint_1h,
        }.get(horizon, self.paper_entry_checkpoint_5m)

    def paper_allowed_statuses(self) -> set[str]:
        return {
            value.strip().upper()
            for value in self.paper_allowed_statuses_csv.split(",")
            if value.strip()
        }

    def paper_allowed_grades(self) -> set[str]:
        return {
            value.strip().upper()
            for value in self.paper_allowed_grades_csv.split(",")
            if value.strip()
        }

    def enforce_phase_lock(self) -> None:
        super().enforce_phase_lock()
        if self.paper_trading_enabled and self.phase_rank < 25:
            raise SystemExit(
                "FATAL CONFIG ERROR: paper trading yalniz P2.5+ fazinda acilabilir."
            )
        if self.paper_trading_enabled and not self.forecast_recording_enabled:
            raise SystemExit(
                "FATAL CONFIG ERROR: paper trading icin forecast recording acik olmali."
            )
        if self.paper_starting_bankroll_usdc <= 0:
            raise SystemExit("FATAL CONFIG ERROR: paper bankroll pozitif olmali.")
        if self.paper_stake_usdc <= 0:
            raise SystemExit("FATAL CONFIG ERROR: paper stake pozitif olmali.")
        if not 0.0 <= self.paper_min_confidence <= 1.0:
            raise SystemExit("FATAL CONFIG ERROR: PAPER_MIN_CONFIDENCE 0..1 olmali.")
        if not 0.0 <= self.paper_min_agreement <= 1.0:
            raise SystemExit("FATAL CONFIG ERROR: PAPER_MIN_AGREEMENT 0..1 olmali.")
        if not 0.0 <= self.paper_min_price < self.paper_max_price <= 1.0:
            raise SystemExit("FATAL CONFIG ERROR: paper price araligi gecersiz.")
        if self.paper_slippage < 0 or self.paper_fee_bps < 0:
            raise SystemExit("FATAL CONFIG ERROR: paper maliyetleri negatif olamaz.")
        for horizon in ("5m", "15m", "1h"):
            checkpoint = self.paper_entry_checkpoint(horizon)
            if checkpoint not in self.checkpoints_for(horizon):
                raise SystemExit(
                    "FATAL CONFIG ERROR: paper entry checkpoint recorder "
                    f"checkpoint listesinde yok: {horizon}=T-{checkpoint}"
                )
