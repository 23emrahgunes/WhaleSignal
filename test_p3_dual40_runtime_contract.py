from __future__ import annotations

import json
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
    set_ladder_state,
    update_cycle,
    upsert_market_decision,
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
    up_ask: float = 0.40,
    down_ask: float = 0.40,
) -> None:
    store = BookSnapshotStore(path)
    try:
        for side, token, ask in (
            ("UP", "up-token", up_ask),
            ("DOWN", "down-token", down_ask),
        ):
            store.insert(
                condition_id="paper-condition",
                combo_key="XRP:5m",
                side=side,
                snapshot=OrderBookSnapshot.from_levels(
                    token_id=token,
                    ts_ms=source_ms or observed_ms,
                    bids=[(max(0.01, ask - 0.01), 10.0)],
                    asks=[(ask, ask_size)],
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
        _insert_touch_pair(
            settings.p26_db_path,
            observed_ms=observed_ms,
            up_ask=0.25,
            down_ask=0.35,
        )

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
        assert settled["up_fill_price"] == pytest.approx(0.25)
        assert settled["down_fill_price"] == pytest.approx(0.35)
        assert settled["realized_pnl_usdc"] == pytest.approx(2.0)
        assert settled["details"]["paper_fill_rule"] == (
            "ENTRY_OR_RECORDED_BEST_ASK_LE_MAKER_FULL_SIDE"
        )
        assert settled["details"]["paper_up_fill_evidence"]["touch_ts_ms"] == observed_ms
    finally:
        conn.close()


def test_paper_fill_price_is_first_recorded_executable_ask(tmp_path):
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
        opened_ms = int(cycle["created_at_ms"])
        store = BookSnapshotStore(settings.p26_db_path)
        try:
            for offset_ms, ask in ((100, 0.35), (200, 0.20)):
                observed_ms = opened_ms + offset_ms
                store.insert(
                    condition_id="paper-condition",
                    combo_key="XRP:5m",
                    side="UP",
                    snapshot=OrderBookSnapshot.from_levels(
                        token_id="up-token",
                        ts_ms=observed_ms,
                        bids=[(ask - 0.01, 10.0)],
                        asks=[(ask, 5.0)],
                    ),
                    recv_ts_ms=observed_ms,
                )
        finally:
            store.close()

        p26 = sqlite3.connect(settings.p26_db_path)
        p26.row_factory = sqlite3.Row
        try:
            result = engine._paper_tick(conn, p26, cycle, opened_ms + 300)
        finally:
            p26.close()

        assert result["status"] == "PAPER_WAIT_BOOK"
        waiting = active_cycle(conn, scope="PAPER", asset="XRP")
        assert waiting is not None
        assert waiting["up_fill_price"] == pytest.approx(0.35)
        assert waiting["details"]["paper_up_fill_evidence"]["best_ask"] == pytest.approx(
            0.35
        )
    finally:
        conn.close()


def test_paper_rechecks_existing_book_rows_when_recv_time_advances(tmp_path):
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
        opened_ms = int(cycle["created_at_ms"])

        store = BookSnapshotStore(settings.p26_db_path)
        try:
            snapshot = OrderBookSnapshot.from_levels(
                token_id="up-token",
                ts_ms=opened_ms - 100,
                bids=[(0.005, 10.0)],
                asks=[(0.01, 5.0)],
            )
            store.insert(
                condition_id="paper-condition",
                combo_key="XRP:5m",
                side="UP",
                snapshot=snapshot,
                recv_ts_ms=opened_ms - 50,
            )
            row_id = store.conn.execute(
                "SELECT id FROM p26_clob_books WHERE condition_id=? AND side='UP'",
                ("paper-condition",),
            ).fetchone()[0]
            store.insert(
                condition_id="paper-condition",
                combo_key="XRP:5m",
                side="UP",
                snapshot=snapshot,
                recv_ts_ms=opened_ms + 250,
            )
        finally:
            store.close()

        update_cycle(
            conn,
            int(cycle["id"]),
            details_merge={"paper_last_scanned_up_book_id": int(row_id) + 100},
        )
        cycle = active_cycle(conn, scope="PAPER", asset="XRP")
        assert cycle is not None

        p26 = sqlite3.connect(settings.p26_db_path)
        p26.row_factory = sqlite3.Row
        try:
            result = engine._paper_tick(conn, p26, cycle, opened_ms + 300)
        finally:
            p26.close()

        assert result["status"] == "PAPER_WAIT_BOOK"
        waiting = active_cycle(conn, scope="PAPER", asset="XRP")
        assert waiting is not None
        assert waiting["up_filled_shares"] == pytest.approx(5.0)
        assert waiting["up_fill_price"] == pytest.approx(0.01)
        assert waiting["details"]["paper_up_fill_evidence"]["book_id"] == row_id
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


def test_paper_expiry_waits_for_official_result_when_one_leg_filled(tmp_path):
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
            up_fill_price=0.25,
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

        assert result["status"] == "WAIT_RESOLUTION"
        waiting = cycle_for_condition(
            conn,
            scope="PAPER",
            asset="XRP",
            condition_id="paper-condition",
        )
        assert waiting is not None
        assert waiting["status"] == "WAIT_RESOLUTION"
        assert waiting["official_result"] is None
        assert waiting["realized_pnl_usdc"] is None
        assert waiting["details"]["paper_waits_for_official_result"] is True
        recovery = ladder_state(conn, "PAPER", "XRP")
        assert recovery["level_index"] == 0
        assert recovery["loss_pool_usdc"] == pytest.approx(0.0)
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


def test_paper_wait_resolution_settles_winning_leg_at_recorded_price(
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
            down_fill_price=0.25,
            residual_side="DOWN",
            residual_shares=5.0,
        )
        cycle = active_cycle(conn, scope="PAPER", asset="XRP")
        assert cycle is not None
        monkeypatch.setattr(
            engine,
            "_fetch_official_result",
            lambda _cycle: ("DOWN", "TEST_OFFICIAL"),
        )

        p26 = sqlite3.connect(settings.p26_db_path)
        p26.row_factory = sqlite3.Row
        try:
            result = engine._resolution_tick(conn, cycle, now_ms, p26=p26)
        finally:
            p26.close()

        assert result["status"] == "RESOLVED_DOWN"
        assert result["pnl_usdc"] == pytest.approx(3.75)
        assert active_cycle(conn, scope="PAPER", asset="XRP") is None
        settled = cycle_for_condition(
            conn,
            scope="PAPER",
            asset="XRP",
            condition_id="paper-condition",
        )
        assert settled is not None
        assert settled["official_result"] == "DOWN"
        assert settled["details"]["paper_settlement_rule"] == (
            "OFFICIAL_RESULT_ACTUAL_FILL_PRICE"
        )
        assert ladder_state(conn, "PAPER", "XRP")["loss_pool_usdc"] == 0.0
    finally:
        conn.close()


def test_paper_wait_resolution_advances_ladder_only_after_actual_loss(
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
            up_filled_shares=5.0,
            up_fill_price=0.25,
            residual_side="UP",
            residual_shares=5.0,
        )
        cycle = active_cycle(conn, scope="PAPER", asset="XRP")
        assert cycle is not None
        monkeypatch.setattr(
            engine,
            "_fetch_official_result",
            lambda _cycle: ("DOWN", "TEST_OFFICIAL"),
        )

        p26 = sqlite3.connect(settings.p26_db_path)
        p26.row_factory = sqlite3.Row
        try:
            result = engine._resolution_tick(conn, cycle, now_ms, p26=p26)
        finally:
            p26.close()

        assert result["status"] == "RESOLVED_DOWN"
        assert result["pnl_usdc"] == pytest.approx(-1.25)
        recovery = ladder_state(conn, "PAPER", "XRP")
        assert recovery["level_index"] == 0
        assert recovery["loss_pool_usdc"] == 0.0
    finally:
        conn.close()


def test_wait_resolution_records_unresolved_source(tmp_path, monkeypatch):
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
            down_fill_price=0.25,
            residual_side="DOWN",
            residual_shares=5.0,
        )
        cycle = active_cycle(conn, scope="PAPER", asset="XRP")
        assert cycle is not None
        monkeypatch.setattr(
            engine,
            "_fetch_official_result",
            lambda _cycle: (None, "CONDITION_NOT_FOUND"),
        )

        result = engine._resolution_tick(conn, cycle, now_ms)

        assert result["status"] == "WAIT_RESOLUTION"
        waiting = active_cycle(conn, scope="PAPER", asset="XRP")
        assert waiting is not None
        assert waiting["details"]["last_resolution_source"] == "CONDITION_NOT_FOUND"
        assert waiting["details"]["last_resolution_attempt_ms"] == now_ms
        assert waiting["details"]["resolution_attempts"] == 1
        assert waiting["details"]["resolution_wait_age_sec"] == pytest.approx(3.0)
    finally:
        conn.close()


def test_wait_resolution_records_fetch_error(tmp_path, monkeypatch):
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
            status="WAIT_RESOLUTION",
            market_end_ts_ms=now_ms - 3_000,
        )
        update_cycle(
            conn,
            1,
            down_filled_shares=5.0,
            down_fill_price=0.25,
            residual_side="DOWN",
            residual_shares=5.0,
        )
        cycle = active_cycle(conn, scope="PAPER", asset="XRP")
        assert cycle is not None

        def broken(_cycle):
            raise TimeoutError("gamma timed out")

        monkeypatch.setattr(engine, "_fetch_official_result", broken)

        result = engine._resolution_tick(conn, cycle, now_ms)

        assert result["status"] == "WAIT_RESOLUTION"
        waiting = active_cycle(conn, scope="PAPER", asset="XRP")
        assert waiting is not None
        assert waiting["details"]["last_resolution_error"]["type"] == "TimeoutError"
        assert waiting["details"]["last_resolution_attempt_ms"] == now_ms
    finally:
        conn.close()


