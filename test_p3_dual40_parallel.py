from __future__ import annotations

import json
import sqlite3
import time

import pytest

from p3_config import DUAL40_MODE, P3Settings
from p3_dual40_core import RegimeDecision
from p3_dual40_engine import Dual40MakerEngine
from p3_dual40_store import (
    active_cycle,
    active_cycles,
    connect_dual40,
    create_cycle,
    ladder_state,
    market_decisions,
    set_ladder_state,
)
from p3_live_state import LiveState


ASSETS = ("BTC", "ETH", "SOL", "XRP")


def _settings(tmp_path, **overrides) -> P3Settings:
    values = {
        "strategy_mode": DUAL40_MODE,
        "p26_db_path": str(tmp_path / "p26.sqlite"),
        "p3_db_path": str(tmp_path / "p3.sqlite"),
        "reports_dir": str(tmp_path / "reports"),
        "max_capital_per_cycle_usdc": 30.0,
        "max_quantity_shares": 30.0,
        "dual40_confirm_sec": 1.0,
        "dual40_market_age_sec": 20.0,
        "dual40_lookback_sec": 1.0,
        "dual40_min_tte_sec": 60.0,
        "dual40_cancel_tte_sec": 30.0,
        "dual40_opening_gate_mode": "OFF",
        "dual40_forecast_gate_mode": "OFF",
        "dual40_global_risk_mode": "OFF",
        "live_feature_enabled": False,
        "live_auto_execute_enabled": False,
    }
    values.update(overrides)
    settings = P3Settings(_env_file=None, **values)
    settings.validate_research_safety()
    return settings


def _seed_p26(
    path: str,
    assets=ASSETS,
    *,
    now_ms: int | None = None,
    up_ask: float = 0.52,
    down_ask: float = 0.52,
) -> None:
    now = int(now_ms or time.time() * 1000)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(
            """
            CREATE TABLE p26_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE p26_market_tokens(
                condition_id TEXT,
                combo_key TEXT,
                market_end_ts_ms INTEGER,
                side TEXT,
                token_id TEXT,
                active INTEGER
            );
            CREATE TABLE p26_clob_books(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                condition_id TEXT,
                token_id TEXT,
                side TEXT,
                source_ts_ms INTEGER,
                recv_ts_ms INTEGER,
                inserted_at_ms INTEGER,
                bids_json TEXT,
                asks_json TEXT
            );
            CREATE TABLE p26_fee_schedules(
                condition_id TEXT,
                token_id TEXT,
                taker_only INTEGER,
                source_ts_ms INTEGER
            );
            CREATE TABLE p26_labels(
                condition_id TEXT PRIMARY KEY,
                official_label INTEGER,
                official_result_source TEXT,
                official_resolved_at_ms INTEGER
            );
            """
        )
        conn.execute(
            "INSERT INTO p26_meta(key,value) VALUES(?,?)",
            (
                "book_transport_status_json",
                json.dumps({"connected": True, "last_receive_ms": now}),
            ),
        )
        for index, asset in enumerate(assets):
            condition = f"cond-{asset.lower()}"
            end_ms = now + 180_000 + index
            for side in ("UP", "DOWN"):
                token = f"{asset.lower()}-{side.lower()}"
                ask = up_ask if side == "UP" else down_ask
                bids = json.dumps(
                    [[0.48, 10.0], [0.40, 2.0]]
                    if ask > 0.50
                    else [[max(0.01, round(ask - 0.005, 4)), 10.0]]
                )
                asks = json.dumps([[ask, 20.0]])
                conn.execute(
                    """
                    INSERT INTO p26_market_tokens
                    VALUES(?,?,?,?,?,1)
                    """,
                    (condition, f"{asset}:5m", end_ms, side, token),
                )
                conn.execute(
                    """
                    INSERT INTO p26_fee_schedules
                    VALUES(?,?,1,?)
                    """,
                    (condition, token, now),
                )
                for offset in range(0, 14_000, 250):
                    ts = now - 12_000 + offset
                    conn.execute(
                        """
                        INSERT INTO p26_clob_books(
                            condition_id,token_id,side,source_ts_ms,recv_ts_ms,
                            inserted_at_ms,bids_json,asks_json
                        ) VALUES(?,?,?,?,?,?,?,?)
                        """,
                        (condition, token, side, ts, ts, ts, bids, asks),
                    )
        conn.commit()
    finally:
        conn.close()


