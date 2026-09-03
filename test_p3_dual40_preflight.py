from types import SimpleNamespace

import p3_dual40_preflight as module


class _Settings:
    dual40_active = True


def test_dual40_preflight_replaces_legacy_transport_reason(monkeypatch):
    monkeypatch.setattr(
        module,
        "_base_preflight",
        lambda settings, for_arming, **kwargs: {
            "ok": False,
            "purpose": "ARM_LIVE",
            "reasons": [
                "DUAL40_BOOK_TRANSPORT_NOT_READY",
                "DUAL40_NO_MAKER_READY_5M_MARKET",
            ],
            "warnings": [],
            "checks": {"dual40": {"ok": False}},
        },
    )
    monkeypatch.setattr(
        module,
        "_runtime_check",
        lambda settings: {
            "ok": True,
            "ladder_state": {"hard_stopped": 0},
            "active_cycle": None,
            "transport": {"ok": True},
            "maker_zero_fee_markets": 4,
        },
    )

    result = module.run_dual40_preflight(_Settings(), for_arming=True)
    assert result["ok"] is True
    assert result["reasons"] == []
    assert result["checks"]["dual40"]["maker_zero_fee_markets"] == 4
    assert result["dual40_runtime_contract"] == "P26_BOOK_COLLECTOR_HEALTH_V1"


def test_dual40_preflight_preserves_non_dual_risk_failures(monkeypatch):
    monkeypatch.setattr(
        module,
        "_base_preflight",
        lambda settings, for_arming, **kwargs: {
            "ok": False,
            "purpose": "ARM_LIVE",
            "reasons": ["INSUFFICIENT_COLLATERAL"],
            "warnings": [],
            "checks": {},
        },
    )
    monkeypatch.setattr(
        module,
        "_runtime_check",
        lambda settings: {
            "ok": True,
            "ladder_state": {"hard_stopped": 0},
            "active_cycle": None,
            "transport": {"ok": True},
            "maker_zero_fee_markets": 1,
        },
    )

    result = module.run_dual40_preflight(_Settings(), for_arming=True)
    assert result["ok"] is False
    assert result["reasons"] == ["INSUFFICIENT_COLLATERAL"]
