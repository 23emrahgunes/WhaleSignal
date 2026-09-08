from __future__ import annotations

import json
import sqlite3
import time

import pytest

from p3_dual40_core import (
    MidPoint,
    OpeningGateProfile,
    evaluate_opening_stability,
)
from p3_dual40_engine import Dual40MakerEngine
from p3_dual40_forecast import (
    ForecastGateDecision,
    P25StateForecastProvider,
    evaluate_forecast_value,
)
from p3_dual40_store import (
    active_cycles,
    connect_dual40,
    create_cycle,
    ladder_state,
    market_decisions,
    set_ladder_state,
    update_cycle,
)
from p3_live_state import LiveState
from test_p3_dual40_parallel import ASSETS, _FakeGateway, _seed_p26, _settings


def _profile() -> OpeningGateProfile:
    return OpeningGateProfile(
        observation_sec=35.0,
        max_mid_range=0.06,
        max_net_drift=0.025,
        max_one_way_ratio=0.65,
        max_spread_each=0.08,
        forecast_min_p_up=0.45,
        forecast_max_p_up=0.55,
        max_queue_imbalance=3.0,
        min_depth_balance_ratio=0.75,
    )


def _points(values: list[float]) -> list[MidPoint]:
    step = 35_000 // (len(values) - 1)
    return [MidPoint(1_000_000 + index * step, value) for index, value in enumerate(values)]


def _opening(
    up: list[float],
    down: list[float],
    *,
    profile: OpeningGateProfile | None = None,
    queue_up: float = 2.0,
    queue_down: float = 2.0,
    depth_up: float = 20.0,
    depth_down: float = 20.0,
):
    return evaluate_opening_stability(
        profile=profile or _profile(),
        up_points=_points(up),
        down_points=_points(down),
        market_age_sec=35.0,
        current_up_spread=0.04,
        current_down_spread=0.04,
        queue_up_at_40=queue_up,
        queue_down_at_40=queue_down,
        near_depth_up=depth_up,
        near_depth_down=depth_down,
    )


def test_full_35_second_balanced_opening_is_eligible():
    result = _opening([0.50] * 8, [0.50] * 8)
    assert result.eligible is True
    assert result.reason == "OPENING_STABLE_TWO_WAY"
    assert result.history_span_sec >= 34.0


def test_early_directional_move_then_flat_is_rejected_by_opening_drift():
    result = _opening(
        [0.50, 0.54, 0.54, 0.54, 0.54, 0.54, 0.54, 0.54],
        [0.50, 0.46, 0.46, 0.46, 0.46, 0.46, 0.46, 0.46],
    )
    assert result.eligible is False
    assert result.reason == "REJECTED_OPENING_DRIFT"


def test_opening_queue_and_counter_leg_depth_are_independent_rejects():
    queue = _opening([0.50] * 8, [0.50] * 8, queue_up=10.0, queue_down=1.0)
    assert queue.reason == "REJECTED_ASYMMETRIC_40C_QUEUE"

    depth = _opening([0.50] * 8, [0.50] * 8, depth_up=20.0, depth_down=5.0)
    assert depth.reason == "REJECTED_THIN_COUNTER_LEG"


def test_opening_profile_is_stricter_at_recovery_levels(tmp_path):
    settings = _settings(tmp_path)
    normal = settings.dual40_gate_profile(0)
    level_10 = settings.dual40_gate_profile(1)
    level_30 = settings.dual40_gate_profile(2)
    assert normal["observation_sec"] < level_10["observation_sec"] < level_30["observation_sec"]
    assert normal["max_net_drift"] > level_10["max_net_drift"] > level_30["max_net_drift"]
    assert normal["forecast_min_p_up"] < level_10["forecast_min_p_up"] < level_30["forecast_min_p_up"]


def test_forecast_value_neutral_directional_and_stale():
    common = {
        "confidence": 0.05,
        "model_version": "model-v1",
        "max_age_ms": 2000,
        "minimum_p_up": 0.45,
        "maximum_p_up": 0.55,
        "combo_key": "BTC:5m",
        "condition_suffix": "12345678",
    }
    neutral = evaluate_forecast_value(p_up_external=0.50, age_ms=100, **common)
    assert neutral.eligible is True
    assert neutral.reason == "FORECAST_NEUTRAL"

    directional = evaluate_forecast_value(p_up_external=0.60, age_ms=100, **common)
    assert directional.eligible is False
    assert directional.reason == "REJECTED_STRONG_DIRECTIONAL_ALPHA"

    stale = evaluate_forecast_value(p_up_external=0.50, age_ms=2001, **common)
    assert stale.eligible is False
    assert stale.reason == "FORECAST_STALE"