def _engine(tmp_path, **settings_overrides) -> Dual40MakerEngine:
    settings = _settings(tmp_path, **settings_overrides)
    _seed_p26(settings.p26_db_path)
    state = LiveState(
        live_feature_enabled=settings.live_feature_enabled,
        auto_execute_enabled=settings.live_auto_execute_enabled,
    )
    engine = Dual40MakerEngine(settings, state, gateway_factory=lambda _: _FakeGateway())
    now = time.monotonic()
    for asset in ASSETS:
        engine._gate_since[f"PAPER:{asset}:cond-{asset.lower()}"] = now - 2.0
        engine._gate_since[f"LIVE:{asset}:cond-{asset.lower()}"] = now - 2.0
    return engine


class _FakeGateway:
    posted: list[dict] = []

    def collateral_balance_usdc(self, *, refresh=True):
        return 100.0

    def pair_balances(self, **kwargs):
        return {"up": 0.0, "down": 0.0}

    def post_pair_post_only_gtc(self, **kwargs):
        self.posted.append(kwargs)
        return {
            "ok": True,
            "up_order_id": f"up-{len(self.posted)}",
            "down_order_id": f"down-{len(self.posted)}",
            "heartbeat_id": "hb",
            "submitted_at_ms": int(time.time() * 1000),
        }


def test_paper_opens_four_eligible_assets_in_same_tick(tmp_path):
    engine = _engine(tmp_path)
    result = engine.tick()

    assert result["status"] == "MULTI_ASSET_TICK"
    conn = connect_dual40(engine.settings.p3_db_path)
    try:
        cycles = active_cycles(conn, scope="PAPER")
        assert len(cycles) == 4
        assert {cycle["asset"] for cycle in cycles} == set(ASSETS)
    finally:
        conn.close()


def test_active_sol_cycle_does_not_block_other_assets(tmp_path):
    engine = _engine(tmp_path)
    conn = connect_dual40(engine.settings.p3_db_path)
    try:
        create_cycle(
            conn,
            scope="PAPER",
            asset="SOL",
            session_id=None,
            condition_id="existing-sol",
            combo_key="SOL:5m",
            market_end_ts_ms=int(time.time() * 1000) + 180_000,
            level_index=0,
            target_shares=5.0,
            maker_price=0.40,
            status="PAPER_RESTING",
            gate={},
            up_token_id="sol-up-old",
            down_token_id="sol-down-old",
            loss_pool_before_usdc=0.0,
        )
    finally:
        conn.close()

    engine.tick()
    conn = connect_dual40(engine.settings.p3_db_path)
    try:
        assert {cycle["asset"] for cycle in active_cycles(conn, scope="PAPER")} == set(ASSETS)
    finally:
        conn.close()


def test_recovery_state_is_asset_scoped(tmp_path):
    engine = _engine(tmp_path)
    conn = connect_dual40(engine.settings.p3_db_path)
    try:
        create_cycle(
            conn,
            scope="PAPER",
            asset="ETH",
            session_id=None,
            condition_id="eth-loss",
            combo_key="ETH:5m",
            market_end_ts_ms=1,
            level_index=0,
            target_shares=5.0,
            maker_price=0.40,
            status="WAIT_RESOLUTION",
            gate={
                "opening_gate": {"eligible": True, "reason": "OPENING_STABLE_TWO_WAY"},
                "stages": [
                    {"stage": "WOULD_OPEN", "eligible": True},
                    {"stage": "OPENED", "eligible": True},
                ],
            },
            up_token_id="eth-up",
            down_token_id="eth-down",
            loss_pool_before_usdc=0.0,
        )
        cycle = active_cycle(conn, scope="PAPER", asset="ETH")
        assert cycle is not None
        engine._apply_ladder_and_finalize(
            conn,
            cycle=cycle,
            status="RESOLVED_DOWN",
            pnl=-2.0,
            official_result="DOWN",
        )
        assert ladder_state(conn, "PAPER", "ETH")["level_index"] == 1
        decision = next(
            item
            for item in market_decisions(conn, scope="PAPER")
            if item["condition_id"] == "eth-loss"
        )
        assert decision["decision"] == "SETTLED"
        assert decision["final_gate"]["opening_gate"]["eligible"] is True
        assert [stage["stage"] for stage in decision["final_gate"]["stages"]][-2:] == [
            "OPENED",
            "SETTLED",
        ]
        for asset in ("BTC", "SOL", "XRP"):
            assert ladder_state(conn, "PAPER", asset)["level_index"] == 0
            assert ladder_state(conn, "PAPER", asset)["loss_pool_usdc"] == 0.0
    finally:
        conn.close()


