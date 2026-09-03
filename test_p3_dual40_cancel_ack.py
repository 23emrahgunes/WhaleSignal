from __future__ import annotations

import sys
import time
from types import SimpleNamespace

from p3_config import DUAL40_MODE, P3Settings
from p3_dual40_runtime import ProductionDual40MakerEngine
from p3_dual40_store import active_cycle, connect_dual40, ladder_state
from p3_live_dual40_gateway import Dual40Gateway, _cancel_response_ok
from p3_live_state import LiveState


def test_known_order_cancel_requires_every_requested_id():
    assert _cancel_response_ok(
        {"canceled": ["up-order", "down-order"], "not_canceled": {}},
        requested_order_ids=["up-order", "down-order"],
    )
    assert not _cancel_response_ok(
        {"canceled": ["up-order"], "not_canceled": {}},
        requested_order_ids=["up-order", "down-order"],
    )
    assert not _cancel_response_ok(
        {
            "canceled": ["up-order"],
            "not_canceled": {"down-order": "already matched or still open"},
        },
        requested_order_ids=["up-order", "down-order"],
    )
    assert not _cancel_response_ok(
        {"success": True},
        requested_order_ids=["up-order"],
    )


def test_cancel_ack_understands_camel_case_and_rejects_unknown_payloads():
    assert _cancel_response_ok({"canceled": [], "notCanceled": {}})
    assert not _cancel_response_ok(
        {"canceled": [], "notCanceled": {"order-1": "not cancelled"}}
    )
    assert _cancel_response_ok({"success": True})
    assert not _cancel_response_ok({})
    assert not _cancel_response_ok({"success": False})
    assert not _cancel_response_ok({"errorMsg": "cancel rejected"})


class _KnownOrderClob:
    def __init__(self, response):
        self.response = response
        self.requests: list[list[str]] = []

    def cancel_orders(self, order_ids):
        self.requests.append(list(order_ids))
        return self.response


def test_gateway_cancel_pair_exposes_incomplete_ack_as_failure():
    clob = _KnownOrderClob(
        {
            "canceled": ["up-order"],
            "not_canceled": {"down-order": "still resting"},
        }
    )
    gateway = Dual40Gateway.__new__(Dual40Gateway)
    gateway.clob = clob

    result = gateway.cancel_pair("up-order", "down-order")

    assert result["ok"] is False
    assert result["order_ids"] == ["up-order", "down-order"]
    assert clob.requests == [["up-order", "down-order"]]


def test_gateway_cancel_pair_accepts_complete_explicit_ack():
    clob = _KnownOrderClob(
        {
            "canceled": ["down-order", "up-order"],
            "not_canceled": {},
        }
    )
    gateway = Dual40Gateway.__new__(Dual40Gateway)
    gateway.clob = clob

    result = gateway.cancel_pair("up-order", "down-order")

    assert result["ok"] is True


class _OrderMarketCancelParams:
    def __init__(self, *, market=None, asset_id=None):
        self.market = market
        self.asset_id = asset_id


class _ScopedClob:
    def __init__(self):
        self.assets: list[str] = []

    def cancel_market_orders(self, payload):
        token_id = str(payload.asset_id)
        self.assets.append(token_id)
        if token_id == "down-token":
            return {
                "canceled": [],
                "not_canceled": {"down-order": "still resting"},
            }
        return {"canceled": ["up-order"], "not_canceled": {}}


def test_scoped_cancel_fails_when_either_token_reports_not_canceled(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "py_clob_client_v2",
        SimpleNamespace(OrderMarketCancelParams=_OrderMarketCancelParams),
    )
    clob = _ScopedClob()
    gateway = Dual40Gateway.__new__(Dual40Gateway)
    gateway.clob = clob

    result = gateway.cancel_market_pair(
        up_token_id="up-token",
        down_token_id="down-token",
    )

    assert result["ok"] is False
    assert [item["ok"] for item in result["responses"]] == [True, False]
    assert clob.assets == ["up-token", "down-token"]


class _CancelNotAcknowledgedGateway:
    def collateral_balance_usdc(self, *, refresh=True):
        return 35.0

    def pair_balances(self, *, up_token_id, down_token_id, refresh=True):
        return {"up": 0.0, "down": 0.0}

    def post_pair_post_only_gtc(self, **kwargs):
        return {
            "ok": False,
            "response_uncertain": True,
            "reconciliation_required": True,
            "error_code": "POST_ONLY_BATCH_EXCEPTION",
            "up_order_id": None,
            "down_order_id": None,
        }

    def cancel_pair(self, *order_ids, **kwargs):
        return {
            "ok": False,
            "order_ids": [],
            "scope": "DUAL40_TOKEN_PAIR",
            "response": {
                "canceled": [],
                "not_canceled": {"unknown-order": "still resting"},
            },
        }


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


def test_unacknowledged_cancel_keeps_cycle_active_and_halts_live(tmp_path):
    settings = _settings(tmp_path)
    state = LiveState(live_feature_enabled=True, auto_execute_enabled=True)
    state.arm({"ok": True, "checked_at_ms": int(time.time() * 1000)})
    gateway = _CancelNotAcknowledgedGateway()
    engine = ProductionDual40MakerEngine(
        settings,
        state,
        gateway_factory=lambda _: gateway,
    )
    engine._fresh_preflight = lambda: True

    conn = connect_dual40(settings.p3_db_path)
    try:
        result = engine._open_live(
            conn,
            {
                "condition_id": "cond-cancel-ack",
                "combo_key": "BTC:5m",
                "market_end_ts_ms": int(time.time() * 1000) + 240_000,
                "up_token_id": "up-token",
                "down_token_id": "down-token",
            },
            ladder_state(conn, "LIVE"),
        )
        current = active_cycle(conn)

        assert result["status"] == "CANCELLING_RETRY_HALT"
        assert current is not None
        assert current["status"] == "CANCELLING"
        assert current["error_code"] == "DUAL40_CANCEL_RETRY_REQUIRED"
        snapshot = state.snapshot()
        assert snapshot.mode == "LIVE_HALTED"
        assert snapshot.halted is True
        assert snapshot.reason == "DUAL40_CANCEL_RETRY_REQUIRED"
    finally:
        conn.close()
