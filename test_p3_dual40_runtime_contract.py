from __future__ import annotations

import sqlite3

import pytest

from p26_book_store import BookSnapshotStore
from p26_execution import OrderBookSnapshot
from p3_config import DUAL40_MODE, P3Settings
from p3_dual40_runtime import ProductionDual40MakerEngine
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
