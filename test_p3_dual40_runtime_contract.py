from __future__ import annotations

import sqlite3
import time

import pytest

from p26_book_store import BookSnapshotStore
from p26_execution import OrderBookSnapshot
from p3_config import DUAL40_MODE, P3Settings
from p3_dual40_runtime import ProductionDual40MakerEngine
from p3_dual40_store import (
    active_cycle,
    connect_dual40,
    create_cycle,
    cycle_for_condition,
    ladder_state,
    update_cycle,
)
from p3_live_state import LiveState


def _settings(tmp_path) -> P3Settings:
    return P3Settings(
        _env_file=None,
        strategy_mode=DUAL40_MODE,
        p26_db_path=str(tmp_path / "p26.sqlite"),
        p3_db_path=str(tmp_path / "p3.sqlite"),
        reports_dir=str(tmp_path / "reports"),
        live_feature_enabled=True,
        live_auto_execute_enabled=True,
        dual40_min_collateral_to_arm_usdc=35.0,
        dual40_near_touch_price=0.41,
    )


def test_production_runtime_reads_real_p26_book_schema_and_near_touch(tmp_path):
    settings = _settings(tmp_path)
    engine = ProductionDual40MakerEngine(
        settings,
        LiveState(live_feature_enabled=True, auto_execute_enabled=True),
        gateway_factory=lambda _: object(),
    )

    assert engine.policy.near_touch_price == pytest.approx(0.41)

    store = BookSnapshotStore(settings.p26_db_path)
    try:
        # A newer exchange source timestamp was observed first.
        store.insert(
            condition_id="cond-1",
            combo_key="BTC:5m",
            side="UP",
            snapshot=OrderBookSnapshot.from_levels(
                token_id="up-token",
                ts_ms=2_000,
                bids=[(0.39, 10.0)],
                asks=[(0.42, 9.0)],
            ),
            recv_ts_ms=3_000,
        )
        # A reconnect may freshly observe an older unchanged source timestamp.
        # Runtime freshness must follow recv_ts_ms, not source_ts_ms.
        store.insert(
            condition_id="cond-1",
            combo_key="BTC:5m",
            side="UP",
            snapshot=OrderBookSnapshot.from_levels(
                token_id="up-token",
                ts_ms=1_000,
                bids=[(0.39, 10.0)],
                asks=[(0.40, 2.0), (0.41, 7.0)],
            ),
            recv_ts_ms=4_000,
        )
    finally:
        store.close()

    conn = sqlite3.connect(settings.p26_db_path)
    conn.row_factory = sqlite3.Row
    try:
        view = engine._latest_book(conn, "cond-1", "up")
    finally:
        conn.close()

    assert view is not None
    assert view["recv_ts_ms"] == 4_000
    assert view["source_ts_ms"] == 1_000
    assert view["best_ask"] == pytest.approx(0.40)
    assert view["visible_ask_capacity_at_maker"] == pytest.approx(2.0)


def _create_paper_cycle(
    conn,
    *,
    now_ms: int,
    status: str = "PAPER_RESTING",
    market_end_ts_ms: int | None = None,
) -> int:
    return create_cycle(
        conn,
        scope="PAPER",
        asset="XRP",
        session_id=None,
        condition_id="paper-condition",
        combo_key="XRP:5m",
        market_end_ts_ms=market_end_ts_ms or now_ms + 60_000,
        level_index=0,
        target_shares=5.0,
        maker_price=0.40,
        status=status,
        gate={},
        up_token_id="up-token",
        down_token_id="down-token",
        loss_pool_before_usdc=0.0,
    )


def _insert_touch_pair(
    path: str,
    *,
    observed_ms: int,
    source_ms: int | None = None,
    ask_size: float = 0.01,
) -> None:
    store = BookSnapshotStore(path)
    try:
        for side, token in (("UP", "up-token"), ("DOWN", "down-token")):
            store.insert(
                condition_id="paper-condition",
                combo_key="XRP:5m",
                side=side,
                snapshot=OrderBookSnapshot.from_levels(
                    token_id=token,
                    ts_ms=source_ms or observed_ms,
                    bids=[(0.39, 10.0)],
                    asks=[(0.40, ask_size)],
                ),
                recv_ts_ms=observed_ms,
            )
    finally:
        store.close()


