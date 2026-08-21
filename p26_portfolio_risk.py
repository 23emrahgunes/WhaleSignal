"""Fail-closed fixed-stake portfolio controls for RESEARCH_PAPER_V2."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class PortfolioRiskPolicy:
    initial_equity_usdc: float
    max_open_positions_total: int
    max_open_exposure_usdc: float
    max_exposure_per_asset_usdc: float
    max_exposure_per_horizon_usdc: float
    max_overlapping_positions_per_asset: int
    max_crypto_cluster_exposure_usdc: float
    daily_loss_limit_usdc: float
    max_drawdown_fraction: float
    consecutive_loss_limit: int
    cooldown_sec: int
    global_kill_switch: bool = False


@dataclass(frozen=True)
class PortfolioRiskState:
    equity_usdc: float
    available_equity_usdc: float
    open_positions_total: int
    open_exposure_usdc: float
    asset_open_positions: int
    asset_exposure_usdc: float
    horizon_exposure_usdc: float
    crypto_cluster_exposure_usdc: float
    daily_realized_pnl_usdc: float
    peak_equity_usdc: float
    drawdown_fraction: float
    consecutive_losses: int
    last_loss_ts_ms: Optional[int]


@dataclass(frozen=True)
class PortfolioRiskResult:
    allowed: bool
    reason: str
    state: PortfolioRiskState
    details: tuple[str, ...] = ()


def _utc_day_start_ms(now_ms: int) -> int:
    dt = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    start = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
    return int(start.timestamp() * 1000)


def policy_from_settings(settings) -> PortfolioRiskPolicy:  # noqa: ANN001
    return PortfolioRiskPolicy(
        initial_equity_usdc=settings.paper_v2_initial_equity_usdc,
        max_open_positions_total=settings.paper_v2_max_open_positions_total,
        max_open_exposure_usdc=settings.paper_v2_max_open_exposure_usdc,
        max_exposure_per_asset_usdc=settings.paper_v2_max_exposure_per_asset_usdc,
        max_exposure_per_horizon_usdc=settings.paper_v2_max_exposure_per_horizon_usdc,
        max_overlapping_positions_per_asset=settings.paper_v2_max_overlapping_positions_per_asset,
        max_crypto_cluster_exposure_usdc=settings.paper_v2_max_crypto_cluster_exposure_usdc,
        daily_loss_limit_usdc=settings.paper_v2_daily_loss_limit_usdc,
        max_drawdown_fraction=settings.paper_v2_max_drawdown_fraction,
        consecutive_loss_limit=settings.paper_v2_consecutive_loss_limit,
        cooldown_sec=settings.paper_v2_cooldown_sec,
        global_kill_switch=settings.paper_v2_global_kill_switch,
    )


def portfolio_state(
    conn: sqlite3.Connection,
    *,
    policy: PortfolioRiskPolicy,
    asset: str,
    horizon: str,
    strategy_version: str = "RESEARCH_PAPER_V2",
    now_ms: Optional[int] = None,
) -> PortfolioRiskState:
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    rows = conn.execute(
        """
        SELECT status,combo_key,horizon,stake_usdc,fee_usdc,realized_pnl,
               settled_at_ms,created_at_ms
        FROM p26_paper_trades
        WHERE strategy_version=?
        ORDER BY COALESCE(settled_at_ms,created_at_ms),id
        """,
        (strategy_version,),
    ).fetchall()
    open_rows = [row for row in rows if row["status"] == "OPEN"]
    settled = [row for row in rows if row["status"] == "SETTLED"]
    realized = sum(float(row["realized_pnl"] or 0.0) for row in settled)
    equity = float(policy.initial_equity_usdc) + realized
    exposure = lambda row: float(row["stake_usdc"] or 0.0) + float(row["fee_usdc"] or 0.0)
    open_exposure = sum(exposure(row) for row in open_rows)
    asset_prefix = f"{asset.upper()}:"
    asset_rows = [row for row in open_rows if str(row["combo_key"]).upper().startswith(asset_prefix)]
    horizon_rows = [row for row in open_rows if str(row["horizon"]) == str(horizon)]
    day_start = _utc_day_start_ms(now)
    daily_pnl = sum(
        float(row["realized_pnl"] or 0.0)
        for row in settled
        if row["settled_at_ms"] is not None and int(row["settled_at_ms"]) >= day_start
    )
    running = float(policy.initial_equity_usdc)
    peak = running
    for row in settled:
        running += float(row["realized_pnl"] or 0.0)
        peak = max(peak, running)
    drawdown = max(0.0, (peak - equity) / peak) if peak > 0 else 1.0
    consecutive_losses = 0
    last_loss_ts: Optional[int] = None
    for row in reversed(settled):
        if float(row["realized_pnl"] or 0.0) < 0:
            consecutive_losses += 1
            if last_loss_ts is None and row["settled_at_ms"] is not None:
                last_loss_ts = int(row["settled_at_ms"])
        else:
            break
    return PortfolioRiskState(
        equity_usdc=equity,
        available_equity_usdc=max(0.0, equity - open_exposure),
        open_positions_total=len(open_rows),
        open_exposure_usdc=open_exposure,
        asset_open_positions=len(asset_rows),
        asset_exposure_usdc=sum(exposure(row) for row in asset_rows),
        horizon_exposure_usdc=sum(exposure(row) for row in horizon_rows),
        crypto_cluster_exposure_usdc=open_exposure,
        daily_realized_pnl_usdc=daily_pnl,
        peak_equity_usdc=peak,
        drawdown_fraction=drawdown,
        consecutive_losses=consecutive_losses,
        last_loss_ts_ms=last_loss_ts,
    )


def evaluate_portfolio_risk(
    state: PortfolioRiskState,
    *,
    policy: PortfolioRiskPolicy,
    candidate_stake_usdc: float,
    projected_fee_usdc: float,
    now_ms: Optional[int] = None,
) -> PortfolioRiskResult:
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    required = float(candidate_stake_usdc) + float(projected_fee_usdc)
    if policy.global_kill_switch:
        return PortfolioRiskResult(False, "GLOBAL_KILL_SWITCH", state)
    if state.available_equity_usdc + 1e-12 < required:
        return PortfolioRiskResult(False, "INSUFFICIENT_PAPER_BANKROLL", state)
    if state.open_positions_total >= policy.max_open_positions_total:
        return PortfolioRiskResult(False, "PORTFOLIO_POSITION_LIMIT", state)
    if state.open_exposure_usdc + required > policy.max_open_exposure_usdc + 1e-12:
        return PortfolioRiskResult(False, "PORTFOLIO_EXPOSURE_LIMIT", state)
    if state.asset_exposure_usdc + required > policy.max_exposure_per_asset_usdc + 1e-12:
        return PortfolioRiskResult(False, "ASSET_EXPOSURE_LIMIT", state)
    if state.horizon_exposure_usdc + required > policy.max_exposure_per_horizon_usdc + 1e-12:
        return PortfolioRiskResult(False, "HORIZON_EXPOSURE_LIMIT", state)
    if state.asset_open_positions >= policy.max_overlapping_positions_per_asset:
        return PortfolioRiskResult(False, "CORRELATION_CLUSTER_EXCEEDED", state)
    if state.crypto_cluster_exposure_usdc + required > policy.max_crypto_cluster_exposure_usdc + 1e-12:
        return PortfolioRiskResult(False, "CRYPTO_CLUSTER_EXPOSURE_LIMIT", state)
    if state.daily_realized_pnl_usdc <= -abs(policy.daily_loss_limit_usdc):
        return PortfolioRiskResult(False, "DAILY_LOSS_LIMIT", state)
    if state.drawdown_fraction >= policy.max_drawdown_fraction:
        return PortfolioRiskResult(False, "MAX_DRAWDOWN_KILL_SWITCH", state)
    if (
        state.consecutive_losses >= policy.consecutive_loss_limit
        and state.last_loss_ts_ms is not None
        and now < state.last_loss_ts_ms + policy.cooldown_sec * 1000
    ):
        return PortfolioRiskResult(False, "COOLDOWN_ACTIVE", state)
    return PortfolioRiskResult(True, "PASS", state)
