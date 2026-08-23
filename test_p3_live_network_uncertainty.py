from __future__ import annotations

from types import SimpleNamespace

import pytest

from p3_live_gateway import PolymarketLiveGateway
from p3_live_gateway_v2 import RiskAwarePolymarketLiveGateway


def _gateway() -> RiskAwarePolymarketLiveGateway:
    g = object.__new__(RiskAwarePolymarketLiveGateway)
    g.settings = SimpleNamespace(live_settlement_wait_sec=0.001, live_settlement_poll_sec=0.001)
    return g


def test_lost_pair_submit_response_is_marked_uncertain(monkeypatch) -> None:
    gateway = _gateway()

    def boom(_self, **_kwargs):
        raise TimeoutError("response lost after write")

    monkeypatch.setattr(PolymarketLiveGateway, "post_two_leg_fok", boom)
    result = gateway.post_two_leg_fok(
        up_token_id="u", down_token_id="d", quantity_shares=5,
        up_limit_price=0.4, down_limit_price=0.5,
    )
    assert result["submit_response_uncertain"] is True
    assert result["up_order_id"] is None and result["down_order_id"] is None
    assert result["submit_error"]["type"] == "TimeoutError"


def test_no_post_submit_balance_reads_fail_closed() -> None:
    gateway = _gateway()

    def unavailable(_token, *, refresh=True):
        raise TimeoutError("balance api down")

    gateway.conditional_balance_shares = unavailable
    with pytest.raises(RuntimeError, match="could not be observed"):
        gateway.wait_for_leg_deltas(
            up_token_id="u", down_token_id="d",
            before_up=0.0, before_down=0.0, quantity_shares=5.0,
        )


def test_unwind_balance_unknown_is_never_classified_flat() -> None:
    gateway = _gateway()

    def unavailable(_token, *, refresh=True):
        raise TimeoutError("balance api down")

    gateway.conditional_balance_shares = unavailable
    result = gateway.wait_for_unwind(token_id="u", before_entry_balance=0.0)
    assert result["verified"] is False
    assert result["balance_observation_uncertain"] is True
    assert result["residual"] == float("inf")
