from pathlib import Path
from types import SimpleNamespace

import p25_main
from p25_all5m_web import run_web as run_all5m_web
from p25_live_all5m_market import All5mMarketBuyController
from p25_live_xrp import XRP5mLivePilot
from p25_web_records import run_web as run_legacy_xrp_web


def _cfg(tmp_path: Path, strategy: str):
    return SimpleNamespace(
        paper_strategy_version=strategy,
        paper_min_edge=0.08,
        p25_live_feature_enabled=False,
        p25_live_armed=False,
        p25_live_arm_nonce="",
        p25_live_asset="XRP",
        p25_live_horizon="5m",
        p25_live_strategy_version=strategy,
        p25_live_max_stake_usdc=1.10,
        p25_live_max_price_drift_pct=0.10,
        p25_live_max_limit_price=0.83,
        p25_live_ledger_path=str(tmp_path / "live.sqlite"),
        p25_live_clob_host="https://clob.polymarket.com",
        p25_live_chain_id=137,
        p25_live_geoblock_url="https://polymarket.com/api/geoblock",
        p25_live_require_geoblock_clear=True,
        p25_live_settlement_wait_sec=1.0,
        p25_live_settlement_poll_sec=0.1,
    )


def test_repository_baseline_keeps_legacy_xrp_controller_for_deploy_smoke(tmp_path):
    controller, profile = p25_main._build_live_controller(
        _cfg(tmp_path, "INDEP_PTB_BINANCE_25C_5M_V1")
    )
    assert isinstance(controller, XRP5mLivePilot)
    assert profile == "XRP5M_LEGACY_BASELINE"
    assert controller.status()["scope"] == "XRP:5m"
    web_runner, web_profile = p25_main._web_runner_for_live_profile(profile)
    assert web_runner is run_legacy_xrp_web
    assert web_profile == "XRP5M_LEGACY_WEB"


def test_directional_v2_switches_to_signal_immediate_all5m_fak_controller(tmp_path):
    controller, profile = p25_main._build_live_controller(
        _cfg(tmp_path, "INDEP_PTB_BINANCE_DIRECTIONAL_5M_V2")
    )
    assert isinstance(controller, All5mMarketBuyController)
    assert profile == "ALL5M_MARKET_BUY_DRY_FIRST"
    web_runner, web_profile = p25_main._web_runner_for_live_profile(profile)
    assert web_runner is run_all5m_web
    assert web_profile == "ALL5M_WEB"
    status = controller.status()
    assert status["scope"] == "BTC/ETH/SOL/XRP:5m"
    assert status["dry_ready"] is False
    assert status["armed"] is False
    assert status["order_mode"] == "SIGNAL_IMMEDIATE_FAK_LIVE_EDGE_CAP"
    assert status["execution_price_mode"] == "SIGNAL_IMMEDIATE_LIMIT_CAP"
    assert status["paper_drift_enforced"] is False
    assert status["live_min_edge"] == 0.08
    assert status["pre_submit_book_check"] is False
    assert status["matching_engine_is_liquidity_gate"] is True
    assert status["parallel_execution"] is True
    assert status["max_parallel_workers"] == 4
    assert status["market_buy_usdc"] == 1.0
    assert status["partial_fill_ok"] is True
    assert status["local_share_min_gate"] is False