def test_paper_hard_stop_resets_to_base_and_reopens_next_series(tmp_path):
    engine = _engine(tmp_path)
    conn = connect_dual40(engine.settings.p3_db_path)
    try:
        set_ladder_state(
            conn,
            scope="PAPER",
            asset="BTC",
            level_index=2,
            loss_pool_usdc=18.0,
            hard_stopped=True,
            hard_stop_reason="HARD_STOP_MAX_30_CANNOT_RECOVER",
        )
    finally:
        conn.close()

    engine.tick()
    conn = connect_dual40(engine.settings.p3_db_path)
    try:
        active_assets = {cycle["asset"] for cycle in active_cycles(conn, scope="PAPER")}
        assert active_assets == set(ASSETS)
        btc_state = ladder_state(conn, "PAPER", "BTC")
        assert btc_state["hard_stopped"] == 0
        assert btc_state["level_index"] == 0
        assert btc_state["loss_pool_usdc"] == 0.0
        btc_cycle = active_cycle(conn, scope="PAPER", asset="BTC")
        assert btc_cycle is not None
        assert btc_cycle["target_shares"] == 5.0
    finally:
        conn.close()


def test_live_hard_stop_remains_locked(tmp_path):
    settings = _settings(
        tmp_path,
        live_feature_enabled=True,
        live_auto_execute_enabled=True,
        web_auth_required=True,
        web_password="very-safe-test-password",
    )
    _seed_p26(settings.p26_db_path)
    state = LiveState(live_feature_enabled=True, auto_execute_enabled=True)
    state.arm({"ok": True, "checked_at_ms": int(time.time() * 1000)})
    gateway = _FakeGateway()
    gateway.posted = []
    engine = Dual40MakerEngine(settings, state, gateway_factory=lambda _: gateway)
    engine._fresh_preflight = lambda: True
    now = time.monotonic()
    for asset in ASSETS:
        engine._gate_since[f"LIVE:{asset}:cond-{asset.lower()}"] = now - 2.0
    conn = connect_dual40(settings.p3_db_path)
    try:
        set_ladder_state(
            conn,
            scope="LIVE",
            asset="BTC",
            level_index=2,
            loss_pool_usdc=18.0,
            hard_stopped=True,
            hard_stop_reason="HARD_STOP_MAX_30_CANNOT_RECOVER",
        )
    finally:
        conn.close()

    engine.tick()
    conn = connect_dual40(settings.p3_db_path)
    try:
        assert active_cycle(conn, scope="LIVE", asset="BTC") is None
        assert ladder_state(conn, "LIVE", "BTC")["hard_stopped"] == 1
        assert len(gateway.posted) == 1
        assert gateway.posted[0]["up_token_id"] != "btc-up"
    finally:
        conn.close()


def test_no_duplicate_active_cycle_per_asset(tmp_path):
    settings = _settings(tmp_path)
    conn = connect_dual40(settings.p3_db_path)
    try:
        kwargs = dict(
            scope="PAPER",
            asset="BTC",
            session_id=None,
            condition_id="btc-one",
            combo_key="BTC:5m",
            market_end_ts_ms=1,
            level_index=0,
            target_shares=5.0,
            maker_price=0.40,
            status="PAPER_RESTING",
            gate={},
            up_token_id="up",
            down_token_id="down",
            loss_pool_before_usdc=0.0,
        )
        create_cycle(conn, **kwargs)
        with pytest.raises(sqlite3.IntegrityError):
            create_cycle(conn, **{**kwargs, "condition_id": "btc-two"})
    finally:
        conn.close()


def test_different_assets_can_have_active_cycles(tmp_path):
    settings = _settings(tmp_path)
    conn = connect_dual40(settings.p3_db_path)
    try:
        for asset in ("BTC", "ETH"):
            create_cycle(
                conn,
                scope="PAPER",
                asset=asset,
                session_id=None,
                condition_id=f"{asset}-cond",
                combo_key=f"{asset}:5m",
                market_end_ts_ms=1,
                level_index=0,
                target_shares=5.0,
                maker_price=0.40,
                status="PAPER_RESTING",
                gate={},
                up_token_id=f"{asset}-up",
                down_token_id=f"{asset}-down",
                loss_pool_before_usdc=0.0,
            )
        assert {cycle["asset"] for cycle in active_cycles(conn, scope="PAPER")} == {"BTC", "ETH"}
    finally:
        conn.close()


