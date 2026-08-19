"""Smoke-test the deployable P2.5 entrypoint and safety surface."""

import p25_main
from p25_config import Settings


def test_p25_entrypoint_imports_without_side_effects():
    assert callable(p25_main.run)
    assert callable(p25_main.main)


def test_p25_settings_have_no_execution_secrets():
    forbidden = {
        "private_key",
        "api_secret",
        "order_submit",
        "live_execution_enabled",
        "wallet_key",
    }
    assert forbidden.isdisjoint(Settings.model_fields)
