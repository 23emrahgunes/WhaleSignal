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


class _Client:
    pass


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


def _controller(tmp_path, monkeypatch):
    client = _Client()
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


def test_signal_immediate_fak_partial_fill_is_verified_and_session_continues(tmp_path, monkeypatch):
    controller, _client = _controller(tmp_path, monkeypatch)
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

    # p=58%, live edge floor=8pt -> exact 50c FAK limit.
    controller._submit_one(_trigger("cond-partial"), selected_probability=0.58)

    assert posted == {
        "token_id": "sol-down",
        "amount_usdc": 1.0,
        "protected_price": 0.50,
    }
    status = controller.status()
    assert status["order_mode"] == "SIGNAL_IMMEDIATE_FAK_LIVE_EDGE_CAP"
    assert status["execution_price_mode"] == "SIGNAL_IMMEDIATE_LIMIT_CAP"
    assert status["paper_drift_enforced"] is False
    assert status["live_min_edge"] == 0.08
    assert status["parallel_execution"] is True
    assert status["pre_submit_book_check"] is False
    assert status["matching_engine_is_liquidity_gate"] is True
    assert status["partial_fill_ok"] is True
    assert status["fak_no_match_is_normal"] is True
    assert status["armed"] is True
    assert status["halted"] is False
    assert status["last_reason"] == "PARTIAL_FILL_VERIFIED_SOL:5m"
    latest = controller.ledger.latest()
    assert latest is not None
    assert latest["status"] == "PARTIAL_FILL_VERIFIED"
    assert latest["order_id"] == "fak-partial-1"
    assert abs(float(latest["filled_shares"]) - 1.0) < 1e-9


def test_live_edge_cap_ignores_old_paper_drift_and_posts_immediately(tmp_path, monkeypatch):
    # Old logic: paper 30c * 1.10 = 33c. New logic: p=95%, edge floor=8pt,
    # hard cap=83c -> immediately submit a FAK with 83c price protection.
    controller, _client = _controller(tmp_path, monkeypatch)
    posted = {}

    def fake_post(_client, *, token_id, amount_usdc, protected_price):
        posted.update(token_id=token_id, amount_usdc=amount_usdc, protected_price=protected_price)
        return {"success": True, "orderID": "edge-cap-follow", "status": "matched"}

    monkeypatch.setattr(controller, "_post_market_buy", fake_post)
    monkeypatch.setattr(controller, "_wait_for_fill_delta", lambda *_a, **_k: 2.0)
    controller._submit_one(
        _trigger("cond-live-edge", fill=0.30),
        selected_probability=0.95,
    )

    assert posted == {
        "token_id": "sol-down",
        "amount_usdc": 1.0,
        "protected_price": 0.83,
    }
    latest = controller.ledger.latest()
    assert latest is not None
    assert float(latest["live_limit_price"]) == 0.83
    assert latest["status"] == "FILLED_VERIFIED"


def test_live_edge_price_cap_is_sent_to_matching_engine_not_checked_with_local_book(tmp_path, monkeypatch):
    # p=70%, edge floor=8pt -> 62c limit. We submit immediately at 62c. If the
    # real best ask is 63c, the CLOB itself atomically kills the FAK as no-match.
    controller, _client = _controller(tmp_path, monkeypatch)
    posted = {}
    exc = RuntimeError(
        "PolyApiException[status_code=400, error_message={'error': "
        "'no orders found to match with FAK order. FAK orders are partially filled or killed if no match is found.', "
        "'orderID': '0xedgecap'}]"
    )

    def fake_post(_client, *, token_id, amount_usdc, protected_price):
        posted.update(token_id=token_id, amount_usdc=amount_usdc, protected_price=protected_price)
        raise exc

    monkeypatch.setattr(controller, "_post_market_buy", fake_post)
    monkeypatch.setattr(controller, "_wait_for_fill_delta", lambda *_a, **_k: 0.0)
    controller._submit_one(
        _trigger("cond-edge-erased", fill=0.30),
        selected_probability=0.70,
    )

    assert posted["protected_price"] == 0.62
    latest = controller.ledger.latest()
    assert latest is not None
    assert latest["status"] == "NO_FILL_FAK_KILLED"
    assert latest["order_id"] == "0xedgecap"
    assert controller.status()["halted"] is False


def test_full_fill_is_verified(tmp_path, monkeypatch):
    controller, _client = _controller(tmp_path, monkeypatch)
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


def test_zero_fill_return_payload_is_normal_and_session_continues(tmp_path, monkeypatch):
    controller, _client = _controller(tmp_path, monkeypatch)
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
    controller, _client = _controller(tmp_path, monkeypatch)
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
    controller, _client = _controller(tmp_path, monkeypatch)
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


def test_no_local_book_precheck_even_when_matching_engine_has_no_liquidity(tmp_path, monkeypatch):
    controller, _client = _controller(tmp_path, monkeypatch)
    posted = {"count": 0}
    exc = RuntimeError(
        "PolyApiException[status_code=400, error_message={'error': "
        "'no orders found to match with FAK order. FAK orders are partially filled or killed if no match is found.', "
        "'orderID': '0xempty'}]"
    )

    def fake_post(*_args, **_kwargs):
        posted["count"] += 1
        raise exc

    monkeypatch.setattr(controller, "_post_market_buy", fake_post)
    monkeypatch.setattr(controller, "_wait_for_fill_delta", lambda *_a, **_k: 0.0)
    controller._submit_one(_trigger("cond-no-book-precheck"), selected_probability=0.58)

    assert posted["count"] == 1
    latest = controller.ledger.latest()
    assert latest is not None
    assert latest["status"] == "NO_FILL_FAK_KILLED"
    assert controller.status()["pre_submit_book_check"] is False


def test_submit_async_runs_different_assets_in_parallel_not_one_global_queue(tmp_path, monkeypatch):
    controller, _client = _controller(tmp_path, monkeypatch)
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


def test_market_buy_source_is_signal_immediate_fak_and_has_no_paper_drift_or_book_precheck():
    text = Path("p25_live_all5m_market.py").read_text(encoding="utf-8")
    assert "OrderType.FAK" in text
    assert "SIGNAL_IMMEDIATE_FAK_LIVE_EDGE_CAP" in text
    assert '"paper_drift_enforced": False' in text
    assert '"pre_submit_book_check": False' in text
    assert "PARTIAL_FILL_VERIFIED" in text
    assert "NO_FILL_FAK_KILLED" in text
    assert "paper_fill_cap) * (1.0 + drift)" not in text
    assert "get_order_book" not in text
