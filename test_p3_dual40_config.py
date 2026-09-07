import pytest

from p3_config import DUAL40_MODE, P3Settings


def _settings(tmp_path, **overrides):
    values = {
        "P3_STRATEGY_MODE": DUAL40_MODE,
        "P3_P26_DB_PATH": str(tmp_path / "p26.sqlite"),
        "P3_DB_PATH": str(tmp_path / "p3.sqlite"),
        "P3_MAX_CAPITAL_PER_CYCLE_USDC": "30",
        "P3_MAX_QUANTITY_SHARES": "30",
        "P3_WEB_AUTH_REQUIRED": "false",
        "P3_LIVE_FEATURE_ENABLED": "false",
        "P3_LIVE_AUTO_EXECUTE_ENABLED": "false",
    }
    values.update(overrides)
    return P3Settings(**values)


def test_dual40_contract_is_40c_5_10_30_and_35_arm_floor(tmp_path):
    settings = _settings(tmp_path)
    settings.validate_research_safety()
    assert settings.dual40_active is True
    assert settings.dual40_price == 0.40
    assert settings.dual40_ladder() == (5.0, 10.0, 30.0)
    assert settings.dual40_min_collateral_to_arm_usdc == 35.0
    assert settings.dual40_paper_max_concurrent_assets == 4
    assert settings.dual40_live_max_concurrent_assets == 1
    assert settings.dual40_opening_mode() == "SHADOW"
    assert settings.dual40_forecast_mode() == "SHADOW"
    assert settings.dual40_risk_mode() == "SHADOW"
    assert settings.dual40_gate_profile(0)["observation_sec"] == 35.0
    assert settings.dual40_gate_profile(2)["forecast_min_p_up"] == 0.49


def test_dual40_rejects_uncapped_ladder(tmp_path):
    settings = _settings(tmp_path, P3_DUAL40_LADDER="5,10,30,90")
    with pytest.raises(ValueError, match="hard-locked"):
        settings.validate_research_safety()


def test_dual40_rejects_live_capital_below_full_ladder(tmp_path):
    settings = _settings(tmp_path, P3_DUAL40_MIN_COLLATERAL_TO_ARM_USDC="29.99")
    with pytest.raises(ValueError, match=r"below \$30"):
        settings.validate_research_safety()


def test_dual40_rejects_invalid_concurrency(tmp_path):
    settings = _settings(tmp_path, P3_DUAL40_PAPER_MAX_CONCURRENT_ASSETS="5")
    with pytest.raises(ValueError, match="P3_DUAL40_PAPER_MAX_CONCURRENT_ASSETS"):
        settings.validate_research_safety()

    settings = _settings(tmp_path, P3_DUAL40_LIVE_MAX_CONCURRENT_ASSETS="0")
    with pytest.raises(ValueError, match="P3_DUAL40_LIVE_MAX_CONCURRENT_ASSETS"):
        settings.validate_research_safety()

    settings = _settings(tmp_path, P3_DUAL40_LIVE_MAX_CONCURRENT_ASSETS="2")
    with pytest.raises(ValueError, match="must remain 1"):
        settings.validate_research_safety()


def test_dual40_rejects_invalid_selective_gate_configuration(tmp_path):
    settings = _settings(tmp_path, P3_DUAL40_OPENING_GATE_MODE="MAYBE")
    with pytest.raises(ValueError, match="P3_DUAL40_OPENING_GATE_MODE"):
        settings.validate_research_safety()

    settings = _settings(tmp_path, P3_DUAL40_FORECAST_MIN_P_UP="0.45,0.48")
    with pytest.raises(ValueError, match="P3_DUAL40_FORECAST_MIN_P_UP"):
        settings.validate_research_safety()

    settings = _settings(
        tmp_path,
        P3_DUAL40_FORECAST_STATE_URL="https://example.com/api/state",
    )
    with pytest.raises(ValueError, match="local HTTP"):
        settings.validate_research_safety()