def test_paper_any_recorded_40c_touch_fills_full_virtual_pair(tmp_path):
    settings = _settings(tmp_path)
    engine = ProductionDual40MakerEngine(
        settings,
        LiveState(live_feature_enabled=True, auto_execute_enabled=True),
        gateway_factory=lambda _: object(),
    )
    conn = connect_dual40(settings.p3_db_path)
    try:
        now_ms = int(time.time() * 1000)
        _create_paper_cycle(conn, now_ms=now_ms)
        cycle = active_cycle(conn, scope="PAPER", asset="XRP")
        assert cycle is not None
        observed_ms = int(cycle["created_at_ms"]) + 100
        _insert_touch_pair(settings.p26_db_path, observed_ms=observed_ms)

        p26 = sqlite3.connect(settings.p26_db_path)
        p26.row_factory = sqlite3.Row
        try:
            result = engine._paper_tick(conn, p26, cycle, observed_ms + 100)
        finally:
            p26.close()

        settled = cycle_for_condition(
            conn,
            scope="PAPER",
            asset="XRP",
            condition_id="paper-condition",
        )
        assert result["status"] == "PAPER_MATCHED_FILLED"
        assert settled is not None
        assert settled["up_filled_shares"] == pytest.approx(5.0)
        assert settled["down_filled_shares"] == pytest.approx(5.0)
        assert settled["realized_pnl_usdc"] == pytest.approx(1.0)
        assert settled["details"]["paper_fill_rule"] == (
            "ENTRY_OR_RECORDED_BEST_ASK_LE_MAKER_FULL_SIDE"
        )
        assert settled["details"]["paper_up_fill_evidence"]["touch_ts_ms"] == observed_ms
    finally:
        conn.close()


def test_paper_counts_delayed_touch_sourced_before_market_close(tmp_path):
    settings = _settings(tmp_path)
    engine = ProductionDual40MakerEngine(
        settings,
        LiveState(live_feature_enabled=True, auto_execute_enabled=True),
        gateway_factory=lambda _: object(),
    )
    conn = connect_dual40(settings.p3_db_path)
    try:
        now_ms = int(time.time() * 1000)
        market_end_ms = now_ms + 5_000
        _create_paper_cycle(
            conn,
            now_ms=now_ms,
            market_end_ts_ms=market_end_ms,
        )
        cycle = active_cycle(conn, scope="PAPER", asset="XRP")
        assert cycle is not None
        _insert_touch_pair(
            settings.p26_db_path,
            source_ms=int(cycle["created_at_ms"]) + 100,
            observed_ms=market_end_ms + 100,
        )

        p26 = sqlite3.connect(settings.p26_db_path)
        p26.row_factory = sqlite3.Row
        try:
            result = engine._paper_tick(conn, p26, cycle, market_end_ms + 200)
        finally:
            p26.close()

        assert result["status"] == "PAPER_MATCHED_FILLED"
        settled = cycle_for_condition(
            conn,
            scope="PAPER",
            asset="XRP",
            condition_id="paper-condition",
        )
        assert settled is not None
        assert settled["details"]["paper_up_fill_evidence"]["touch_ts_ms"] == (
            int(cycle["created_at_ms"]) + 100
        )
    finally:
        conn.close()


def test_paper_expiry_advances_partial_cycle_even_when_books_disappear(tmp_path):
    settings = _settings(tmp_path)
    engine = ProductionDual40MakerEngine(
        settings,
        LiveState(live_feature_enabled=True, auto_execute_enabled=True),
        gateway_factory=lambda _: object(),
    )
    store = BookSnapshotStore(settings.p26_db_path)
    store.close()
    conn = connect_dual40(settings.p3_db_path)
    try:
        now_ms = int(time.time() * 1000)
        cycle_id = _create_paper_cycle(
            conn,
            now_ms=now_ms,
            market_end_ts_ms=now_ms - 3_000,
        )
        update_cycle(
            conn,
            cycle_id,
            up_filled_shares=5.0,
            residual_side="UP",
            residual_shares=5.0,
        )
        cycle = active_cycle(conn, scope="PAPER", asset="XRP")
        assert cycle is not None

        p26 = sqlite3.connect(settings.p26_db_path)
        p26.row_factory = sqlite3.Row
        try:
            result = engine._paper_tick(conn, p26, cycle, now_ms)
        finally:
            p26.close()

        assert result["status"] == "PAPER_SINGLE_LEG_LOSS"
        refreshed = active_cycle(conn, scope="PAPER", asset="XRP")
        assert refreshed is None
        settled = cycle_for_condition(
            conn,
            scope="PAPER",
            asset="XRP",
            condition_id="paper-condition",
        )
        assert settled is not None
        assert settled["status"] == "PAPER_SINGLE_LEG_LOSS"
        assert settled["official_result"] is None
        assert settled["realized_pnl_usdc"] == pytest.approx(-2.0)
        assert settled["details"]["paper_waits_for_official_result"] is False
        recovery = ladder_state(conn, "PAPER", "XRP")
        assert recovery["level_index"] == 1
        assert engine.policy.ladder[recovery["level_index"]] == pytest.approx(10.0)
        assert recovery["loss_pool_usdc"] == pytest.approx(2.0)
    finally:
        conn.close()