class _Response:
    def __init__(self, payload: dict):
        self.raw = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit: int) -> bytes:
        return self.raw


def test_forecast_provider_matches_current_card_and_caches_one_fetch():
    calls: list[object] = []
    payload = {
        "cards": [
            {
                "combo": "BTC:5m",
                "active": True,
                "condition_id": "0xABCDEF0012345678",
                "tte_sec": 240.0,
                "prediction_ready": True,
                "p_up_external": 0.50,
                "confidence": 0.04,
                "model_version": "model-v1",
                "clob_age_ms": 100,
                "transport_age_ms": 100,
                "source_age_ms": 100,
                "book_age_ms": 100,
            }
        ]
    }

    def opener(request, timeout):  # noqa: ANN001
        calls.append((request, timeout))
        return _Response(payload)

    provider = P25StateForecastProvider(
        state_url="http://127.0.0.1:8091/api/state",
        timeout_ms=250,
        max_age_ms=2000,
        cache_ms=2000,
        tte_tolerance_sec=3.0,
        opener=opener,
    )
    first = provider.evaluate(
        now_ms=10_000,
        combo_key="BTC:5m",
        condition_id="condition-12345678",
        tte_sec=240.0,
        minimum_p_up=0.45,
        maximum_p_up=0.55,
    )
    second = provider.evaluate(
        now_ms=11_000,
        combo_key="BTC:5m",
        condition_id="condition-12345678",
        tte_sec=239.0,
        minimum_p_up=0.45,
        maximum_p_up=0.55,
    )
    assert first.eligible is True
    assert second.eligible is True
    assert len(calls) == 1


def test_forecast_provider_adjusts_cached_tte_and_rejects_stale_payload():
    payload = {
        "now": 100.0,
        "cards": [
            {
                "combo": "ETH:5m",
                "active": True,
                "condition_id": "0xabcdef0012345678",
                "tte_sec": 240.0,
                "prediction_ready": True,
                "p_up_external": 0.50,
                "confidence": 0.04,
                "model_version": "model-v1",
                "clob_age_ms": 100,
                "transport_age_ms": 100,
                "source_age_ms": 100,
                "book_age_ms": 100,
            }
        ],
    }

    provider = P25StateForecastProvider(
        state_url="http://127.0.0.1:8091/api/state",
        timeout_ms=250,
        max_age_ms=2000,
        cache_ms=2000,
        tte_tolerance_sec=3.0,
        opener=lambda _request, timeout: _Response(payload),
    )
    fresh = provider.evaluate(
        now_ms=101_000,
        combo_key="ETH:5m",
        condition_id="condition-12345678",
        tte_sec=239.0,
        minimum_p_up=0.45,
        maximum_p_up=0.55,
    )
    assert fresh.eligible is True
    assert fresh.age_ms == 1100

    stale_provider = P25StateForecastProvider(
        state_url="http://127.0.0.1:8091/api/state",
        timeout_ms=250,
        max_age_ms=2000,
        cache_ms=2000,
        tte_tolerance_sec=3.0,
        opener=lambda _request, timeout: _Response(payload),
    )
    stale = stale_provider.evaluate(
        now_ms=113_720,
        combo_key="ETH:5m",
        condition_id="condition-12345678",
        tte_sec=226.28,
        minimum_p_up=0.45,
        maximum_p_up=0.55,
    )
    assert stale.eligible is False
    assert stale.reason == "FORECAST_STALE"
    assert stale.age_ms == 13_820


def test_forecast_provider_rejects_different_condition_suffix():
    payload = {
        "cards": [
            {
                "combo": "BTC:5m",
                "active": True,
                "condition_id": "0xabcdef0087654321",
                "tte_sec": 240.0,
                "prediction_ready": True,
                "p_up_external": 0.50,
                "confidence": 0.04,
                "model_version": "model-v1",
                "clob_age_ms": 100,
            }
        ]
    }
    provider = P25StateForecastProvider(
        state_url="http://127.0.0.1:8091/api/state",
        timeout_ms=250,
        max_age_ms=2000,
        cache_ms=2000,
        tte_tolerance_sec=3.0,
        opener=lambda _request, timeout: _Response(payload),
    )

    decision = provider.evaluate(
        now_ms=10_000,
        combo_key="BTC:5m",
        condition_id="0xabcdef0012345678",
        tte_sec=240.0,
        minimum_p_up=0.45,
        maximum_p_up=0.55,
    )

    assert decision.eligible is False
    assert decision.reason == "FORECAST_MARKET_MISMATCH"


