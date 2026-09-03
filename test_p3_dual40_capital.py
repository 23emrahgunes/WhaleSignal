from pathlib import Path
from types import SimpleNamespace

import pytest

import p3_live_preflight as preflight
from p3_config import DUAL40_MODE, P3Settings
from p3_dual40_capital import required_live_collateral
from p3_dual40_core import Dual40Policy


def test_remaining_path_collateral_falls_only_by_realized_losses():
    policy = Dual40Policy(price=0.40, ladder=(5.0, 10.0, 30.0))

    assert required_live_collateral(
        policy=policy,
        level_index=0,
        initial_arm_floor_usdc=35.0,
    ) == pytest.approx(35.0)
    assert required_live_collateral(
        policy=policy,
        level_index=1,
        initial_arm_floor_usdc=35.0,
    ) == pytest.approx(33.0)
    assert required_live_collateral(
        policy=policy,
        level_index=2,
        initial_arm_floor_usdc=35.0,
    ) == pytest.approx(29.0)


def test_remaining_path_collateral_never_drops_below_remaining_strategy_capital():
    policy = Dual40Policy(price=0.40, ladder=(5.0, 10.0, 30.0))

    assert required_live_collateral(
        policy=policy,
        level_index=0,
        initial_arm_floor_usdc=1.0,
    ) == pytest.approx(30.0)
    assert required_live_collateral(
        policy=policy,
        level_index=1,
        initial_arm_floor_usdc=1.0,
    ) == pytest.approx(28.0)
    assert required_live_collateral(
        policy=policy,
        level_index=2,
        initial_arm_floor_usdc=1.0,
    ) == pytest.approx(24.0)


def test_remaining_path_collateral_rejects_invalid_level():
    policy = Dual40Policy(price=0.40, ladder=(5.0, 10.0, 30.0))

    with pytest.raises(ValueError, match="level_index"):
        required_live_collateral(
            policy=policy,
            level_index=-1,
            initial_arm_floor_usdc=35.0,
        )
    with pytest.raises(ValueError, match="level_index"):
        required_live_collateral(
            policy=policy,
            level_index=3,
            initial_arm_floor_usdc=35.0,
        )


def _settings(tmp_path) -> P3Settings:
    return P3Settings(
        _env_file=None,
        strategy_mode=DUAL40_MODE,
        p26_db_path=str(tmp_path / "p26.sqlite"),
        p3_db_path=str(tmp_path / "p3.sqlite"),
        reports_dir=str(tmp_path / "reports"),
        live_feature_enabled=True,
        live_auto_execute_enabled=True,
        dual40_min_collateral_to_arm_usdc=35.0,
    )


def _secrets():
    return SimpleNamespace(
        signature_type=0,
        funder=None,
        wallet="0xabc",
        has_private_key=True,
        has_full_clob_creds=True,
    )


def _patch_ready_runtime(monkeypatch, *, level_index: int, collateral: float):
    monkeypatch.setattr(
        preflight,
        "_geoblock",
        lambda settings, opener=None: {
            "blocked": False,
            "country": "SE",
            "region": None,
        },
    )
    monkeypatch.setattr(
        preflight,
        "_dual40_runtime_check",
        lambda settings: {
            "ok": True,
            "ladder_state": {
                "level_index": level_index,
                "loss_pool_usdc": 0.0,
                "hard_stopped": 0,
            },
            "active_cycle": None,
            "transport": {"ok": True},
            "active_5m_markets": 4,
            "maker_zero_fee_markets": 4,
            "markets": [],
        },
    )
    monkeypatch.setattr(
        preflight,
        "parse_clob_balance_usdc",
        lambda payload: collateral,
    )


def _account_probe(**kwargs):
    return {
        "server_ok": True,
        "signer": "0xabc",
        "balance_payload": {"allowances": {"exchange": "1"}},
    }


def test_preflight_requires_35_at_initial_level(monkeypatch, tmp_path):
    _patch_ready_runtime(monkeypatch, level_index=0, collateral=34.99)

    result = preflight.run_live_preflight(
        _settings(tmp_path),
        for_arming=True,
        account_probe=_account_probe,
        secret_reader=_secrets,
    )

    assert result["ok"] is False
    assert "INSUFFICIENT_COLLATERAL" in result["reasons"]
    assert result["checks"]["risk_config"]["required_collateral_now_usdc"] == pytest.approx(35.0)


def test_preflight_allows_33_after_first_realized_loss(monkeypatch, tmp_path):
    _patch_ready_runtime(monkeypatch, level_index=1, collateral=33.0)

    result = preflight.run_live_preflight(
        _settings(tmp_path),
        for_arming=True,
        account_probe=_account_probe,
        secret_reader=_secrets,
    )

    assert result["ok"] is True
    assert result["reasons"] == []
    assert result["checks"]["risk_config"]["current_level_index"] == 1
    assert result["checks"]["risk_config"]["required_collateral_now_usdc"] == pytest.approx(33.0)
    assert result["risk"]["required_collateral_now_usdc"] == pytest.approx(33.0)


def test_runtime_and_preflight_both_use_same_capital_function():
    preflight_source = Path("p3_live_preflight.py").read_text(encoding="utf-8")
    runtime_source = "\n".join(
        [
            Path("p3_dual40_runtime.py").read_text(encoding="utf-8"),
            Path("p3_dual40_runtime_impl.py").read_text(encoding="utf-8"),
        ]
    )

    assert "required_live_collateral(" in preflight_source
    assert "required_live_collateral(" in runtime_source
    assert "required_remaining_collateral_usdc" in runtime_source