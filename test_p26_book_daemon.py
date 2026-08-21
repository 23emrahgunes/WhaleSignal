import sqlite3
import time

from p26_book_daemon import BookCollector, load_active_markets
from p26_config import P26Settings


def _settings(tmp_path, p25):
    return P26Settings(
        p25_db_path=str(p25),
        p26_db_path=str(tmp_path / "p26.sqlite"),
        book_persist_min_interval_ms=0,
    )


def test_active_market_discovery_and_book_delta_persistence(tmp_path):
    p25 = tmp_path / "p25.sqlite"
    conn = sqlite3.connect(p25)
    conn.execute(
        """
        CREATE TABLE markets(
            condition_id TEXT PRIMARY KEY,combo_key TEXT,market_end REAL,resolved INTEGER
        )
        """
    )
    now_ms = int(time.time() * 1000)
    conn.execute(
        "INSERT INTO markets VALUES(?,?,?,0)",
        ("cond", "BTC:5m", (now_ms + 300_000) / 1000),
    )
    conn.commit(); conn.close()
    assert load_active_markets(str(p25), now_ms=now_ms)[0].condition_id == "cond"

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