def test_official_result_uses_p26_slug_when_condition_filter_lags(
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
            down_fill_price=0.25,
            residual_side="DOWN",
            residual_shares=5.0,
        )
        p26 = sqlite3.connect(settings.p26_db_path)
        try:
            p26.execute(
                """
                INSERT INTO p26_canonical_rows(
                    condition_id,market_id,slug,combo_key,asset,horizon,
                    market_start_ts_ms,market_end_ts_ms,checkpoint_sec,
                    nominal_target_ts_ms,decision_ts_ms,capture_lag_ms,
                    source_snapshot_id,feature_vector_json,feature_names_json,
                    feature_vector_sha256,feature_schema_version,feature_schema_hash,
                    extraction_policy_version,quality_status,lineage_status,
                    training_eligible,lineage_json,code_commit,created_at_ms
                )
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "paper-condition",
                    "123",
                    "xrp-updown-5m-test",
                    "XRP:5m",
                    "XRP",
                    "5m",
                    now_ms - 300_000,
                    now_ms - 3_000,
                    60,
                    now_ms,
                    now_ms,
                    0,
                    1,
                    "{}",
                    "[]",
                    "sha",
                    "v1",
                    "hash",
                    "policy",
                    "OK",
                    "OK",
                    1,
                    "{}",
                    "test",
                    now_ms,
                ),
            )
            p26.commit()
        finally:
            p26.close()

        def fake_gamma_json(url):
            assert "condition_ids" not in url
            if url.endswith("/markets/123"):
                return {"conditionId": "other-condition"}
            if url.endswith("/events/slug/xrp-updown-5m-test"):
                return {
                    "markets": [
                        {
                            "conditionId": "paper-condition",
                            "umaResolutionStatus": "resolved",
                            "closed": True,
                            "outcomes": json.dumps(["Up", "Down"]),
                            "clobTokenIds": json.dumps(["up-token", "down-token"]),
                            "outcomePrices": json.dumps(["1", "0"]),
                        }
                    ]
                }
            raise AssertionError(url)

        monkeypatch.setattr(engine, "_gamma_json", fake_gamma_json)
        cycle = active_cycle(conn, scope="PAPER", asset="XRP")
        assert cycle is not None

        result = engine._resolution_tick(conn, cycle, now_ms)

        assert result["status"] == "RESOLVED_UP"
        settled = cycle_for_condition(
            conn,
            scope="PAPER",
            asset="XRP",
            condition_id="paper-condition",
        )
        assert settled is not None
        assert settled["official_result"] == "UP"
        assert settled["details"]["official_result_source"].startswith(
            "event_slug:"
        )
    finally:
        conn.close()


def test_official_result_uses_p25_market_record_when_p26_label_lags(
    tmp_path,
    monkeypatch,
):
    settings = _settings(tmp_path)
    engine = ProductionDual40MakerEngine(
        settings,
        LiveState(live_feature_enabled=True, auto_execute_enabled=True),
        gateway_factory=lambda _: object(),
    )
    p25_path = tmp_path / "p25.sqlite"
    p25 = sqlite3.connect(p25_path)
    try:
        p25.execute(
            """
            CREATE TABLE markets(
              condition_id TEXT PRIMARY KEY,market_id TEXT,slug TEXT,
              official_result TEXT,official_result_source TEXT,
              official_resolved_at REAL
            )
            """
        )
        p25.execute(
            "INSERT INTO markets VALUES(?,?,?,?,?,?)",
            (
                "paper-condition",
                "789",
                "btc-updown-5m-test",
                "UP",
                "unit-test",
                1_800_000_000.0,
            ),
        )
        p25.commit()
    finally:
        p25.close()
    monkeypatch.setattr(engine, "_p25_db_path", lambda: str(p25_path))

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
            up_filled_shares=5.0,
            up_fill_price=0.25,
            residual_side="UP",
            residual_shares=5.0,
        )
        cycle = active_cycle(conn, scope="PAPER", asset="XRP")
        assert cycle is not None

        result = engine._resolution_tick(conn, cycle, now_ms)

        assert result["status"] == "RESOLVED_UP"
        settled = cycle_for_condition(
            conn,
            scope="PAPER",
            asset="XRP",
            condition_id="paper-condition",
        )
        assert settled is not None
        assert settled["official_result"] == "UP"
        assert settled["details"]["official_result_source"] == "P25:unit-test"
    finally:
        conn.close()


def test_finalize_uses_current_loss_pool_when_cycle_was_opened_from_stale_state(
    tmp_path,
):
    settings = _settings(tmp_path)
    engine = ProductionDual40MakerEngine(
        settings,
        LiveState(live_feature_enabled=True, auto_execute_enabled=True),
        gateway_factory=lambda _: object(),
    )
    conn = connect_dual40(settings.p3_db_path)
    try:
        now_ms = int(time.time() * 1000)
        cycle_id = _create_paper_cycle(
            conn,
            now_ms=now_ms,
            status="WAIT_RESOLUTION",
            market_end_ts_ms=now_ms - 3_000,
        )
        set_ladder_state(
            conn,
            scope="PAPER",
            asset="XRP",
            level_index=1,
            loss_pool_usdc=0.95,
            hard_stopped=False,
            hard_stop_reason=None,
        )
        cycle = cycle_for_condition(
            conn,
            scope="PAPER",
            asset="XRP",
            condition_id="paper-condition",
        )
        assert cycle is not None
        assert cycle["loss_pool_before_usdc"] == pytest.approx(0.0)

        result = engine._apply_ladder_and_finalize(
            conn,
            cycle=cycle,
            status="RESOLVED_UP",
            pnl=-0.50,
            official_result="UP",
        )

        assert result["status"] == "RESOLVED_UP"
        recovery = ladder_state(conn, "PAPER", "XRP")
        assert recovery["loss_pool_usdc"] == 0.0
        assert recovery["level_index"] == 0
        settled = cycle_for_condition(
            conn,
            scope="PAPER",
            asset="XRP",
            condition_id="paper-condition",
        )
        assert settled is not None
        assert settled["loss_pool_after_usdc"] == pytest.approx(0.0)
    finally:
        conn.close()


def test_paper_hard_stop_terminal_loss_starts_new_base_series(tmp_path):
    settings = _settings(tmp_path)
    engine = ProductionDual40MakerEngine(
        settings,
        LiveState(live_feature_enabled=True, auto_execute_enabled=True),
        gateway_factory=lambda _: object(),
    )
    conn = connect_dual40(settings.p3_db_path)
    try:
        now_ms = int(time.time() * 1000)
        cycle_id = _create_paper_cycle(
            conn,
            now_ms=now_ms,
            status="WAIT_RESOLUTION",
            market_end_ts_ms=now_ms - 3_000,
        )
        update_cycle(conn, cycle_id, level_index=2, loss_pool_before_usdc=20.0)
        cycle = cycle_for_condition(
            conn,
            scope="PAPER",
            asset="XRP",
            condition_id="paper-condition",
        )
        assert cycle is not None

        result = engine._apply_ladder_and_finalize(
            conn,
            cycle=cycle,
            status="RESOLVED_DOWN",
            pnl=-12.0,
            official_result="DOWN",
        )

        assert result["ladder"]["hard_stopped"] is True
        assert result["ladder"]["state_hard_stopped"] is False
        recovery = ladder_state(conn, "PAPER", "XRP")
        assert recovery["hard_stopped"] == 0
        assert recovery["level_index"] == 0
        assert recovery["loss_pool_usdc"] == pytest.approx(0.0)
        settled = cycle_for_condition(
            conn,
            scope="PAPER",
            asset="XRP",
            condition_id="paper-condition",
        )
        assert settled is not None
        assert settled["loss_pool_after_usdc"] == pytest.approx(0.0)
        assert settled["details"]["paper_ladder_action"] == "RESET_TO_BASE_NO_MARTINGALE"
        assert settled["details"]["paper_unrecovered_loss_pool_usdc"] > 0
    finally:
        conn.close()


def test_paper_alternate_market_skip_toggles_after_opened_market(tmp_path):
    settings = _settings(tmp_path)
    engine = ProductionDual40MakerEngine(
        settings,
        LiveState(live_feature_enabled=True, auto_execute_enabled=True),
        gateway_factory=lambda _: object(),
    )
    conn = connect_dual40(settings.p3_db_path)
    try:
        now_ms = int(time.time() * 1000)
        upsert_market_decision(
            conn,
            scope="PAPER",
            asset="BTC",
            combo_key="BTC:5m",
            condition_id="opened-market",
            market_start_ts_ms=now_ms - 300_000,
            market_end_ts_ms=now_ms,
            decision="OPENED",
            reason="PAPER_RELAXED_LIMIT_READY",
            eligible=True,
        )
        assert engine._paper_alternate_market_skip_due(
            conn,
            asset="BTC",
            market_end_ts_ms=now_ms + 300_000,
        )

        upsert_market_decision(
            conn,
            scope="PAPER",
            asset="BTC",
            combo_key="BTC:5m",
            condition_id="skipped-market",
            market_start_ts_ms=now_ms,
            market_end_ts_ms=now_ms + 300_000,
            decision="PAPER_ALTERNATE_MARKET_SKIP",
            reason="PAPER_ALTERNATE_MARKET_SKIP",
            eligible=False,
        )
        assert not engine._paper_alternate_market_skip_due(
            conn,
            asset="BTC",
            market_end_ts_ms=now_ms + 600_000,
        )
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