def test_paper_keeps_virtual_limit_open_inside_legacy_cancel_window(tmp_path):
    settings = _settings(tmp_path)
    engine = ProductionDual40MakerEngine(
        settings,
        LiveState(live_feature_enabled=True, auto_execute_enabled=True),
        gateway_factory=lambda _: object(),
    )
    store = BookSnapshotStore(settings.p26_db_path)
    store.close()
    conn = connect_dual40(settings.p3_db_path)
    try:
        now_ms = int(time.time() * 1000)
        _create_paper_cycle(
            conn,
            now_ms=now_ms,
            market_end_ts_ms=now_ms + 20_000,
        )
        cycle = active_cycle(conn, scope="PAPER", asset="XRP")
        assert cycle is not None

        p26 = sqlite3.connect(settings.p26_db_path)
        p26.row_factory = sqlite3.Row
        try:
            result = engine._paper_tick(conn, p26, cycle, now_ms)
        finally:
            p26.close()

        assert result["status"] == "PAPER_WAIT_BOOK"
        assert active_cycle(conn, scope="PAPER", asset="XRP") is not None
    finally:
        conn.close()


def test_legacy_paper_wait_resolution_settles_without_official_result(
    tmp_path,
    monkeypatch,
):
    settings = _settings(tmp_path)
    engine = ProductionDual40MakerEngine(
        settings,
        LiveState(live_feature_enabled=True, auto_execute_enabled=True),
        gateway_factory=lambda _: object(),
    )
    store = BookSnapshotStore(settings.p26_db_path)
    store.close()
    conn = connect_dual40(settings.p3_db_path)
    try:
        now_ms = int(time.time() * 1000)
        cycle_id = _create_paper_cycle(
            conn,
            now_ms=now_ms,
            status="WAIT_RESOLUTION",
            market_end_ts_ms=now_ms - 3_000,
        )
        update_cycle(
            conn,
            cycle_id,
            down_filled_shares=5.0,
            residual_side="DOWN",
            residual_shares=5.0,
        )
        cycle = active_cycle(conn, scope="PAPER", asset="XRP")
        assert cycle is not None
        monkeypatch.setattr(
            engine,
            "_fetch_official_result",
            lambda _cycle: pytest.fail("PAPER must not fetch an official result"),
        )

        p26 = sqlite3.connect(settings.p26_db_path)
        p26.row_factory = sqlite3.Row
        try:
            result = engine._resolution_tick(conn, cycle, now_ms, p26=p26)
        finally:
            p26.close()

        assert result["status"] == "PAPER_SINGLE_LEG_LOSS"
        assert result["pnl_usdc"] == pytest.approx(-2.0)
        assert active_cycle(conn, scope="PAPER", asset="XRP") is None
    finally:
        conn.close()


def test_paper_touch_before_cycle_open_does_not_fill_virtual_orders(tmp_path):
    settings = _settings(tmp_path)
    engine = ProductionDual40MakerEngine(
        settings,
        LiveState(live_feature_enabled=True, auto_execute_enabled=True),
        gateway_factory=lambda _: object(),
    )
    before_open_ms = int(time.time() * 1000) - 10_000
    _insert_touch_pair(settings.p26_db_path, observed_ms=before_open_ms)
    conn = connect_dual40(settings.p3_db_path)
    try:
        now_ms = int(time.time() * 1000)
        _create_paper_cycle(conn, now_ms=now_ms)
        cycle = active_cycle(conn, scope="PAPER", asset="XRP")
        assert cycle is not None

        p26 = sqlite3.connect(settings.p26_db_path)
        p26.row_factory = sqlite3.Row
        try:
            result = engine._paper_tick(conn, p26, cycle, now_ms + 100)
        finally:
            p26.close()

        assert result["status"] == "PAPER_RESTING"
        refreshed = active_cycle(conn, scope="PAPER", asset="XRP")
        assert refreshed is not None
        assert refreshed["up_filled_shares"] == pytest.approx(0.0)
        assert refreshed["down_filled_shares"] == pytest.approx(0.0)
    finally:
        conn.close()
