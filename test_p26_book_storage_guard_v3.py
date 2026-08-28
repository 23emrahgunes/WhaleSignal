from __future__ import annotations

import time

from p26_book_daemon_resilient_v3 import (
    ResilientBookCollectorV3,
    prune_book_history,
)
from p26_book_store import BookSnapshotStore
from p26_execution import OrderBookSnapshot
from test_p26_book_daemon import _market_db, _settings


def test_repeated_empty_ask_is_not_persisted_until_state_recovers(tmp_path):
    p25 = tmp_path / "p25.sqlite"
    now = int(time.time() * 1000)
    c = _market_db(p25, now)
    c.close()
    collector = ResilientBookCollectorV3(_settings(tmp_path, p25, persist_ms=10_000))
    try:
        collector.token_meta = {"up": ("cond", "BTC:5m", "UP")}
        collector.handle_event(
            {
                "event_type": "book",
                "asset_id": "up",
                "timestamp": now,
                "bids": [{"price": "0.49", "size": "10"}],
                "asks": [{"price": "0.51", "size": "10"}],
            },
            recv_ms=now,
        )
        # Risk-critical transition: must bypass the 10s throttle.
        collector.handle_event(
            {
                "event_type": "price_change",
                "timestamp": now + 1,
                "price_changes": [
                    {"asset_id": "up", "side": "SELL", "price": "0.51", "size": "0"}
                ],
            },
            recv_ms=now + 1,
        )
        # Still empty. A bid-only event must not create another full-depth history row.
        collector.handle_event(
            {
                "event_type": "price_change",
                "timestamp": now + 2,
                "price_changes": [
                    {"asset_id": "up", "side": "BUY", "price": "0.49", "size": "11"}
                ],
            },
            recv_ms=now + 2,
        )
        # Recovery is also a state transition and must persist immediately.
        collector.handle_event(
            {
                "event_type": "price_change",
                "timestamp": now + 3,
                "price_changes": [
                    {"asset_id": "up", "side": "SELL", "price": "0.52", "size": "12"}
                ],
            },
            recv_ms=now + 3,
        )
        rows = collector.books.conn.execute(
            "SELECT asks_json FROM p26_clob_books WHERE token_id='up' ORDER BY id"
        ).fetchall()
        assert len(rows) == 3
        history = collector.books.history(
            "cond", "UP", start_ts_ms=now - 1, end_ts_ms=now + 10
        )
        assert history[0].best_ask == 0.51
        assert history[1].best_ask is None
        assert history[2].best_ask == 0.52
    finally:
        collector.close()


def test_session_seed_persists_explicit_empty_ask_truth(tmp_path):
    p25 = tmp_path / "p25.sqlite"
    now = int(time.time() * 1000)
    c = _market_db(p25, now)
    c.close()
    collector = ResilientBookCollectorV3(_settings(tmp_path, p25, persist_ms=100))
    try:
        collector.token_meta = {"up": ("cond", "BTC:5m", "UP")}
        seeded, missing = collector._apply_session_seed(
            [
                {
                    "asset_id": "up",
                    "timestamp": now,
                    "bids": [{"price": "0.49", "size": "10"}],
                    "asks": [],
                }
            ],
            recv_ms=now + 10,
            session_started_ms=now + 5,
        )
        assert seeded == 1
        assert missing == 0
        row = collector.books.conn.execute(
            "SELECT asks_json,recv_ts_ms FROM p26_clob_books WHERE token_id='up'"
        ).fetchone()
        assert row is not None
        assert row["asks_json"] == "[]"
        assert int(row["recv_ts_ms"]) == now + 10
        assert collector.last_empty_ask_state["up"] is True
    finally:
        collector.close()


def test_15m_prune_uses_observation_time_and_keeps_latest_per_token(tmp_path):
    db = str(tmp_path / "p26.sqlite")
    now = 2_000_000_000_000
    store = BookSnapshotStore(db)
    try:
        def add(token: str, source: int, recv: int, ask: float) -> None:
            snap = OrderBookSnapshot.from_levels(
                token_id=token,
                ts_ms=source,
                bids=[(ask - 0.01, 10.0)],
                asks=[(ask, 10.0)],
            )
            store.insert(
                condition_id="cond-" + token,
                combo_key="BTC:5m",
                side="UP",
                snapshot=snap,
                recv_ts_ms=recv,
            )

        add("moving", now - 30 * 60_000, now - 20 * 60_000, 0.30)
        add("moving", now - 20 * 60_000, now - 10 * 60_000, 0.31)
        add("moving", now - 10 * 60_000, now - 1 * 60_000, 0.32)
        # Old but sole/latest observation: retention must fail closed by keeping it.
        add("resting", now - 60 * 60_000, now - 30 * 60_000, 0.40)
    finally:
        store.close()

    deleted = prune_book_history(db, now_ms=now, retention_ms=15 * 60_000)
    assert deleted == 1

    store = BookSnapshotStore(db)
    try:
        moving = store.conn.execute(
            "SELECT recv_ts_ms FROM p26_clob_books WHERE token_id='moving' ORDER BY recv_ts_ms"
        ).fetchall()
        resting = store.conn.execute(
            "SELECT recv_ts_ms FROM p26_clob_books WHERE token_id='resting'"
        ).fetchall()
        assert [int(row[0]) for row in moving] == [
            now - 10 * 60_000,
            now - 1 * 60_000,
        ]
        assert len(resting) == 1
    finally:
        store.close()
