import time

from p26_book_store import BookSnapshotStore
from p26_execution import OrderBookSnapshot
from p26_fee import FeeScheduleStore
from p3_config import P3Settings
from p3_scanner_resilient import (
    MIN_ROTATION_SESSION_AGE_MS,
    RECONNECT_GRACE_MS,
    ReconnectAwareStructuralArbScanner,
)


def _settings(tmp_path):
    return P3Settings(
        p26_db_path=str(tmp_path / "p26.sqlite"),
        p3_db_path=str(tmp_path / "p3.sqlite"),
        reports_dir=str(tmp_path / "reports"),
        scan_interval_ms=250,
        web_port=18093,
    )


def test_planned_rotation_grace_does_not_weaken_dead_transport_gate(tmp_path):
    scanner = object.__new__(ReconnectAwareStructuralArbScanner)
    scanner.settings = _settings(tmp_path)
    now = int(time.time() * 1000)

    assert scanner._transport_current_or_planned_rotation(
        {
            "connected": True,
            "heartbeat_ts_ms": now,
            "session_started_ms": now - 1_000,
            "last_message_recv_ms": 0,
        },
        now,
    )
    assert scanner._transport_current_or_planned_rotation(
        {
            "connected": False,
            "heartbeat_ts_ms": now,
            "session_started_ms": now - MIN_ROTATION_SESSION_AGE_MS - 5_000,
            "last_message_recv_ms": now - 100,
        },
        now,
    )
    assert not scanner._transport_current_or_planned_rotation(
        {
            "connected": False,
            "heartbeat_ts_ms": now,
            "session_started_ms": now - 1_000,
            "last_message_recv_ms": now - 100,
        },
        now,
    )
    assert not scanner._transport_current_or_planned_rotation(
        {
            "connected": False,
            "heartbeat_ts_ms": now,
            "session_started_ms": now - MIN_ROTATION_SESSION_AGE_MS - 5_000,
            "last_message_recv_ms": now - RECONNECT_GRACE_MS - 1,
        },
        now,
    )


def test_runtime_latest_book_uses_last_observation_time(tmp_path):
    settings = _settings(tmp_path)
    fees = FeeScheduleStore(settings.p26_db_path)
    fees.upsert_market_info(
        condition_id="cond",
        combo_key="BTC:5m",
        market_end_ts_ms=20_000,
        source_ts_ms=1_000,
        payload={
            "t": [{"t": "up", "o": "UP"}, {"t": "down", "o": "DOWN"}],
            "fd": None,
        },
    )
    fees.close()

    store = BookSnapshotStore(settings.p26_db_path)
    store.insert(
        condition_id="cond",
        combo_key="BTC:5m",
        side="UP",
        snapshot=OrderBookSnapshot.from_levels(
            token_id="up", ts_ms=2_000,
            bids=[(0.49, 10)], asks=[(0.51, 10)],
        ),
        recv_ts_ms=3_000,
    )
    store.insert(
        condition_id="cond",
        combo_key="BTC:5m",
        side="UP",
        snapshot=OrderBookSnapshot.from_levels(
            token_id="up", ts_ms=1_500,
            bids=[(0.48, 10)], asks=[(0.52, 10)],
        ),
        recv_ts_ms=9_000,
    )
    store.close()

    scanner = ReconnectAwareStructuralArbScanner(settings)
    try:
        row = scanner._latest_book("cond", "UP")
        assert row is not None
        assert int(row["source_ts_ms"]) == 1_500
        assert int(row["recv_ts_ms"]) == 9_000
    finally:
        scanner.close()
