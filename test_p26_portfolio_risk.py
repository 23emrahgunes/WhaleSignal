import sqlite3

from p26_portfolio_risk import (
    PortfolioRiskPolicy,
    PortfolioRiskState,
    evaluate_portfolio_risk,
)


def _policy(**changes):
    values=dict(
        initial_equity_usdc=1000,max_open_positions_total=4,
        max_open_exposure_usdc=10,max_exposure_per_asset_usdc=3.0,
        max_exposure_per_horizon_usdc=5,max_overlapping_positions_per_asset=1,
        max_crypto_cluster_exposure_usdc=10,daily_loss_limit_usdc=25,
        max_drawdown_fraction=0.10,consecutive_loss_limit=3,cooldown_sec=900,
        global_kill_switch=False,
    )
    values.update(changes)
    return PortfolioRiskPolicy(**values)


def _state(**changes):
    values=dict(
        equity_usdc=1000,available_equity_usdc=1000,open_positions_total=0,
        open_exposure_usdc=0,asset_open_positions=0,asset_exposure_usdc=0,
        horizon_exposure_usdc=0,crypto_cluster_exposure_usdc=0,
        daily_realized_pnl_usdc=0,peak_equity_usdc=1000,
        drawdown_fraction=0,consecutive_losses=0,last_loss_ts_ms=None,
    )
    values.update(changes)
    return PortfolioRiskState(**values)


def test_portfolio_gate_allows_flat_small_stake_and_blocks_overlap():
    allowed=evaluate_portfolio_risk(
        _state(),policy=_policy(),candidate_stake_usdc=2.5,
        projected_fee_usdc=0.01,now_ms=1000,
    )
    assert allowed.allowed
    overlap=evaluate_portfolio_risk(
        _state(asset_open_positions=1,asset_exposure_usdc=2.5),
        policy=_policy(max_exposure_per_asset_usdc=10),
        candidate_stake_usdc=2.5,projected_fee_usdc=0,now_ms=1000,
    )
    assert not overlap.allowed
    assert overlap.reason=="CORRELATION_CLUSTER_EXCEEDED"


def test_portfolio_gate_blocks_bankroll_drawdown_and_cooldown():
    bankroll=evaluate_portfolio_risk(
        _state(available_equity_usdc=1),policy=_policy(),
        candidate_stake_usdc=2.5,projected_fee_usdc=0,now_ms=1000,
    )
    assert bankroll.reason=="INSUFFICIENT_PAPER_BANKROLL"
    drawdown=evaluate_portfolio_risk(
        _state(drawdown_fraction=0.11),policy=_policy(),
        candidate_stake_usdc=2.5,projected_fee_usdc=0,now_ms=1000,
    )
    assert drawdown.reason=="MAX_DRAWDOWN_KILL_SWITCH"
    cooldown=evaluate_portfolio_risk(
        _state(consecutive_losses=3,last_loss_ts_ms=900),policy=_policy(cooldown_sec=10),
        candidate_stake_usdc=2.5,projected_fee_usdc=0,now_ms=1000,
    )
    assert cooldown.reason=="COOLDOWN_ACTIVE"
