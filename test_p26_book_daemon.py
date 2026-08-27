import asyncio
import json
import sqlite3
import time

from p26_book_daemon import BookCollector, load_active_markets
from p26_config import P26Settings


def _settings(tmp_path, p25, *, persist_ms=0):
    return P26Settings(
        p25_db_path=str(p25),
        p26_db_path=str(tmp_path / "p26.sqlite"),
        book_persist_min_interval_ms=persist_ms,
    )


def _market_db(path, now_ms):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE markets(
            condition_id TEXT PRIMARY KEY,combo_key TEXT,
            market_start REAL,market_end REAL,resolved INTEGER
        )
        """
    )
    conn.execute(
        "INSERT INTO markets VALUES(?,?,?,?,0)",
        ("cond", "BTC:5m", (now_ms - 60_000) / 1000, (now_ms + 300_000) / 1000),
    )
    conn.commit()
    return conn


def test_active_market_discovery_and_book_delta_persistence(tmp_path):
    p25 = tmp_path / "p25.sqlite"
    now_ms = int(time.time() * 1000)
    conn = _market_db(p25, now_ms)
    conn.close()
    market = load_active_markets(str(p25), now_ms=now_ms)[0]
    assert market.condition_id == "cond"
    assert market.active_at(now_ms) is True

    collector = BookCollector(_settings(tmp_path, p25))
    try:
        collector.fees.upsert_market_info(
            condition_id="cond", combo_key="BTC:5m",
            market_end_ts_ms=now_ms + 300_000,
            source_ts_ms=now_ms,
            payload={
                "fd": None,
                "t": [{"t": "up", "o": "Up"}, {"t": "down", "o": "Down"}],
            },
        )
        collector.token_meta = {
            "up": ("cond", "BTC:5m", "UP"),
            "down": ("cond", "BTC:5m", "DOWN"),
        }
        collector.handle_event(
            {
                "event_type": "book", "asset_id": "up", "timestamp": now_ms,
                "bids": [{"price": "0.49", "size": "10"}],
                "asks": [{"price": "0.51", "size": "10"}],
            },
            recv_ms=now_ms,
        )
        collector.handle_event(
            {
                "event_type": "price_change", "timestamp": now_ms + 100,
                "price_changes": [
                    {"asset_id": "up", "side": "SELL", "price": "0.51", "size": "0"},
                    {"asset_id": "up", "side": "SELL", "price": "0.52", "size": "12"},
                ],
            },
            recv_ms=now_ms + 100,
        )
        history = collector.books.history(
            "cond", "UP", start_ts_ms=now_ms - 1, end_ts_ms=now_ms + 200
        )
        assert len(history) == 2
        assert history[-1].best_ask == 0.52
        assert collector.fees.get("cond", "up").enabled is False
    finally:
        collector.close()


def test_empty_ask_transition_is_persisted_immediately_even_inside_throttle(tmp_path):
    """A removed ask side must replace stale executable liquidity in P26 truth."""
    p25 = tmp_path / "p25.sqlite"
    now_ms = int(time.time() * 1000)
    conn = _market_db(p25, now_ms)
    conn.close()
    collector = BookCollector(_settings(tmp_path, p25, persist_ms=10_000))
    try:
        collector.token_meta = {"up": ("cond", "BTC:5m", "UP")}
        collector.handle_event(
            {
                "event_type": "book",
                "asset_id": "up",
                "timestamp": now_ms,
                "bids": [{"price": "0.49", "size": "10"}],
                "asks": [{"price": "0.51", "size": "10"}],
            },
            recv_ms=now_ms,
        )
        # Only 1ms later the entire executable ask side disappears. The normal
        # 10s history throttle must not hide this risk-critical state transition.
        collector.handle_event(
            {
                "event_type": "price_change",
                "timestamp": now_ms + 1,
                "price_changes": [
                    {"asset_id": "up", "side": "SELL", "price": "0.51", "size": "0"},
                ],
            },
            recv_ms=now_ms + 1,
        )
        history = collector.books.history(
            "cond", "UP", start_ts_ms=now_ms - 1, end_ts_ms=now_ms + 10
        )
        assert len(history) == 2
        assert history[0].best_ask == 0.51
        assert history[-1].asks == ()
        assert history[-1].best_ask is None
        assert collector.last_persist_ms["up"] == now_ms + 1
    finally:
        collector.close()


def test_near_future_market_is_prefetched_but_not_currently_active(tmp_path, monkeypatch):
    p25 = tmp_path / "p25.sqlite"
    now_ms = 2_000_000
    conn = sqlite3.connect(p25)
    conn.execute(
        """
        CREATE TABLE markets(
            condition_id TEXT PRIMARY KEY,combo_key TEXT,
            market_start REAL,market_end REAL,resolved INTEGER
        )
        """
    )
    rows = [
        ("active", "BTC:5m", (now_ms - 10_000) / 1000, (now_ms + 290_000) / 1000, 0),
        ("future", "BTC:5m", (now_ms + 60_000) / 1000, (now_ms + 360_000) / 1000, 0),
        ("far", "BTC:5m", (now_ms + 600_000) / 1000, (now_ms + 900_000) / 1000, 0),
    ]
    conn.executemany("INSERT INTO markets VALUES(?,?,?,?,?)", rows)
    conn.commit(); conn.close()

    markets = load_active_markets(str(p25), now_ms=now_ms, prefetch_ms=120_000)
    assert {market.condition_id for market in markets} == {"active", "future"}
    assert {market.condition_id for market in markets if market.active_at(now_ms)} == {"active"}


def test_book_health_meta_records_live_transport_and_session(tmp_path):
    p25 = tmp_path / "p25.sqlite"
    now_ms = int(time.time() * 1000)
    conn = _market_db(p25, now_ms); conn.close()
    collector = BookCollector(_settings(tmp_path, p25))
    try:
        collector.token_meta = {
            "up": ("cond", "BTC:5m", "UP"),
            "down": ("cond", "BTC:5m", "DOWN"),
        }
        collector.active_condition_count = 1
        collector.collected_condition_count = 1
        collector._write_health(
            connected=True,
            session_started_ms=now_ms,
            last_message_recv_ms=now_ms + 10,
            force=True,
        )
        row = collector.fees.conn.execute(
            "SELECT value FROM p26_meta WHERE key='book_collector_health_json'"
        ).fetchone()
        payload = json.loads(row[0])
        assert payload["connected"] is True
        assert payload["session_started_ms"] == now_ms
        assert payload["subscribed_tokens"] == 2
        assert payload["active_conditions"] == 1
    finally:
        collector.close()
