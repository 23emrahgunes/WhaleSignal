from pathlib import Path


def test_directional_v2_deploy_uses_all5m_dry_required_live_envelope():
    text = Path("deploy_p25_strict.sh").read_text(encoding="utf-8")
    assert "INDEP_PTB_BINANCE_DIRECTIONAL_5M_V2" in text
    assert "P25_LIVE_FEATURE_ENABLED': 'false'" in text
    assert "P25_LIVE_ARMED': 'false'" in text
    assert "P25_LIVE_MAX_STAKE_USDC': '1.10'" in text
    assert "P25_LIVE_MAX_PRICE_DRIFT_PCT': '0.10'" in text
    assert "P25_LIVE_MAX_LIMIT_PRICE': '0.83'" in text
    assert "/api/all5m-live/status" in text
    assert "ALL5M LIVE=DRY_REQUIRED+UNARMED" in text
    assert "min_arm_collateral_usdc" in text
