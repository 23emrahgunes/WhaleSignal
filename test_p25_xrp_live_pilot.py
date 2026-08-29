from pathlib import Path
from types import SimpleNamespace

from p25_live_xrp import (
    LivePilotLedger,
    LiveTrigger,
    XRP5mLivePilot,
    evaluate_live_trigger_scope,
)


class _Value:
    def __init__(self, value):
        self.value = value


class _Combo:
    def __init__(self, asset="XRP", horizon="5m"):
        self.asset = _Value(asset)
        self.horizon = _Value(horizon)
        self.key = f"{asset}:{horizon}"


class _Ref:
    def __init__(self, asset="XRP", horizon="5m"):
        self.combo = _Combo(asset, horizon)
        self.condition_id = "cond-1"
        self.market_id = "market-1"
        self.up_token_id = "up-token"
        self.down_token_id = "down-token"


def _cfg(**overrides):
    values = dict(
        p25_live_feature_enabled=True,
        p25_live_armed=True,
        p25_live_arm_nonce="pilot-0001",
        p25_live_asset="XRP",
        p25_live_horizon="5m",
        p25_live_strategy_version="DEEP_VALUE_25C_5M_DUAL_V1",
        p25_live_max_stake_usdc=1.10,
        p25_live_max_price_drift_pct=0.10,
        p25_live_max_limit_price=0.255,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _paper(**overrides):
    values = dict(
        status="OPEN",
        strategy_version="DEEP_VALUE_25C_5M_DUAL_V1",
        side="DOWN",
        shares=6.060606,
        fill_price=0.165,
        stake_usdc=1.0,
    )
    values.update(overrides)
    return values


def test_live_scope_accepts_exact_xrp5m_paper_cohort():
    trigger, reason = evaluate_live_trigger_scope(_cfg(), _Ref(), _paper())
    assert reason == "OK"
    assert trigger is not None
    assert trigger.combo_key == "XRP:5m"
    assert trigger.side == "DOWN"
    assert trigger.token_id == "down-token"


def test_live_scope_rejects_other_asset_and_other_strategy():
    trigger, reason = evaluate_live_trigger_scope(_cfg(), _Ref(asset="SOL"), _paper())
    assert trigger is None
    assert reason == "OUTSIDE_LIVE_SCOPE"

    trigger, reason = evaluate_live_trigger_scope(
        _cfg(),
        _Ref(),
        _paper(strategy_version="OLD_STRATEGY"),
    )
    assert trigger is None
    assert reason == "LIVE_STRATEGY_MISMATCH"


def test_live_scope_rejects_unarmed_or_over_110_cap():
    trigger, reason = evaluate_live_trigger_scope(
        _cfg(p25_live_armed=False),
        _Ref(),
        _paper(),
    )
    assert trigger is None
    assert reason == "LIVE_NOT_ARMED"

    trigger, reason = evaluate_live_trigger_scope(
        _cfg(),
        _Ref(),
        _paper(stake_usdc=1.11),
    )
    assert trigger is None
    assert reason == "LIVE_STAKE_CAP"

    trigger, reason = evaluate_live_trigger_scope(
        _cfg(),
        _Ref(),
        _paper(stake_usdc=1.10),
    )
    assert reason == "OK"
    assert trigger is not None


def test_fresh_depth_can_use_ten_percent_price_drift_but_not_more():
    class Level:
        def __init__(self, price, size):
            self.price = price
            self.size = size

    class Book:
        min_order_size = 1.0
        asks = [Level("0.170", "3"), Level("0.180", "4")]

    class Client:
        def get_order_book(self, _token):
            return Book()

    # Paper fill 16.5c with 10% drift permits up to 18.15c. Six shares can fill
    # through the 18c level.
    limit, capacity, min_size = XRP5mLivePilot._fresh_limit_for_quantity(
        Client(),
        token_id="token",
        requested_shares=6.0,
        max_live_limit_price=0.1815,
    )
    assert limit == 0.18
    assert capacity == 7.0
    assert min_size == 1.0

    # A tighter 17.5c ceiling cannot source all six shares and must fail closed.
    limit, capacity, _ = XRP5mLivePilot._fresh_limit_for_quantity(
        Client(),
        token_id="token",
        requested_shares=6.0,
        max_live_limit_price=0.175,
    )
    assert limit is None
    assert capacity == 3.0


def test_arm_nonce_is_restart_safe_one_shot(tmp_path: Path):
    ledger = LivePilotLedger(str(tmp_path / "live.sqlite"))
    trigger = LiveTrigger(
        condition_id="cond-1",
        market_id="market-1",
        combo_key="XRP:5m",
        strategy_version="DEEP_VALUE_25C_5M_DUAL_V1",
        side="UP",
        token_id="up-token",
        requested_shares=8.0,
        paper_fill_cap=0.125,
        paper_stake_usdc=1.0,
    )
    assert ledger.reserve(
        arm_nonce="pilot-0001",
        trigger=trigger,
        live_limit_price=0.132,
        collateral_before_usdc=10.0,
        country="SE",
        region=None,
    )
    assert ledger.consumed("pilot-0001")
    assert not ledger.reserve(
        arm_nonce="pilot-0001",
        trigger=trigger,
        live_limit_price=0.132,
        collateral_before_usdc=10.0,
        country="SE",
        region=None,
    )
    latest = ledger.latest()
    assert latest is not None
    assert latest["status"] == "RESERVED"
