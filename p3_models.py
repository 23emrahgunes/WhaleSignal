"""Data models for P3 structural-arbitrage SHADOW research."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional


ARB_BUY_MERGE = "ARB_COMPLETE_SET_BUY_MERGE_V1"
ARB_SPLIT_SELL = "ARB_COMPLETE_SET_SPLIT_SELL_V1"
STRUCTURAL_STRATEGIES = {ARB_BUY_MERGE, ARB_SPLIT_SELL}


@dataclass(frozen=True)
class StructuralOpportunity:
    strategy: str
    condition_id: str
    combo_key: str
    detected_ts_ms: int
    up_book_id: int
    down_book_id: int
    up_book_ts_ms: int
    down_book_ts_ms: int
    source_skew_ms: int
    max_book_age_ms: int
    quantity_shares: float
    up_vwap: float
    down_vwap: float
    up_fee_usdc: float
    down_fee_usdc: float
    gross_edge_per_share: float
    gross_profit_usdc: float
    execution_buffer_usdc: float
    net_profit_usdc: float
    capital_usdc: float
    net_roi: float
    up_limit_price: float
    down_limit_price: float
    fee_lineage_ok: bool
    quality_status: str = "OK"

    def __post_init__(self) -> None:
        if self.strategy not in STRUCTURAL_STRATEGIES:
            raise ValueError(f"unsupported structural strategy: {self.strategy}")
        if not self.condition_id or not self.combo_key:
            raise ValueError("condition_id and combo_key are required")
        if self.quantity_shares <= 0:
            raise ValueError("quantity_shares must be positive")
        if self.capital_usdc <= 0:
            raise ValueError("capital_usdc must be positive")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class LegSimulation:
    filled_shares: float
    notional_usdc: float
    fee_usdc: float
    vwap: Optional[float]
    worst_price: Optional[float]
    complete: bool


@dataclass(frozen=True)
class ReplayOutcome:
    opportunity_id: int
    delay_ms: int
    target_ts_ms: int
    observed_ts_ms: Optional[int]
    strategy: str
    quantity_shares: float
    up_fill: bool
    down_fill: bool
    both_fill: bool
    outcome: str
    up_exec_price: Optional[float]
    down_exec_price: Optional[float]
    gross_profit_usdc: Optional[float]
    unwind_side: Optional[str]
    unwind_price: Optional[float]
    unwind_fee_usdc: Optional[float]
    unwind_loss_usdc: Optional[float]
    cycle_net_pnl_usdc: Optional[float]
    details: dict
