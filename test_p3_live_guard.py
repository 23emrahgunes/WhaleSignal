from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from p3_config import P3Settings
from p3_live_preflight import run_live_preflight
from p3_live_state import LiveState, MODE_DRY, MODE_LIVE_ARMED, MODE_LIVE_HALTED


class _GeoResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def _geo(payload: dict):
    def opener(_req, timeout=5.0):
        assert timeout == 5.0
        return _GeoResponse(payload)

    return opener


def _settings(tmp_path, **overrides) -> P3Settings:
    values = {
        "p3_db_path": str(tmp_path / "p3.sqlite"),
        "p26_db_path": str(tmp_path / "p26.sqlite"),
        "live_feature_enabled": True,
        "live_auto_execute_enabled": True,
        "live_require_dry_validated": False,
        "live_max_capital_per_cycle_usdc": 1.0,
        "live_control_host": "127.0.0.1",
        "live_control_port": 18094,
        "web_host": "127.0.0.1",
        "web_port": 18093,
    }
    values.update(overrides)
    return P3Settings(**values)


def _secret(*, has_key: bool = True):
    return SimpleNamespace(
        has_private_key=has_key,
        wallet="0xabc" if has_key else None,
        funder=None,
        has_full_clob_creds=True if has_key else False,
        signature_type=0,
    )


def _dry_validated(_conn, _settings):
    return {
        "readiness": {"status": "DRY_VALIDATED"},
        "attempts_executed": 100,
        "cumulative_pnl_usdc": 10.0,
        "pair_completion_rate": 0.99,
        "one_leg_rate": 0.01,
    }


def test_live_state_always_starts_dry_and_restart_forgets_arm() -> None:
    state = LiveState(live_feature_enabled=True, auto_execute_enabled=True)
    assert state.snapshot().mode == MODE_DRY
    assert not state.can_auto_execute()
    state.arm({"ok": True, "checked_at_ms": 1, "reasons": []})
    assert state.snapshot().mode == MODE_LIVE_ARMED
    assert state.can_auto_execute()

    restarted = LiveState(live_feature_enabled=True, auto_execute_enabled=True)
    assert restarted.snapshot().mode == MODE_DRY
    assert not restarted.can_auto_execute()

    state.halt("TEST_FAILURE")
    assert state.snapshot().mode == MODE_LIVE_HALTED
    assert not state.can_auto_execute()


def test_live_state_refuses_failed_preflight() -> None:
    state = LiveState(live_feature_enabled=True, auto_execute_enabled=True)
    with pytest.raises(ValueError):
        state.arm({"ok": False, "reasons": ["NOPE"]})
    assert state.snapshot().mode == MODE_DRY


def test_zero_balance_connectivity_probe_passes_but_full_arm_fails(tmp_path) -> None:
    settings = _settings(tmp_path)

    def account_probe(**_kwargs):
        return {
            "signer": "0x123",
            "server_ok": {"ok": True},
            "balance_payload": {
                "balance": "0",
                "allowances": {"exchange": "1000000"},
            },
        }

    probe = run_live_preflight(
        settings,
        for_arming=False,
        account_probe=account_probe,
        geoblock_opener=_geo({"blocked": False, "country": "SE"}),
        secret_reader=lambda: _secret(),
        dry_summary_builder=_dry_validated,
    )
    assert probe["ok"] is True
    assert probe["purpose"] == "CONNECTIVITY_ONLY_NO_ORDER"
    assert probe["checks"]["clob"]["collateral_usdc"] == 0.0

    arm = run_live_preflight(
        settings,
        for_arming=True,
        account_probe=account_probe,
        geoblock_opener=_geo({"blocked": False, "country": "SE"}),
        secret_reader=lambda: _secret(),
        dry_summary_builder=_dry_validated,
    )
    assert arm["ok"] is False
    assert "INSUFFICIENT_COLLATERAL" in arm["reasons"]


def test_geoblock_is_hard_gate_before_live_auth(tmp_path) -> None:
    settings = _settings(tmp_path)
    called = False

    def account_probe(**_kwargs):
        nonlocal called
        called = True
        return {}

    result = run_live_preflight(
        settings,
        for_arming=True,
        account_probe=account_probe,
        geoblock_opener=_geo({"blocked": True, "country": "XX"}),
        secret_reader=lambda: _secret(),
        dry_summary_builder=_dry_validated,
    )
    assert result["ok"] is False
    assert "JURISDICTION_BLOCKED" in result["reasons"]
    assert called is False


def test_missing_private_key_fails_even_connectivity_only(tmp_path) -> None:
    settings = _settings(tmp_path)
    result = run_live_preflight(
        settings,
        for_arming=False,
        account_probe=lambda **_: pytest.fail("account probe must not run"),
        geoblock_opener=_geo({"blocked": False}),
        secret_reader=lambda: _secret(has_key=False),
        dry_summary_builder=_dry_validated,
    )
    assert result["ok"] is False
    assert "PRIVATE_KEY_MISSING" in result["reasons"]


def test_default_dry_validation_gate_blocks_early_live_arm(tmp_path) -> None:
    settings = _settings(tmp_path, live_require_dry_validated=True)

    def not_ready(_conn, _settings):
        return {
            "readiness": {"status": "NOT_READY"},
            "attempts_executed": 20,
            "cumulative_pnl_usdc": 8.6,
            "pair_completion_rate": 1.0,
            "one_leg_rate": 0.0,
        }

    result = run_live_preflight(
        settings,
        for_arming=True,
        account_probe=lambda **_: {
            "signer": "0x123",
            "server_ok": {"ok": True},
            "balance_payload": {
                "balance": "10000000",
                "allowances": {"exchange": "10000000"},
            },
        },
        geoblock_opener=_geo({"blocked": False}),
        secret_reader=lambda: _secret(),
        dry_summary_builder=not_ready,
    )
    assert result["ok"] is False
    assert "STRICT_DRY_NOT_VALIDATED" in result["reasons"]


def test_live_config_rejects_public_control_plane(tmp_path) -> None:
    settings = _settings(tmp_path, live_control_host="0.0.0.0")
    with pytest.raises(ValueError, match="loopback"):
        settings.validate_research_safety()


def test_auto_execute_cannot_be_enabled_without_live_feature(tmp_path) -> None:
    settings = _settings(
        tmp_path,
        live_feature_enabled=False,
        live_auto_execute_enabled=True,
    )
    with pytest.raises(ValueError, match="auto execution"):
        settings.validate_research_safety()
