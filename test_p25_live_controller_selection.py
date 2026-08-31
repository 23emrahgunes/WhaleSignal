from pathlib import Path
from types import SimpleNamespace

import p25_main
from p25_live_all5m_market import All5mMarketBuyController
from p25_live_xrp import XRP5mLivePilot


def _cfg(tmp_path: Path, strategy: str):
    return SimpleNamespace(
        paper_strategy_version=strategy,
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


def test_directional_v2_switches_to_dry_first_all5m_market_buy_controller(tmp_path):
    controller, profile = p25_main._build_live_controller(
        _cfg(tmp_path, "INDEP_PTB_BINANCE_DIRECTIONAL_5M_V2")
    )
    assert isinstance(controller, All5mMarketBuyController)
    assert profile == "ALL5M_MARKET_BUY_DRY_FIRST"
    status = controller.status()
    assert status["scope"] == "BTC/ETH/SOL/XRP:5m"
    assert status["dry_ready"] is False
    assert status["armed"] is False
    assert status["order_mode"] == "MARKET_BUY_FAK_USDC"
    assert status["market_buy_usdc"] == 1.0
    assert status["min_fak_depth_usdc"] == 0.25
    assert status["partial_fill_ok"] is True
    assert status["local_share_min_gate"] is False
