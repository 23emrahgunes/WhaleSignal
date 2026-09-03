from __future__ import annotations

import sys
import time
from types import SimpleNamespace

import pytest

import p3_dual40_runtime as runtime
from p3_config import DUAL40_MODE, P3Settings
from p3_dual40_paper import (
    observed_fill_from_visible_depth,
    visible_ask_capacity,
)
from p3_dual40_runtime import ProductionDual40MakerEngine
from p3_dual40_store import active_cycle, connect_dual40, ladder_state
from p3_live_dual40_gateway import Dual40Gateway
from p3_live_state import LiveState


def test_paper_fill_uses_max_visible_depth_without_reusing_snapshots():
    capacity = visible_ask_capacity(
        [(0.39, 1.0), (0.40, 2.0), (0.41, 100.0)],
        max_price=0.40,
    )
    assert capacity == pytest.approx(3.0)

    first = observed_fill_from_visible_depth(
        previous_filled=0.0,
        target_shares=30.0,
        visible_capacity=capacity,
    )
    repeated = observed_fill_from_visible_depth(
        previous_filled=first,
        target_shares=30.0,
        visible_capacity=capacity,
    )
    smaller_later = observed_fill_from_visible_depth(
        previous_filled=repeated,
        target_shares=30.0,
        visible_capacity=1.0,
    )
    larger_later = observed_fill_from_visible_depth(
        previous_filled=smaller_later,
        target_shares=30.0,
        visible_capacity=8.0,
    )

    assert first == pytest.approx(3.0)
    assert repeated == pytest.approx(3.0)
    assert smaller_later == pytest.approx(3.0)
    assert larger_later == pytest.approx(8.0)


class _OrderMarketCancelParams:
    def __init__(self, *, market=None, asset_id=None):
        self.market = market
        self.asset_id = asset_id


class _OrderArgs:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _PostOrdersV2Args:
    def __init__(self, *, order, orderType):
        self.order = order
        self.orderType = orderType


class _OrderType:
    GTC = "GTC"


class _Side:
    BUY = "BUY"


def _install_fake_live_sdk(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "py_clob_client_v2",
        SimpleNamespace(
            OrderMarketCancelParams=_OrderMarketCancelParams,
            OrderArgs=_OrderArgs,
            OrderType=_OrderType,
            PostOrdersV2Args=_PostOrdersV2Args,
            Side=_Side,
        ),
    )


class _ScopedCancelClob:
    def __init__(self):
        self.cancelled_assets: list[str] = []

    def cancel_market_orders(self, payload):
        self.cancelled_assets.append(str(payload.asset_id))
        return {"success": True}


def test_gateway_unknown_order_ids_cancel_only_the_two_outcome_tokens(monkeypatch):
    _install_fake_live_sdk(monkeypatch)
    clob = _ScopedCancelClob()
    gateway = Dual40Gateway.__new__(Dual40Gateway)
    gateway.clob = clob

    result = gateway.cancel_pair(
        None,
        None,
        up_token_id="up-token",
        down_token_id="down-token",
    )

    assert result["ok"] is True
    assert result["scope"] == "DUAL40_TOKEN_PAIR"
    assert result["fallback"] == "CANCEL_DUAL40_TOKEN_PAIR_NO_ORDER_IDS"
    assert clob.cancelled_assets == ["up-token", "down-token"]


class _TimeoutSubmitClob(_ScopedCancelClob):
    def post_heartbeat(self, heartbeat_id):
        return {"heartbeat_id": "hb-1"}

    def create_order(self, order):
        return order

    def post_orders(self, orders, post_only=False):
        assert post_only is True
        raise TimeoutError("response lost after request write")


def test_gateway_timeout_immediately_cancels_scoped_tokens(monkeypatch):
    _install_fake_live_sdk(monkeypatch)
    clob = _TimeoutSubmitClob()
    gateway = Dual40Gateway.__new__(Dual40Gateway)
    gateway.clob = clob

    result = gateway.post_pair_post_only_gtc(
        up_token_id="up-token",
        down_token_id="down-token",
        quantity_shares=5.0,
        price=0.40,
    )

    assert result["ok"] is False
    assert result["response_uncertain"] is True
    assert result["reconciliation_required"] is True
    assert result["emergency_cancel_pair"]["ok"] is True
    assert clob.cancelled_assets == ["up-token", "down-token"]


class _UncertainGateway:
    def __init__(self):
        self.cancel_calls: list[dict] = []

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
        self.cancel_calls.append({"order_ids": order_ids, **kwargs})
        return {"ok": True, "scope": "DUAL40_TOKEN_PAIR"}

    def merge_matched(self, **kwargs):
        raise AssertionError("no merge expected for a verified no-fill")


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


def test_uncertain_live_submit_is_cancelled_reconciled_and_persisted(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime.time, "sleep", lambda seconds: None)
    settings = _settings(tmp_path)
    state = LiveState(live_feature_enabled=True, auto_execute_enabled=True)
    state.arm({"ok": True, "checked_at_ms": int(time.time() * 1000)})
    gateway = _UncertainGateway()
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
                "condition_id": "cond-1",
                "combo_key": "BTC:5m",
                "market_end_ts_ms": int(time.time() * 1000) + 240_000,
                "up_token_id": "up-token",
                "down_token_id": "down-token",
            },
            ladder_state(conn, "LIVE"),
        )
        row = conn.execute(
            "SELECT * FROM p3_dual40_cycles WHERE condition_id='cond-1'"
        ).fetchone()

        assert result["status"] == "NO_FILL"
        assert row is not None
        assert row["status"] == "NO_FILL"
        assert float(row["realized_pnl_usdc"]) == pytest.approx(0.0)
        assert active_cycle(conn) is None
        assert state.snapshot().mode == "LIVE_HALTED"
        assert gateway.cancel_calls == [
            {
                "order_ids": (None, None),
                "up_token_id": "up-token",
                "down_token_id": "down-token",
            }
        ]
    finally:
        conn.close()
