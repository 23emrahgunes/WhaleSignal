from pathlib import Path
from types import SimpleNamespace

from p25_live_all5m import (
    All5mLiveController,
    All5mLiveLedger,
    All5mLiveTrigger,
    evaluate_all5m_trigger_scope,
)


class _Value:
    def __init__(self, value):
        self.value = value


class _Combo:
    def __init__(self, asset="BTC", horizon="5m"):
        self.asset = _Value(asset)
        self.horizon = _Value(horizon)
        self.key = f"{asset}:{horizon}"


class _Ref:
    def __init__(self, asset="BTC", horizon="5m", condition=None):
        self.combo = _Combo(asset, horizon)
        self.condition_id = condition or f"cond-{asset.lower()}"
        self.market_id = f"market-{asset.lower()}"
        self.up_token_id = f"{asset.lower()}-up"
        self.down_token_id = f"{asset.lower()}-down"


def _cfg(tmp_path: Path, **overrides):
    values = dict(
        p25_live_feature_enabled=True,
        p25_live_armed=True,
        p25_live_arm_nonce="all5m-session-1",
        p25_live_strategy_version="INDEP_PTB_BINANCE_DIRECTIONAL_5M_V2",
        paper_strategy_version="INDEP_PTB_BINANCE_DIRECTIONAL_5M_V2",
        p25_live_max_stake_usdc=1.10,
        p25_live_max_price_drift_pct=0.10,
        p25_live_max_limit_price=0.83,
        p25_live_ledger_path=str(tmp_path / "live.sqlite"),
        p25_live_clob_host="https://clob.polymarket.com",
        p25_live_chain_id=137,
        p25_live_geoblock_url="https://polymarket.com/api/geoblock",
        p25_live_require_geoblock_clear=True,
        p25_live_horizon="5m",
        p25_live_settlement_wait_sec=0.1,
        p25_live_settlement_poll_sec=0.01,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _paper(**overrides):
    values = dict(
        status="OPEN",
        strategy_version="INDEP_PTB_BINANCE_DIRECTIONAL_5M_V2",
        side="UP",
        shares=2.0,
        fill_price=0.50,
        stake_usdc=1.0,
    )
    values.update(overrides)
    return values


def test_all_four_assets_are_in_live_scope_and_non5m_is_rejected(tmp_path):
    cfg = _cfg(tmp_path)
    for asset in ("BTC", "ETH", "SOL", "XRP"):
        trigger, reason = evaluate_all5m_trigger_scope(cfg, _Ref(asset), _paper())
        assert reason == "OK"
        assert trigger is not None
        assert trigger.combo_key == f"{asset}:5m"

    trigger, reason = evaluate_all5m_trigger_scope(cfg, _Ref("BTC", "15m"), _paper())
    assert trigger is None
    assert reason == "OUTSIDE_LIVE_SCOPE"


def test_live_scope_keeps_exact_strategy_and_83c_hard_cap(tmp_path):
    cfg = _cfg(tmp_path)
    trigger, reason = evaluate_all5m_trigger_scope(
        cfg, _Ref("ETH"), _paper(strategy_version="OLD")
    )
    assert trigger is None
    assert reason == "LIVE_STRATEGY_MISMATCH"

    trigger, reason = evaluate_all5m_trigger_scope(
        cfg, _Ref("ETH"), _paper(fill_price=0.831)
    )
    assert trigger is None
    assert reason == "LIVE_PRICE_CAP"


def test_ledger_allows_many_markets_per_session_but_one_attempt_per_condition(tmp_path):
    ledger = All5mLiveLedger(str(tmp_path / "ledger.sqlite"))
    a = All5mLiveTrigger(
        "session-1", "cond-a", "m-a", "BTC:5m",
        "INDEP_PTB_BINANCE_DIRECTIONAL_5M_V2", "UP", "tok-a", 2.0, 0.50, 1.0,
    )
    b = All5mLiveTrigger(
        "session-1", "cond-b", "m-b", "ETH:5m",
        "INDEP_PTB_BINANCE_DIRECTIONAL_5M_V2", "DOWN", "tok-b", 2.0, 0.50, 1.0,
    )
    assert ledger.reserve(
        trigger=a, live_limit_price=0.51, collateral_before_usdc=10.0,
        country="SE", region=None,
    )
    assert ledger.reserve(
        trigger=b, live_limit_price=0.51, collateral_before_usdc=9.0,
        country="SE", region=None,
    )
    assert not ledger.reserve(
        trigger=a, live_limit_price=0.51, collateral_before_usdc=8.0,
        country="SE", region=None,
    )
    assert ledger.session_attempts("session-1") == 2

    # Same condition is allowed in a new explicit operator session.
    a2 = All5mLiveTrigger(
        "session-2", "cond-a", "m-a", "BTC:5m",
        "INDEP_PTB_BINANCE_DIRECTIONAL_5M_V2", "UP", "tok-a", 2.0, 0.50, 1.0,
    )
    assert ledger.reserve(
        trigger=a2, live_limit_price=0.51, collateral_before_usdc=8.0,
        country="SE", region=None,
    )


def test_dry_probe_makes_auth_and_eight_book_requests_but_never_posts_order(tmp_path, monkeypatch):
    calls = {"books": 0, "post": 0}

    class Level:
        price = "0.50"
        size = "10"

    class Book:
        asks = [Level()]
        min_order_size = "1"

    class FakeClient:
        def get_ok(self):
            return True

        def get_order_book(self, _token):
            calls["books"] += 1
            return Book()

        def post_orders(self, *_args, **_kwargs):
            calls["post"] += 1
            raise AssertionError("DRY must never call post_orders")

    fake_secrets = SimpleNamespace(
        has_private_key=True,
        funder="0xfunder",
        signature_type=1,
        has_full_clob_creds=True,
    )
    cfg = _cfg(tmp_path, p25_live_feature_enabled=False, p25_live_armed=False, p25_live_arm_nonce="")
    controller = All5mLiveController(
        cfg,
        client_factory=lambda **_kwargs: FakeClient(),
        secret_reader=lambda: fake_secrets,
    )
    monkeypatch.setattr(
        controller, "_geoblock", lambda: {"blocked": False, "country": "SE", "region": None}
    )
    monkeypatch.setattr(controller, "_collateral_balance", lambda _client: 10.0)

    refs = [_Ref(asset) for asset in ("BTC", "ETH", "SOL", "XRP")]
    result = controller.dry_probe(refs)
    assert result["ok"] is True
    assert result["reason"] == "DRY_PASS_NO_ORDER"
    assert result["checks"]["network"]["book_requests_ok"] == 8
    assert result["checks"]["network"]["post_orders_called"] is False
    assert calls == {"books": 8, "post": 0}
    assert result["status"]["dry_ready"] is True

    armed = controller.arm()
    assert armed["ok"] is True
    assert armed["status"]["armed"] is True
    assert armed["status"]["continuous_session"] is True


def test_arm_is_impossible_without_recent_dry_pass(tmp_path):
    cfg = _cfg(tmp_path, p25_live_feature_enabled=False, p25_live_armed=False, p25_live_arm_nonce="")
    controller = All5mLiveController(cfg)
    result = controller.arm()
    assert result["ok"] is False
    assert result["reason"] == "RECENT_DRY_PASS_REQUIRED"