class _FixedForecast:
    def __init__(self, probability: float):
        self.probability = probability

    def evaluate(self, **kwargs):  # noqa: ANN003
        allowed = kwargs["minimum_p_up"] <= self.probability <= kwargs["maximum_p_up"]
        return ForecastGateDecision(
            allowed,
            "FORECAST_NEUTRAL" if allowed else "REJECTED_STRONG_DIRECTIONAL_ALPHA",
            True,
            self.probability,
            0.05,
            "model-v1",
            100,
            kwargs["combo_key"],
            kwargs["condition_id"][-8:],
        )


def _engine_with_forecast(tmp_path, *, mode: str, probability: float) -> Dual40MakerEngine:
    settings = _settings(
        tmp_path,
        dual40_forecast_gate_mode=mode,
        dual40_opening_gate_mode="OFF",
        dual40_global_risk_mode="OFF",
    )
    _seed_p26(settings.p26_db_path)
    engine = Dual40MakerEngine(
        settings,
        LiveState(live_feature_enabled=False, auto_execute_enabled=False),
        gateway_factory=lambda _: _FakeGateway(),
        forecast_provider=_FixedForecast(probability),
    )
    now = time.monotonic()
    for asset in ASSETS:
        engine._gate_since[f"PAPER:{asset}:cond-{asset.lower()}"] = now - 2.0
    return engine


def test_shadow_strong_forecast_does_not_change_existing_paper_opening(tmp_path):
    engine = _engine_with_forecast(tmp_path, mode="SHADOW", probability=0.70)
    engine.tick()
    conn = connect_dual40(engine.settings.p3_db_path)
    try:
        assert len(active_cycles(conn, scope="PAPER")) == 4
        decisions = market_decisions(conn, scope="PAPER")
        assert all(item["decision"] == "OPENED" for item in decisions)
        assert all(
            item["final_gate"]["forecast_gate"]["reason"]
            == "REJECTED_STRONG_DIRECTIONAL_ALPHA"
            for item in decisions
        )
    finally:
        conn.close()


def test_enforced_recovery_band_skips_market_without_mutating_debt(tmp_path):
    engine = _engine_with_forecast(tmp_path, mode="ENFORCE", probability=0.46)
    conn = connect_dual40(engine.settings.p3_db_path)
    try:
        set_ladder_state(
            conn,
            scope="PAPER",
            asset="BTC",
            level_index=1,
            loss_pool_usdc=2.0,
            hard_stopped=False,
            hard_stop_reason=None,
        )
    finally:
        conn.close()

    engine.tick()
    conn = connect_dual40(engine.settings.p3_db_path)
    try:
        assert {cycle["asset"] for cycle in active_cycles(conn, scope="PAPER")} == {
            "ETH",
            "SOL",
            "XRP",
        }
        btc = ladder_state(conn, "PAPER", "BTC")
        assert btc["level_index"] == 1
        assert btc["loss_pool_usdc"] == pytest.approx(2.0)
        decision = next(
            item for item in market_decisions(conn, scope="PAPER") if item["asset"] == "BTC"
        )
        assert decision["reason"] == "REJECTED_STRONG_DIRECTIONAL_ALPHA"
    finally:
        conn.close()