def test_paper_entry_does_not_wait_for_confirmation(tmp_path):
    engine = _engine(tmp_path)
    engine._gate_since = {}
    result = engine.tick()
    assert {
        asset
        for asset, value in result["assets"].items()
        if value["status"] == "PAPER_OPENED"
    } == set(ASSETS)


@pytest.mark.parametrize("research_reason", ["MID_RANGE_TOO_WIDE", "NET_DRIFT_TOO_HIGH"])
def test_paper_price_history_rejections_are_diagnostic_only(
    tmp_path,
    monkeypatch,
    research_reason,
):
    engine = _engine(tmp_path)

    def rejected_regime(**_kwargs):
        return RegimeDecision(
            eligible=False,
            reason=research_reason,
            score=0.0,
            up_mid=0.70,
            down_mid=0.30,
            mid_range=0.30,
            net_drift=0.20,
            slope_per_sec=0.02,
            one_way_ratio=1.0,
            max_jump=0.10,
            complement_residual=0.0,
            history_span_sec=20.0,
        )

    monkeypatch.setattr(
        "p3_dual40_engine.evaluate_balanced_regime",
        rejected_regime,
    )
    result = engine.tick()

    assert {
        asset
        for asset, value in result["assets"].items()
        if value["status"] == "PAPER_OPENED"
    } == set(ASSETS)
    conn = connect_dual40(engine.settings.p3_db_path)
    try:
        decisions = market_decisions(conn, scope="PAPER")
        assert all(item["reason"] == "PAPER_RELAXED_LIMIT_READY" for item in decisions)
        assert all(
            item["final_gate"]["base_gate"]["research_reason"] == research_reason
            for item in decisions
        )
        assert all(
            item["final_gate"]["base_gate"]["reason"]
            == "PAPER_RELAXED_LIMIT_READY"
            for item in decisions
        )
    finally:
        conn.close()


def test_paper_waits_when_entry_ask_is_far_below_ptb(tmp_path):
    settings = _settings(
        tmp_path,
        dual40_assets_csv="BTC",
        dual40_paper_max_concurrent_assets=1,
        dual40_paper_min_entry_ask=0.25,
    )
    now_ms = int(time.time() * 1000)
    _seed_p26(
        settings.p26_db_path,
        assets=("BTC",),
        now_ms=now_ms,
        up_ask=0.04,
        down_ask=0.97,
    )
    state = LiveState(
        live_feature_enabled=settings.live_feature_enabled,
        auto_execute_enabled=settings.live_auto_execute_enabled,
    )
    engine = Dual40MakerEngine(settings, state, gateway_factory=lambda _: _FakeGateway())
    engine._gate_since["PAPER:BTC:cond-btc"] = time.monotonic() - 2.0

    result = engine.tick()

    assert result["assets"]["BTC"]["status"] != "PAPER_OPENED"
    conn = connect_dual40(settings.p3_db_path)
    try:
        assert active_cycles(conn, scope="PAPER") == []
        decisions = market_decisions(conn, scope="PAPER")
        assert len(decisions) == 1
        assert decisions[0]["reason"] == "PAPER_ENTRY_ASK_TOO_LOW"
        assert decisions[0]["final_gate"]["base_gate"]["paper_min_entry_ask"] == 0.25
    finally:
        conn.close()


