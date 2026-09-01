import threading
import time
from pathlib import Path
from types import SimpleNamespace

from p25_live_all5m import All5mLiveTrigger
from p25_live_all5m_market import (
    All5mMarketBuyController,
    _exception_order_id,
    _is_authoritative_fak_terminal,
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


class _Value:
    def __init__(self, value):
        self.value = value


class _Combo:
    def __init__(self, asset="BTC", horizon="5m"):
        self.asset = _Value(asset)
        self.horizon = _Value(horizon)
        self.key = f"{asset}:{horizon}"


class _Ref:
    def __init__(self, asset="BTC", horizon="5m"):
        self.combo = _Combo(asset, horizon)
        self.condition_id = f"cond-{asset.lower()}"
        self.market_id = f"market-{asset.lower()}"
        self.up_token_id = f"{asset.lower()}-up"
        self.down_token_id = f"{asset.lower()}-down"


def _cfg(tmp_path: Path):
    return SimpleNamespace(
        p25_live_feature_enabled=True,
        p25_live_armed=True,
        p25_live_arm_nonce="all5m-session-market-1",
        p25_live_strategy_version="INDEP_PTB_BINANCE_DIRECTIONAL_5M_V2",
        paper_strategy_version="INDEP_PTB_BINANCE_DIRECTIONAL_5M_V2",
        paper_min_edge=0.08,
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


def _trigger(condition="cond-sol", *, combo="SOL:5m", fill=0.505):
    asset = combo.split(":", 1)[0].lower()
    return All5mLiveTrigger(
        session_nonce="all5m-session-market-1",
        condition_id=condition,
        market_id=f"market-{condition}",
        combo_key=combo,
        strategy_version="INDEP_PTB_BINANCE_DIRECTIONAL_5M_V2",
        side="DOWN",
        token_id=f"{asset}-down",
        requested_shares=1.9802,
        paper_fill_cap=fill,
        paper_stake_usdc=1.0,
    )


def _paper(asset="BTC", *, probability=0.90, fill=0.50):
    return {
        "status": "OPEN",
        "strategy_version": "INDEP_PTB_BINANCE_DIRECTIONAL_5M_V2",
        "side": "UP",
        "shares": 2.0,
        "fill_price": fill,
        "stake_usdc": 1.0,
        "selected_probability": probability,
        "forecast_p_up": probability,
        "forecast_edge": probability - fill,
        "asset": asset,
    }


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
    monkeypatch.setattr(controller, "_wait_for_fill_delta", lambda *_a, **_k: 1.0)

    # p=58%, live edge floor=8pt -> exact 50c marketable cap.
    controller._submit_one(_trigger("cond-partial"), selected_probability=0.58)

    assert posted == {
        "token_id": "sol-down",
        "amount_usdc": 1.0,
        "protected_price": 0.50,
    }
    status = controller.status()
    assert status["order_mode"] == "MARKETABLE_FAK_LIVE_EDGE_CAP"
    assert status["execution_price_mode"] == "CURRENT_BOOK_WITH_LIVE_EDGE_CAP"
    assert status["paper_drift_enforced"] is False
    assert status["live_min_edge"] == 0.08
    assert status["parallel_execution"] is True
    assert status["partial_fill_ok"] is True
    assert status["positive_depth_only"] is True
    assert status["fak_no_match_is_normal"] is True
    assert 0.0 < status["min_fak_depth_usdc"] <= 1e-8
    assert status["armed"] is True
    assert status["halted"] is False
    assert status["last_reason"] == "PARTIAL_FILL_VERIFIED_SOL:5m"
    latest = controller.ledger.latest()
    assert latest is not None
    assert latest["status"] == "PARTIAL_FILL_VERIFIED"
    assert latest["order_id"] == "fak-partial-1"
    assert abs(float(latest["filled_shares"]) - 1.0) < 1e-9


def test_live_edge_cap_follows_current_book_instead_of_old_paper_drift(tmp_path, monkeypatch):
    # Old logic: paper 30c * 1.10 = 33c, so a live 60c ask was skipped.
    # New logic: p=95%, edge floor=8pt, hard cap=83c -> marketable FAK cap is 83c.
    controller, _client = _controller(
        tmp_path,
        monkeypatch,
        _Book([_Level("0.60", "10")], min_order_size="5"),
    )
    posted = {}

    def fake_post(_client, *, token_id, amount_usdc, protected_price):
        posted.update(
            token_id=token_id,
            amount_usdc=amount_usdc,
            protected_price=protected_price,
        )
        return {"success": True, "orderID": "edge-cap-follow", "status": "matched"}

    monkeypatch.setattr(controller, "_post_market_buy", fake_post)
    monkeypatch.setattr(controller, "_wait_for_fill_delta", lambda *_a, **_k: 2.0)
    controller._submit_one(
        _trigger("cond-live-edge", fill=0.30),
        selected_probability=0.95,
    )

    assert posted["amount_usdc"] == 1.0
    assert posted["protected_price"] == 0.83
    latest = controller.ledger.latest()
    assert latest is not None
    assert float(latest["live_limit_price"]) == 0.83
    assert latest["status"] == "FILLED_VERIFIED"


def test_live_edge_cap_refuses_price_that_erases_forecast_edge(tmp_path, monkeypatch):
    # p=70%, min live edge=8pt -> cap is 62c. A 63c ask must not be chased.
    controller, _client = _controller(
        tmp_path,
        monkeypatch,
        _Book([_Level("0.63", "10")], min_order_size="5"),
    )
    posted = {"count": 0}

    def fake_post(*_a, **_k):
        posted["count"] += 1
        raise AssertionError("must not submit above live edge cap")

    monkeypatch.setattr(controller, "_post_market_buy", fake_post)
    controller._submit_one(
        _trigger("cond-edge-erased", fill=0.30),
        selected_probability=0.70,
    )

    assert posted["count"] == 0
    assert controller.status()["last_reason"] == "LIVE_EDGE_NO_EXECUTABLE_ASK_SOL:5m"


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

    controller._submit_one(_trigger("cond-full"), selected_probability=0.58)
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

    controller._submit_one(_trigger("cond-zero"), selected_probability=0.58)
    latest = controller.ledger.latest()
    assert latest is not None
    assert latest["status"] == "NO_FILL_VERIFIED"
    assert controller.status()["armed"] is True
    assert controller.status()["halted"] is False


def test_authoritative_fak_no_match_is_no_fill_and_live_continues(tmp_path, monkeypatch):
    controller, _client = _controller(
        tmp_path,
        monkeypatch,
        _Book([_Level("0.50", "1.2")], min_order_size="5"),
    )
    exc = RuntimeError(
        "PolyApiException[status_code=400, error_message={'error': "
        "'no orders found to match with FAK order. FAK orders are partially filled or killed if no match is found.', "
        "'orderID': '0xfakdead'}]"
    )
    assert _is_authoritative_fak_terminal(exc) is True
    assert _exception_order_id(exc) == "0xfakdead"
    monkeypatch.setattr(controller, "_post_market_buy", lambda *_a, **_k: (_ for _ in ()).throw(exc))
    monkeypatch.setattr(controller, "_wait_for_fill_delta", lambda *_a, **_k: 0.0)

    controller._submit_one(_trigger("cond-fak-no-match"), selected_probability=0.58)

    status = controller.status()
    assert status["armed"] is True
    assert status["halted"] is False
    assert status["last_reason"] == "NO_FILL_FAK_KILLED_SOL:5m"
    latest = controller.ledger.latest()
    assert latest is not None
    assert latest["status"] == "NO_FILL_FAK_KILLED"
    assert latest["order_id"] == "0xfakdead"
    assert float(latest["filled_shares"]) == 0.0
    assert "AUTHORITATIVE_FAK_TERMINAL" in str(latest["response_json"])


def test_authoritative_fak_terminal_with_balance_delta_is_partial_fill(tmp_path, monkeypatch):
    controller, _client = _controller(
        tmp_path,
        monkeypatch,
        _Book([_Level("0.50", "1.2")], min_order_size="5"),
    )
    exc = RuntimeError(
        "PolyApiException[status_code=400, error_message={'error': "
        "'no orders found to match with FAK order. FAK orders are partially filled or killed if no match is found.', "
        "'orderID': '0xfakpartial'}]"
    )
    monkeypatch.setattr(controller, "_post_market_buy", lambda *_a, **_k: (_ for _ in ()).throw(exc))
    monkeypatch.setattr(controller, "_wait_for_fill_delta", lambda *_a, **_k: 0.25)

    controller._submit_one(_trigger("cond-fak-partial-terminal"), selected_probability=0.58)

    status = controller.status()
    assert status["armed"] is True
    assert status["halted"] is False
    assert status["last_reason"] == "PARTIAL_FILL_VERIFIED_SOL:5m"
    latest = controller.ledger.latest()
    assert latest is not None
    assert latest["status"] == "PARTIAL_FILL_VERIFIED"
    assert latest["order_id"] == "0xfakpartial"
    assert abs(float(latest["filled_shares"]) - 0.25) < 1e-9


def test_fak_submits_even_with_one_cent_of_positive_protected_depth(tmp_path, monkeypatch):
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
    controller._submit_one(_trigger("cond-one-cent"), selected_probability=0.58)

    assert posted["count"] == 1
    latest = controller.ledger.latest()
    assert latest is not None
    assert latest["status"] == "PARTIAL_FILL_VERIFIED"
    assert controller.status()["halted"] is False


def test_fak_does_not_submit_when_live_edge_depth_is_zero(tmp_path, monkeypatch):
    controller, _client = _controller(
        tmp_path,
        monkeypatch,
        _Book([], min_order_size="5"),
    )
    posted = {"count": 0}

    def fake_post(*_args, **_kwargs):
        posted["count"] += 1
        raise AssertionError("must not post when there is no edge-protected ask liquidity")

    monkeypatch.setattr(controller, "_post_market_buy", fake_post)
    controller._submit_one(_trigger("cond-zero-depth"), selected_probability=0.58)

    assert posted["count"] == 0
    assert controller.status()["last_reason"] == "LIVE_EDGE_NO_EXECUTABLE_ASK_SOL:5m"


def test_submit_async_runs_different_assets_in_parallel_not_one_global_queue(tmp_path, monkeypatch):
    controller, _client = _controller(
        tmp_path,
        monkeypatch,
        _Book([_Level("0.50", "10")], min_order_size="5"),
    )
    release = threading.Event()
    both_started = threading.Event()
    started: list[str] = []
    lock = threading.Lock()

    def fake_submit(trigger, *, selected_probability=None):
        assert selected_probability == 0.90
        with lock:
            started.append(trigger.combo_key)
            if len(started) == 2:
                both_started.set()
        release.wait(timeout=1.0)

    monkeypatch.setattr(controller, "_submit_one", fake_submit)

    assert controller.submit_async(_Ref("BTC"), _paper("BTC")) is True
    assert controller.submit_async(_Ref("ETH"), _paper("ETH")) is True
    assert both_started.wait(timeout=0.5) is True

    status = controller.status()
    assert status["parallel_execution"] is True
    assert status["parallel_workers"] == 2
    assert set(status["active_combos"]) == {"BTC:5m", "ETH:5m"}
    assert status["queue_length"] == 0

    release.set()
    deadline = time.time() + 1.0
    while time.time() < deadline and controller.status()["parallel_workers"]:
        time.sleep(0.01)
    assert controller.status()["parallel_workers"] == 0


def test_market_buy_source_uses_live_edge_fak_not_paper_drift_or_fok():
    text = Path("p25_live_all5m_market.py").read_text(encoding="utf-8")
    assert "OrderType.FAK" in text
    assert "MARKETABLE_FAK_LIVE_EDGE_CAP" in text
    assert "paper_drift_enforced\": False" in text
    assert "PARTIAL_FILL_VERIFIED" in text
    assert "NO_FILL_FAK_KILLED" in text
    assert "paper_fill_cap) * (1.0 + drift)" not in text
