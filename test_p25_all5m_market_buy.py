from pathlib import Path
from types import SimpleNamespace

from p25_live_all5m import All5mLiveTrigger
from p25_live_all5m_market import All5mMarketBuyController


class _Level:
    def __init__(self, price, size):
        self.price = str(price)
        self.size = str(size)


class _Book:
    def __init__(self, asks, min_order_size="5"):
        self.asks = asks
        self.min_order_size = min_order_size


class _Client:
    def __init__(self, book):
        self.book = book

    def get_order_book(self, _token):
        return self.book


def _cfg(tmp_path: Path):
    return SimpleNamespace(
        p25_live_feature_enabled=True,
        p25_live_armed=True,
        p25_live_arm_nonce="all5m-session-market-1",
        p25_live_strategy_version="INDEP_PTB_BINANCE_DIRECTIONAL_5M_V2",
        paper_strategy_version="INDEP_PTB_BINANCE_DIRECTIONAL_5M_V2",
        p25_live_max_stake_usdc=1.10,
        p25_live_max_price_drift_pct=0.10,
        p25_live_max_limit_price=0.83,
        p25_live_ledger_path=str(tmp_path / "market-live.sqlite"),
        p25_live_clob_host="https://clob.polymarket.com",
        p25_live_chain_id=137,
        p25_live_geoblock_url="https://polymarket.com/api/geoblock",
        p25_live_require_geoblock_clear=True,
        p25_live_horizon="5m",
        p25_live_settlement_wait_sec=0.1,
        p25_live_settlement_poll_sec=0.01,
    )


def _trigger(condition="cond-sol"):
    return All5mLiveTrigger(
        session_nonce="all5m-session-market-1",
        condition_id=condition,
        market_id=f"market-{condition}",
        combo_key="SOL:5m",
        strategy_version="INDEP_PTB_BINANCE_DIRECTIONAL_5M_V2",
        side="DOWN",
        token_id="sol-down",
        requested_shares=1.9802,
        paper_fill_cap=0.505,
        paper_stake_usdc=1.0,
    )


def _controller(tmp_path, monkeypatch, book):
    client = _Client(book)
    controller = All5mMarketBuyController(
        _cfg(tmp_path),
        client_factory=lambda **_kwargs: client,
        secret_reader=lambda: None,
    )
    monkeypatch.setattr(
        controller,
        "_geoblock",
        lambda: {"blocked": False, "country": "SE", "region": None},
    )
    monkeypatch.setattr(controller, "_collateral_balance", lambda _client: 19.10)
    monkeypatch.setattr(
        controller,
        "_conditional_balance",
        lambda _client, _token, refresh: 0.0,
    )
    return controller, client


def test_market_quote_allows_partial_usdc_capacity_and_ignores_share_minimum():
    # Only $0.60 protected liquidity exists, while book metadata says 5-share minimum.
    # FAK is allowed to take the available portion and cancel the rest.
    client = _Client(_Book([_Level("0.50", "1.2")], min_order_size="5"))
    price, shares, capacity = All5mMarketBuyController._fresh_market_quote_for_usdc(
        client,
        token_id="sol-down",
        amount_usdc=1.0,
        max_live_limit_price=0.5555,
    )
    assert price == 0.50
    assert abs(shares - 1.2) < 1e-9
    assert abs(capacity - 0.60) < 1e-9


def test_fak_partial_fill_is_verified_and_session_continues(tmp_path, monkeypatch):
    controller, _client = _controller(
        tmp_path,
        monkeypatch,
        _Book([_Level("0.50", "1.2")], min_order_size="5"),
    )
    posted = {}

    def fake_post(_client, *, token_id, amount_usdc, protected_price):
        posted.update(
            token_id=token_id,
            amount_usdc=amount_usdc,
            protected_price=protected_price,
        )
        return {"success": True, "orderID": "fak-partial-1", "status": "matched"}

    monkeypatch.setattr(controller, "_post_market_buy", fake_post)
    # Full $1 at 50c is ~2 shares. One share is a real, verified partial fill.
    monkeypatch.setattr(controller, "_wait_for_fill_delta", lambda *_a, **_k: 1.0)

    controller._submit_one(_trigger("cond-partial"))

    assert posted == {
        "token_id": "sol-down",
        "amount_usdc": 1.0,
        "protected_price": 0.50,
    }
    status = controller.status()
    assert status["order_mode"] == "MARKET_BUY_FAK_USDC"
    assert status["partial_fill_ok"] is True
    assert status["positive_depth_only"] is True
    assert 0.0 < status["min_fak_depth_usdc"] <= 1e-8
    assert status["armed"] is True
    assert status["halted"] is False
    assert status["last_reason"] == "PARTIAL_FILL_VERIFIED_SOL:5m"
    latest = controller.ledger.latest()
    assert latest is not None
    assert latest["status"] == "PARTIAL_FILL_VERIFIED"
    assert latest["order_id"] == "fak-partial-1"
    assert abs(float(latest["filled_shares"]) - 1.0) < 1e-9