def test_live_keeps_price_history_regime_rejections(tmp_path, monkeypatch):
    settings = _settings(
        tmp_path,
        live_feature_enabled=True,
        live_auto_execute_enabled=True,
        web_auth_required=True,
        web_password="very-safe-test-password",
    )
    _seed_p26(settings.p26_db_path)
    state = LiveState(live_feature_enabled=True, auto_execute_enabled=True)
    state.arm({"ok": True, "checked_at_ms": int(time.time() * 1000)})
    gateway = _FakeGateway()
    gateway.posted = []
    engine = Dual40MakerEngine(settings, state, gateway_factory=lambda _: gateway)
    engine._fresh_preflight = lambda: True

    monkeypatch.setattr(
        "p3_dual40_engine.evaluate_balanced_regime",
        lambda **_kwargs: RegimeDecision(
            eligible=False,
            reason="MID_RANGE_TOO_WIDE",
            score=0.0,
            up_mid=0.70,
            down_mid=0.30,
            mid_range=0.30,
            net_drift=0.20,
            slope_per_sec=0.02,
            one_way_ratio=1.0,
            max_jump=0.10,
            complement_residual=0.0,
            history_span_sec=20.0,
        ),
    )
    engine.tick()

    conn = connect_dual40(settings.p3_db_path)
    try:
        assert active_cycles(conn, scope="LIVE") == []
        assert gateway.posted == []
        assert {
            item["reason"] for item in market_decisions(conn, scope="LIVE")
        } == {"MID_RANGE_TOO_WIDE"}
    finally:
        conn.close()


def test_paper_marketable_limit_fills_entry_side_immediately(tmp_path):
    settings = _settings(tmp_path)
    engine = Dual40MakerEngine(
        settings,
        LiveState(live_feature_enabled=False, auto_execute_enabled=False),
        gateway_factory=lambda _: _FakeGateway(),
    )
    now_ms = int(time.time() * 1000)
    candidate = {
        "asset": "BTC",
        "condition_id": "paper-cross-btc",
        "combo_key": "BTC:5m",
        "market_end_ts_ms": now_ms + 180_000,
        "up_token_id": "btc-up",
        "down_token_id": "btc-down",
        "score": 0.0,
        "stages": [],
        "up_book": {
            "id": 11,
            "best_ask": 0.75,
            "source_ts_ms": now_ms,
            "recv_ts_ms": now_ms,
        },
        "down_book": {
            "id": 12,
            "best_ask": 0.25,
            "source_ts_ms": now_ms,
            "recv_ts_ms": now_ms,
        },
    }
    conn = connect_dual40(settings.p3_db_path)
    try:
        result = engine._open_paper(
            conn,
            candidate,
            ladder_state(conn, "PAPER", "BTC"),
        )
        cycle = active_cycle(conn, scope="PAPER", asset="BTC")

        assert result["status"] == "PAPER_OPENED"
        assert cycle is not None
        assert cycle["up_filled_shares"] == pytest.approx(0.0)
        assert cycle["down_filled_shares"] == pytest.approx(5.0)
        assert cycle["up_fill_price"] is None
        assert cycle["down_fill_price"] == pytest.approx(0.25)
        assert cycle["residual_side"] == "DOWN"
        assert cycle["details"]["post_only"] is False
        assert cycle["details"]["paper_down_fill_evidence"]["best_ask"] == pytest.approx(0.25)
        assert cycle["details"]["paper_down_fill_evidence"]["fill_kind"] == (
            "ENTRY_MARKETABLE_LIMIT"
        )
    finally:
        conn.close()


def test_market_decision_record_created_for_every_condition(tmp_path):
    engine = _engine(tmp_path)
    engine.tick()
    conn = connect_dual40(engine.settings.p3_db_path)
    try:
        decisions = market_decisions(conn, scope="PAPER")
        assert len(decisions) == 4
        assert {item["asset"] for item in decisions} == set(ASSETS)
        assert all(item["decision"] == "OPENED" for item in decisions)
    finally:
        conn.close()


def test_restart_preserves_four_active_paper_cycles(tmp_path):
    engine = _engine(tmp_path)
    engine.tick()
    reopened = connect_dual40(engine.settings.p3_db_path)
    try:
        assert len(active_cycles(reopened, scope="PAPER")) == 4
        assert active_cycle(reopened, scope="PAPER", asset="BTC") is not None
    finally:
        reopened.close()


def test_live_default_max_concurrent_is_one(tmp_path):
    settings = _settings(
        tmp_path,
        live_feature_enabled=True,
        live_auto_execute_enabled=True,
        web_auth_required=True,
        web_password="very-safe-test-password",
    )
    _seed_p26(settings.p26_db_path)
    state = LiveState(live_feature_enabled=True, auto_execute_enabled=True)
    state.arm({"ok": True, "checked_at_ms": int(time.time() * 1000)})
    gateway = _FakeGateway()
    gateway.posted = []
    engine = Dual40MakerEngine(settings, state, gateway_factory=lambda _: gateway)
    engine._fresh_preflight = lambda: True
    for asset in ASSETS:
        engine._gate_since[f"LIVE:{asset}:cond-{asset.lower()}"] = time.monotonic() - 2.0

    engine.tick()
    conn = connect_dual40(settings.p3_db_path)
    try:
        assert settings.dual40_live_max_concurrent_assets == 1
        assert len(active_cycles(conn, scope="LIVE")) == 1
        assert len(gateway.posted) == 1
    finally:
        conn.close()


