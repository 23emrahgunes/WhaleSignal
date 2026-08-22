import asyncio
import sqlite3
import time

import pytest

from p26_book_daemon_resilient_v2 import ResilientBookCollectorV2
from p26_config import P26Settings
from scripts.p26_book_watchdog import evaluate_health


def _settings(tmp_path):
    return P26Settings(
        p25_db_path=str(tmp_path / "p25.sqlite"),
        p26_db_path=str(tmp_path / "p26.sqlite"),
        book_persist_min_interval_ms=0,
    )


def test_watchdog_accepts_fresh_live_heartbeat():
    now = 10_000
    healthy, reason, age = evaluate_health(
        {"connected": True, "heartbeat_ts_ms": 9_500},
        now_ms=now,
    )
    assert healthy is True
    assert reason == "OK"
    assert age == 500


def test_watchdog_rejects_stale_or_long_disconnected_transport():
    now = 100_000
    healthy, reason, _ = evaluate_health(
        {"connected": True, "heartbeat_ts_ms": 80_000},
        now_ms=now,
    )
    assert healthy is False
    assert reason == "STALE_HEARTBEAT"

    healthy, reason, _ = evaluate_health(
        {"connected": False, "heartbeat_ts_ms": 94_000},
        now_ms=now,
        max_heartbeat_age_ms=15_000,
        disconnected_grace_ms=5_000,
    )
    assert healthy is False
    assert reason == "DISCONNECTED"


@pytest.mark.asyncio
async def test_resilient_v2_reconnect_path_never_runs_sync_prune(tmp_path, monkeypatch):
    sqlite3.connect(tmp_path / "p25.sqlite").close()
    collector = ResilientBookCollectorV2(_settings(tmp_path))
    stop = asyncio.Event()

    async def fake_refresh(_session):
        collector.token_meta = {"token": ("condition", "BTC:5m", "UP")}
        return True

    async def fake_socket(_session, stop_event):
        stop_event.set()

    def forbidden_prune(*args, **kwargs):
        raise AssertionError("sync prune must not run on reconnect path")

    monkeypatch.setattr(collector, "refresh_registry", fake_refresh)
    monkeypatch.setattr(collector, "run_socket", fake_socket)
    monkeypatch.setattr(collector.books, "prune", forbidden_prune)

    try:
        await collector.run(stop)
    finally:
        collector.close()
