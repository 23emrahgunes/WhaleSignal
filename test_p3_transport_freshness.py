from __future__ import annotations

import json
from pathlib import Path

from p26_book_store import BookSnapshotStore
from p26_execution import OrderBookSnapshot
from p26_fee import FeeScheduleStore
from p3_config import P3Settings
from p3_scanner import StructuralArbScanner


def _settings(tmp_path: Path) -> P3Settings:
    return P3Settings(
        p26_db_path=str(tmp_path / "p26.sqlite"),
        p3_db_path=str(tmp_path / "p3.sqlite"),
        reports_dir=str(tmp_path / "reports"),
        scan_interval_ms=250,
        max_book_age_ms=750,
        max_source_skew_ms=500,
        replay_delays_ms="100",
        web_port=18093,
    )


def _seed(
    path: str,
    *,
    source_up: int,
    source_down: int,
    recv_up: int,
    recv_down: int,
    heartbeat: dict | None,
) -> None:
    fees = FeeScheduleStore(path)
    fees.upsert_market_info(
        condition_id="cond",
        combo_key="BTC:5m",
        market_end_ts_ms=max(recv_up, recv_down) + 300_000,
        source_ts_ms=max(recv_up, recv_down),
        payload={
            "t": [{"t": "up", "o": "UP"}, {"t": "down", "o": "DOWN"}],
            "fd": None,
        },
        source="TEST",
    )
    if heartbeat is not None:
        ts = int(heartbeat["heartbeat_ts_ms"])
        fees.conn.execute(
            """
            INSERT INTO p26_meta(key,value,updated_at_ms) VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at_ms=excluded.updated_at_ms
            """,
            ("book_collector_health_json", json.dumps(heartbeat), ts),
        )
        fees.conn.commit()
    fees.close()

    books = BookSnapshotStore(path)
    books.insert(
        condition_id="cond",
        combo_key="BTC:5m",
        side="UP",
        snapshot=OrderBookSnapshot.from_levels(
            token_id="up", ts_ms=source_up,
            bids=[(0.39, 10)], asks=[(0.40, 10)],
        ),
        recv_ts_ms=recv_up,
    )
    books.insert(
        condition_id="cond",
        combo_key="BTC:5m",
        side="DOWN",
        snapshot=OrderBookSnapshot.from_levels(
            token_id="down", ts_ms=source_down,
            bids=[(0.49, 10)], asks=[(0.50, 10)],
        ),
        recv_ts_ms=recv_down,
    )
    books.close()


def test_live_transport_accepts_unchanged_old_resting_quotes(tmp_path):
    s = _settings(tmp_path)
    now = 1_000_000
    session = now - 2_000
    _seed(
        s.p26_db_path,
        source_up=now - 120_000,
        source_down=now - 90_000,
        recv_up=now - 1_500,
        recv_down=now - 1_400,
        heartbeat={
            "connected": True,
            "heartbeat_ts_ms": now - 100,
            "session_started_ms": session,
            "last_message_recv_ms": now - 1_000,
            "subscribed_tokens": 2,
            "active_conditions": 1,
            "collected_conditions": 1,
        },
    )
    scanner = StructuralArbScanner(s)
    try:
        stats = scanner.scan_once(now_ms=now)
        assert stats.conditions == 1
        assert stats.valid_pairs == 1
        assert stats.transport_stale == 0
        assert stats.session_incomplete == 0
        assert stats.high_source_skew == 1
        assert stats.positive_buy_merge == 1
    finally:
        scanner.close()


def test_current_socket_requires_both_books_seen_in_current_session(tmp_path):
    s = _settings(tmp_path)
    now = 2_000_000
    session = now - 1_000
    _seed(
        s.p26_db_path,
        source_up=now - 5_000,
        source_down=now - 5_000,
        recv_up=now - 2_000,
        recv_down=now - 500,
        heartbeat={
            "connected": True,
            "heartbeat_ts_ms": now - 100,
            "session_started_ms": session,
            "last_message_recv_ms": now - 500,
            "subscribed_tokens": 2,
            "active_conditions": 1,
            "collected_conditions": 1,
        },
    )
    scanner = StructuralArbScanner(s)
    try:
        stats = scanner.scan_once(now_ms=now)
        assert stats.valid_pairs == 0
        assert stats.session_incomplete == 1
    finally:
        scanner.close()


def test_dead_transport_rejects_even_recent_book_rows(tmp_path):
    s = _settings(tmp_path)
    now = 3_000_000
    _seed(
        s.p26_db_path,
        source_up=now - 100,
        source_down=now - 100,
        recv_up=now - 100,
        recv_down=now - 100,
        heartbeat={
            "connected": False,
            "heartbeat_ts_ms": now - 100,
            "session_started_ms": now - 1_000,
            "last_message_recv_ms": now - 100,
            "subscribed_tokens": 2,
            "active_conditions": 1,
            "collected_conditions": 1,
        },
    )
    scanner = StructuralArbScanner(s)
    try:
        stats = scanner.scan_once(now_ms=now)
        assert stats.valid_pairs == 0
        assert stats.transport_stale == 1
    finally:
        scanner.close()