def test_live_still_waits_for_confirmation(tmp_path):
    settings = _settings(
        tmp_path,
        live_feature_enabled=True,
        live_auto_execute_enabled=True,
        web_auth_required=True,
        web_password="very-safe-test-password",
    )
    _seed_p26(settings.p26_db_path)
    state = LiveState(live_feature_enabled=True, auto_execute_enabled=True)
    state.arm({"ok": True, "checked_at_ms": int(time.time() * 1000)})
    gateway = _FakeGateway()
    gateway.posted = []
    engine = Dual40MakerEngine(settings, state, gateway_factory=lambda _: gateway)
    engine._fresh_preflight = lambda: True

    result = engine.tick()

    conn = connect_dual40(settings.p3_db_path)
    try:
        assert active_cycles(conn, scope="LIVE") == []
        assert gateway.posted == []
        assert {
            value["status"] for value in result["assets"].values()
        } == {"WAITING_FOR_BALANCED_MARKET"}
    finally:
        conn.close()


def test_live_no_real_network_or_order_submission(tmp_path):
    test_live_default_max_concurrent_is_one(tmp_path)


def test_deterministic_local_simulation_asset_scoped_recovery(tmp_path):
    engine = _engine(tmp_path)
    conn = connect_dual40(engine.settings.p3_db_path)
    try:
        scenarios = {
            "BTC": {"up": 5.0, "down": 5.0, "pnl": 1.0, "status": "PAPER_MATCHED_FILLED", "official": None},
            "ETH": {"up": 5.0, "down": 0.0, "pnl": -2.0, "status": "PAPER_SINGLE_LEG_LOSS", "official": None},
            "SOL": {"up": 0.0, "down": 0.0, "pnl": 0.0, "status": "NO_FILL", "official": None},
            "XRP": {"up": 3.0, "down": 1.0, "pnl": -0.6, "status": "PAPER_SINGLE_LEG_LOSS", "official": None},
        }
        for asset, scenario in scenarios.items():
            cycle_id = create_cycle(
                conn,
                scope="PAPER",
                asset=asset,
                session_id=None,
                condition_id=f"sim-{asset.lower()}",
                combo_key=f"{asset}:5m",
                market_end_ts_ms=1,
                level_index=0,
                target_shares=5.0,
                maker_price=0.40,
                status="WAIT_RESOLUTION",
                gate={},
                up_token_id=f"{asset}-up",
                down_token_id=f"{asset}-down",
                loss_pool_before_usdc=0.0,
            )
            engine._apply_ladder_and_finalize(
                conn,
                cycle={
                    "id": cycle_id,
                    "scope": "PAPER",
                    "asset": asset,
                    "combo_key": f"{asset}:5m",
                    "condition_id": f"sim-{asset.lower()}",
                    "market_end_ts_ms": 1,
                    "loss_pool_before_usdc": 0.0,
                },
                status=str(scenario["status"]),
                pnl=float(scenario["pnl"]),
                official_result=scenario["official"],
            )

        assert ladder_state(conn, "PAPER", "BTC")["level_index"] == 0
        assert ladder_state(conn, "PAPER", "BTC")["loss_pool_usdc"] == 0.0
        assert ladder_state(conn, "PAPER", "ETH")["level_index"] == 1
        assert ladder_state(conn, "PAPER", "ETH")["loss_pool_usdc"] == pytest.approx(2.0)
        assert ladder_state(conn, "PAPER", "SOL")["level_index"] == 0
        assert ladder_state(conn, "PAPER", "SOL")["loss_pool_usdc"] == 0.0
        assert ladder_state(conn, "PAPER", "XRP")["level_index"] == 0
        assert ladder_state(conn, "PAPER", "XRP")["loss_pool_usdc"] == pytest.approx(0.6)
        assert {item["asset"] for item in market_decisions(conn, scope="PAPER", limit=20) if item["condition_id"].startswith("sim-")} == set(ASSETS)
    finally:
        conn.close()
