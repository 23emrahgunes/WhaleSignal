"""Regression tests for P3 LIVE V3 fresh pair economics."""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from p3_config import P3Settings
from p3_live_executor_v3 import P3LiveExecutorV3
from p3_live_gateway_fresh import FreshEconomicPolymarketLiveGateway
from p3_live_gateway_v2 import RiskAwarePolymarketLiveGateway
from p3_live_sizing import buy_merge_metrics
from p3_live_state import LiveState


def _book(asks, bids=(), min_order_size=5.0):
    return {
        "asks": [{"price": p, "size": s} for p, s in asks],
        "bids": [{"price": p, "size": s} for p, s in bids],
        "min_order_size": min_order_size,
    }


def test_fresh_gateway_accepts_profitable_pair_even_if_one_leg_moved_above_old_limit():
    old = RiskAwarePolymarketLiveGateway.__new__(RiskAwarePolymarketLiveGateway)
    fresh = FreshEconomicPolymarketLiveGateway.__new__(FreshEconomicPolymarketLiveGateway)

    # Historical STRICT opportunity could have been UP<=0.40 / DOWN<=0.55.
    # Fresh book moved UP one cent worse but DOWN two cents better, so the PAIR is
    # actually more profitable: 0.41 + 0.53 = 0.94.
    up_book = _book([(0.41, 10.0)])
    down_book = _book([(0.53, 10.0)])

    assert old.buy_capacity_from_book(up_book, max_price=0.40)["capacity_shares"] == 0.0
    assert fresh.buy_capacity_from_book(up_book, max_price=0.40)["capacity_shares"] == 10.0

    up = fresh.quote_buy_from_book(up_book, shares=5.0, max_price=0.40)
    down = fresh.quote_buy_from_book(down_book, shares=5.0, max_price=0.55)

    assert up.complete is True
    assert down.complete is True
    assert up.worst_price == pytest.approx(0.41)
    assert down.worst_price == pytest.approx(0.53)

    metrics = buy_merge_metrics(
        quantity_shares=5.0,
        up_buy=up,
        down_buy=down,
        up_fee_usdc=0.0,
        down_fee_usdc=0.0,
        execution_buffer_per_share=0.0,
    )
    assert metrics["net_profit_usdc"] > 0.0
    assert metrics["net_profit_usdc"] == pytest.approx(0.3)


def test_fresh_gateway_does_not_force_bad_pair_through_edge_gate():
    fresh = FreshEconomicPolymarketLiveGateway.__new__(FreshEconomicPolymarketLiveGateway)
    up = fresh.quote_buy_from_book(_book([(0.43, 10.0)]), shares=5.0, max_price=0.40)
    down = fresh.quote_buy_from_book(_book([(0.58, 10.0)]), shares=5.0, max_price=0.55)

    metrics = buy_merge_metrics(
        quantity_shares=5.0,
        up_buy=up,
        down_buy=down,
        up_fee_usdc=0.0,
        down_fee_usdc=0.0,
        execution_buffer_per_share=0.0,
    )
    assert metrics["net_profit_usdc"] < 0.0


def test_v3_candidate_lookup_is_newest_first_and_revalidates_target_size():
    source = inspect.getsource(P3LiveExecutorV3._next_candidate)
    assert "ORDER BY opened_ts_ms DESC,id DESC" in source
    assert "LIMIT 2000" in source
    assert "LIMIT 200\n" not in source
    assert 'opp["strict_quantity_shares"] = strict_quantity' in source
    assert "live_target_quantity_shares" in source


def test_v3_confirmation_age_budget_is_executor_only_and_at_least_1500ms(monkeypatch):
    monkeypatch.delenv("P3_LIVE_V3_MAX_CONFIRMATION_AGE_MS", raising=False)
    settings = P3Settings(_env_file=None)
    original_scan = int(settings.scan_interval_ms)
    state = LiveState(live_feature_enabled=True, auto_execute_enabled=True)

    executor = P3LiveExecutorV3(settings, state)
    effective_age = (
        int(executor.settings.scan_interval_ms)
        + 3 * int(executor.settings.live_poll_interval_ms)
    )

    assert effective_age >= 1500
    assert int(settings.scan_interval_ms) == original_scan


def test_daemon_routes_live_execution_through_v3():
    text = Path("p3_daemon.py").read_text(encoding="utf-8")
    assert "from p3_live_executor_v3 import P3LiveExecutorV3" in text
    assert "P3LiveExecutorV3(settings, state).process_once" in text
    assert "P3 LIVE v3 result=" in text