def _engine_with_opening_history(
    tmp_path,
    *,
    directional: bool,
) -> tuple[Dual40MakerEngine, int]:
    now_ms = int(time.time() * 1000)
    settings = _settings(
        tmp_path,
        dual40_opening_gate_mode="ENFORCE",
        dual40_forecast_gate_mode="OFF",
        dual40_global_risk_mode="OFF",
    )
    _seed_p26(settings.p26_db_path, now_ms=now_ms)
    conn = sqlite3.connect(settings.p26_db_path)
    try:
        conn.execute("UPDATE p26_market_tokens SET active=0 WHERE combo_key<>'BTC:5m'")
        end_ms = conn.execute(
            "SELECT market_end_ts_ms FROM p26_market_tokens WHERE combo_key='BTC:5m' LIMIT 1"
        ).fetchone()[0]
        start_ms = int(end_ms) - 300_000
        conn.execute("DELETE FROM p26_clob_books WHERE condition_id='cond-btc'")
        opening_up = [0.50, 0.54, 0.54, 0.54, 0.54, 0.54, 0.54, 0.54]
        opening_down = [1.0 - value for value in opening_up]
        if not directional:
            opening_up = [0.50] * 8
            opening_down = [0.50] * 8

        def insert_book(side: str, mid: float, ts_ms: int) -> None:
            bids = json.dumps([[round(mid - 0.02, 4), 10.0], [0.40, 2.0]])
            asks = json.dumps([[round(mid + 0.02, 4), 20.0]])
            conn.execute(
                """
                INSERT INTO p26_clob_books(
                    condition_id,token_id,side,source_ts_ms,recv_ts_ms,
                    inserted_at_ms,bids_json,asks_json
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                ("cond-btc", f"btc-{side.lower()}", side, ts_ms, ts_ms, ts_ms, bids, asks),
            )

        for index, (up_mid, down_mid) in enumerate(zip(opening_up, opening_down)):
            ts_ms = start_ms + index * 5_000
            insert_book("UP", up_mid, ts_ms)
            insert_book("DOWN", down_mid, ts_ms)
        for ts_ms in (now_ms - 1_000, now_ms):
            insert_book("UP", 0.50, ts_ms)
            insert_book("DOWN", 0.50, ts_ms)
        conn.commit()
    finally:
        conn.close()

    engine = Dual40MakerEngine(
        settings,
        LiveState(live_feature_enabled=False, auto_execute_enabled=False),
        gateway_factory=lambda _: _FakeGateway(),
    )
    engine._gate_since["PAPER:BTC:cond-btc"] = time.monotonic() - 2.0
    return engine, now_ms


def test_engine_rejects_early_directional_opening_even_when_recent_window_is_flat(
    monkeypatch,
    tmp_path,
):
    engine, now_ms = _engine_with_opening_history(tmp_path, directional=True)
    monkeypatch.setattr("p3_dual40_engine.time.time", lambda: now_ms / 1000.0)
    engine.tick()
    conn = connect_dual40(engine.settings.p3_db_path)
    try:
        assert active_cycles(conn, scope="PAPER") == []
        decision = market_decisions(conn, scope="PAPER")[0]
        assert decision["reason"] == "REJECTED_OPENING_DRIFT"
        assert decision["final_gate"]["base_gate"]["eligible"] is True
        assert decision["final_gate"]["opening_gate"]["eligible"] is False
    finally:
        conn.close()


def test_engine_opens_after_full_balanced_opening_window(monkeypatch, tmp_path):
    engine, now_ms = _engine_with_opening_history(tmp_path, directional=False)
    monkeypatch.setattr("p3_dual40_engine.time.time", lambda: now_ms / 1000.0)
    result = engine.tick()
    assert result["assets"]["BTC"]["status"] == "PAPER_OPENED"


def test_enforced_global_daily_loss_gate_blocks_new_cycles(tmp_path):
    settings = _settings(
        tmp_path,
        dual40_opening_gate_mode="OFF",
        dual40_forecast_gate_mode="OFF",
        dual40_global_risk_mode="ENFORCE",
        dual40_daily_loss_limit_usdc=1.0,
    )
    _seed_p26(settings.p26_db_path)
    conn = connect_dual40(settings.p3_db_path)
    try:
        cycle_id = create_cycle(
            conn,
            scope="PAPER",
            asset="BTC",
            session_id=None,
            condition_id="historic-loss",
            combo_key="BTC:5m",
            market_end_ts_ms=1,
            level_index=0,
            target_shares=5.0,
            maker_price=0.40,
            status="WAIT_RESOLUTION",
            gate={},
            up_token_id="old-up",
            down_token_id="old-down",
            loss_pool_before_usdc=0.0,
        )
        update_cycle(
            conn,
            cycle_id,
            status="RESOLVED_DOWN",
            realized_pnl_usdc=-2.0,
            resolved_at_ms=int(time.time() * 1000),
        )
    finally:
        conn.close()

    engine = Dual40MakerEngine(
        settings,
        LiveState(live_feature_enabled=False, auto_execute_enabled=False),
        gateway_factory=lambda _: _FakeGateway(),
    )
    now = time.monotonic()
    for asset in ASSETS:
        engine._gate_since[f"PAPER:{asset}:cond-{asset.lower()}"] = now - 2.0
    engine.tick()
    conn = connect_dual40(settings.p3_db_path)
    try:
        assert active_cycles(conn, scope="PAPER") == []
        assert {
            item["reason"] for item in market_decisions(conn, scope="PAPER")
        } == {"GLOBAL_DAILY_LOSS_LIMIT"}
    finally:
        conn.close()