def test_fak_full_fill_is_verified(tmp_path, monkeypatch):
    controller, _client = _controller(
        tmp_path,
        monkeypatch,
        _Book([_Level("0.50", "10")], min_order_size="5"),
    )
    monkeypatch.setattr(
        controller,
        "_post_market_buy",
        lambda *_a, **_k: {"success": True, "orderID": "fak-full", "status": "matched"},
    )
    monkeypatch.setattr(controller, "_wait_for_fill_delta", lambda *_a, **_k: 2.0)

    controller._submit_one(_trigger("cond-full"))
    latest = controller.ledger.latest()
    assert latest is not None
    assert latest["status"] == "FILLED_VERIFIED"
    assert controller.status()["halted"] is False


def test_fak_zero_fill_is_normal_and_session_continues(tmp_path, monkeypatch):
    controller, _client = _controller(
        tmp_path,
        monkeypatch,
        _Book([_Level("0.50", "1.2")], min_order_size="5"),
    )
    monkeypatch.setattr(
        controller,
        "_post_market_buy",
        lambda *_a, **_k: {"success": True, "orderID": "fak-zero", "status": "unmatched"},
    )
    monkeypatch.setattr(controller, "_wait_for_fill_delta", lambda *_a, **_k: 0.0)

    controller._submit_one(_trigger("cond-zero"))
    latest = controller.ledger.latest()
    assert latest is not None
    assert latest["status"] == "NO_FILL_VERIFIED"
    assert controller.status()["armed"] is True
    assert controller.status()["halted"] is False


def test_fak_submits_even_with_one_cent_of_positive_protected_depth(tmp_path, monkeypatch):
    # Only $0.01 of allowed liquidity. "Fill as much as possible" means this must
    # still reach the authenticated FAK submit path instead of being locally rejected.
    controller, _client = _controller(
        tmp_path,
        monkeypatch,
        _Book([_Level("0.50", "0.02")], min_order_size="5"),
    )
    posted = {"count": 0}

    def fake_post(_client, *, token_id, amount_usdc, protected_price):
        posted["count"] += 1
        assert token_id == "sol-down"
        assert amount_usdc == 1.0
        assert protected_price == 0.50
        return {"success": True, "orderID": "fak-one-cent", "status": "matched"}

    monkeypatch.setattr(controller, "_post_market_buy", fake_post)
    monkeypatch.setattr(controller, "_wait_for_fill_delta", lambda *_a, **_k: 0.02)
    controller._submit_one(_trigger("cond-one-cent"))

    assert posted["count"] == 1
    latest = controller.ledger.latest()
    assert latest is not None
    assert latest["status"] == "PARTIAL_FILL_VERIFIED"
    assert controller.status()["halted"] is False


def test_fak_does_not_submit_when_protected_depth_is_zero(tmp_path, monkeypatch):
    controller, _client = _controller(
        tmp_path,
        monkeypatch,
        _Book([], min_order_size="5"),
    )
    posted = {"count": 0}

    def fake_post(*_args, **_kwargs):
        posted["count"] += 1
        raise AssertionError("must not post when there is no protected ask liquidity")

    monkeypatch.setattr(controller, "_post_market_buy", fake_post)
    controller._submit_one(_trigger("cond-zero-depth"))

    assert posted["count"] == 0
    assert controller.status()["last_reason"] == "FRESH_DEPTH_ZERO_OR_MOVED_SOL:5m"


def test_market_buy_source_uses_fak_not_fok():
    text = Path("p25_live_all5m_market.py").read_text(encoding="utf-8")
    assert "OrderType.FAK" in text
    assert "MARKET_BUY_FAK_USDC" in text
    assert "PARTIAL_FILL_VERIFIED" in text
