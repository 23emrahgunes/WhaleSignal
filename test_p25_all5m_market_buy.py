from pathlib import Path
from types import SimpleNamespace

from p25_live_all5m import All5mLiveTrigger
from p25_live_all5m_market import (
    All5mMarketBuyController,
    _exception_order_id,
    _is_authoritative_fok_no_fill,
)


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


def _prepared_controller(tmp_path, monkeypatch, *, conditional_after=0.0):
    book = _Book([_Level("0.50", "10")], min_order_size="5")
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
    values = iter([0.0, conditional_after])
    monkeypatch.setattr(
        controller,
        "_conditional_balance",
        lambda _client, _token, refresh: next(values),
    )
    return controller, client


def test_market_quote_is_usdc_based_and_does_not_apply_share_minimum():
    client = _Client(_Book([_Level("0.50", "10")], min_order_size="5"))
    price, shares, capacity = All5mMarketBuyController._fresh_market_quote_for_usdc(
        client,
        token_id="sol-down",
        amount_usdc=1.0,
        max_live_limit_price=0.5555,
    )
    assert price == 0.50
    assert abs(shares - 2.0) < 1e-9
    assert abs(capacity - 5.0) < 1e-9


def test_submit_uses_exact_one_usdc_market_buy_even_below_book_share_minimum(tmp_path, monkeypatch):
    book = _Book([_Level("0.50", "10")], min_order_size="5")
    client = _Client(book)
    cfg = _cfg(tmp_path)
    controller = All5mMarketBuyController(
        cfg,
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

    posted = {}

    def fake_post(_client, *, token_id, amount_usdc, protected_price):
        posted.update(
            token_id=token_id,
            amount_usdc=amount_usdc,
            protected_price=protected_price,
        )
        return {"success": True, "orderID": "market-order-1", "status": "matched"}

    monkeypatch.setattr(controller, "_post_market_buy", fake_post)
    monkeypatch.setattr(
        controller,
        "_wait_for_fill_delta",
        lambda *_args, **_kwargs: 2.0,
    )

    controller._submit_one(_trigger())

    assert posted["token_id"] == "sol-down"
    assert posted["amount_usdc"] == 1.0
    assert posted["protected_price"] == 0.50
    assert controller.status()["last_reason"] == "FILLED_VERIFIED_SOL:5m"
    latest = controller.ledger.latest()
    assert latest is not None
    assert latest["status"] == "FILLED_VERIFIED"
    assert latest["order_id"] == "market-order-1"
    assert abs(float(latest["filled_shares"]) - 2.0) < 1e-9


def test_market_buy_still_fails_closed_when_one_dollar_cannot_fill_under_price_cap(tmp_path, monkeypatch):
    client = _Client(_Book([_Level("0.50", "0.4")], min_order_size="5"))
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
    posted = {"count": 0}

    def fake_post(*_args, **_kwargs):
        posted["count"] += 1
        raise AssertionError("must not post without $1 depth")

    monkeypatch.setattr(controller, "_post_market_buy", fake_post)
    controller._submit_one(_trigger())

    assert posted["count"] == 0
    assert controller.status()["last_reason"] == "FRESH_DEPTH_OR_PRICE_MOVED_SOL:5m"


def test_authoritative_fok_kill_is_no_fill_and_does_not_halt_session(tmp_path, monkeypatch):
    controller, _client = _prepared_controller(tmp_path, monkeypatch, conditional_after=0.0)
    message = (
        "PolyApiException[status_code=400, error_message={'error': "
        "\"order couldn't be fully filled. FOK orders are fully filled or killed.\", "
        "'orderID': '0xdeadbeef'}]"
    )
    exc = RuntimeError(message)
    assert _is_authoritative_fok_no_fill(exc) is True
    assert _exception_order_id(exc) == "0xdeadbeef"

    monkeypatch.setattr(controller, "_post_market_buy", lambda *_a, **_k: (_ for _ in ()).throw(exc))
    controller._submit_one(_trigger("cond-fok-kill"))

    status = controller.status()
    assert status["halted"] is False
    assert status["armed"] is True
    assert status["last_reason"] == "NO_FILL_FOK_KILLED_SOL:5m"
    latest = controller.ledger.latest()
    assert latest is not None
    assert latest["status"] == "NO_FILL_FOK_KILLED"
    assert latest["order_id"] == "0xdeadbeef"
    assert float(latest["filled_shares"]) == 0.0
    assert "AUTHORITATIVE_FOK_KILLED_NO_FILL" in str(latest["response_json"])


def test_fok_kill_with_unexpected_balance_delta_still_halts(tmp_path, monkeypatch):
    controller, _client = _prepared_controller(tmp_path, monkeypatch, conditional_after=0.25)
    exc = RuntimeError(
        "PolyApiException[status_code=400, error_message={'error': "
        "\"order couldn't be fully filled. FOK orders are fully filled or killed.\", "
        "'orderID': '0xambiguous'}]"
    )
    monkeypatch.setattr(controller, "_post_market_buy", lambda *_a, **_k: (_ for _ in ()).throw(exc))
    controller._submit_one(_trigger("cond-fok-ambiguous"))

    status = controller.status()
    assert status["halted"] is True
    assert status["armed"] is False
    latest = controller.ledger.latest()
    assert latest is not None
    assert latest["status"] == "EXPOSURE_UNCERTAIN_HALT"
    assert float(latest["filled_shares"]) == 0.25
