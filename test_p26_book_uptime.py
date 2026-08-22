import asyncio
import sqlite3
import time

import pytest

from p26_book_daemon_resilient import ResilientBookCollector
from p26_config import P26Settings


def _settings(tmp_path, *, refresh_sec=1):
    p25 = tmp_path / "p25.sqlite"
    conn = sqlite3.connect(p25)
    conn.execute(
        """
        CREATE TABLE markets(
            condition_id TEXT PRIMARY KEY,combo_key TEXT,
            market_start REAL,market_end REAL,resolved INTEGER
        )
        """
    )
    now = time.time()
    conn.execute(
        "INSERT INTO markets VALUES(?,?,?,?,0)",
        ("cond", "BTC:5m", now - 10, now + 300),
    )
    conn.commit(); conn.close()
    return P26Settings(
        p25_db_path=str(p25),
        p26_db_path=str(tmp_path / "p26.sqlite"),
        book_market_refresh_sec=refresh_sec,
        book_persist_min_interval_ms=0,
    )


class _TimeoutWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)

    async def receive(self):
        await asyncio.sleep(0.02)
        raise asyncio.TimeoutError


class _WSContext:
    def __init__(self, ws):
        self.ws = ws
        self.enter_count = 0
        self.exit_count = 0

    async def __aenter__(self):
        self.enter_count += 1
        return self.ws

    async def __aexit__(self, exc_type, exc, tb):
        self.exit_count += 1
        return False


class _FakeSession:
    def __init__(self):
        self.ws = _TimeoutWS()
        self.context = _WSContext(self.ws)
        self.connect_calls = 0

    def ws_connect(self, *args, **kwargs):
        self.connect_calls += 1
        return self.context


@pytest.mark.asyncio
async def test_unchanged_registry_does_not_force_periodic_socket_rotation(tmp_path, monkeypatch):
    collector = ResilientBookCollector(_settings(tmp_path, refresh_sec=1))
    collector.token_meta = {
        "up": ("cond", "BTC:5m", "UP"),
        "down": ("cond", "BTC:5m", "DOWN"),
    }
    refresh_calls = 0

    async def unchanged(_session):
        nonlocal refresh_calls
        refresh_calls += 1
        collector._write_health(connected=True, force=True)
        return False

    monkeypatch.setattr(collector, "refresh_registry", unchanged)
    session = _FakeSession()
    stop = asyncio.Event()

    async def stop_later():
        await asyncio.sleep(1.25)
        stop.set()

    stopper = asyncio.create_task(stop_later())
    try:
        await collector.run_socket(session, stop)
        await stopper
        assert refresh_calls >= 1
        assert session.connect_calls == 1
        assert session.context.enter_count == 1
        assert session.context.exit_count == 1
    finally:
        collector.close()


@pytest.mark.asyncio
async def test_registry_token_change_requests_controlled_reconnect(tmp_path, monkeypatch):
    collector = ResilientBookCollector(_settings(tmp_path, refresh_sec=1))
    collector.token_meta = {
        "up": ("cond", "BTC:5m", "UP"),
        "down": ("cond", "BTC:5m", "DOWN"),
    }
    refresh_calls = 0

    async def changed(_session):
        nonlocal refresh_calls
        refresh_calls += 1
        collector.token_meta["future-up"] = ("future", "BTC:5m", "UP")
        collector.token_meta["future-down"] = ("future", "BTC:5m", "DOWN")
        return True

    monkeypatch.setattr(collector, "refresh_registry", changed)
    session = _FakeSession()
    stop = asyncio.Event()
    started = time.monotonic()
    try:
        await collector.run_socket(session, stop)
        elapsed = time.monotonic() - started
        assert refresh_calls == 1
        assert session.connect_calls == 1
        assert not stop.is_set()
        assert elapsed < 2.0
    finally:
        collector.close()
