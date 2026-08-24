"""Regression tests for P2.5 heavy dashboard snapshot caching."""
from __future__ import annotations

import time
from pathlib import Path

from p25_snapshot_cache import SnapshotCache


def _wait_until(predicate, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def test_snapshot_cache_prewarm_is_single_flight():
    calls = []

    def source():
        calls.append(time.monotonic())
        time.sleep(0.03)
        return {"seq": len(calls)}

    cache = SnapshotCache(source, ttl_sec=5.0)
    cache.prewarm()
    value = cache.get()

    assert value == {"seq": 1}
    assert len(calls) == 1
    assert cache.status()["ready"] is True


def test_expired_snapshot_is_served_stale_while_one_refresh_runs():
    calls = []

    def source():
        calls.append(time.monotonic())
        time.sleep(0.05)
        return {"seq": len(calls)}

    cache = SnapshotCache(source, ttl_sec=0.5)
    cache.prewarm()
    assert cache.get() == {"seq": 1}

    with cache._lock:  # regression test intentionally forces expiry
        cache._completed_at = time.monotonic() - 2.0

    started = time.monotonic()
    stale1 = cache.get()
    stale2 = cache.get()
    elapsed = time.monotonic() - started

    assert stale1 == {"seq": 1}
    assert stale2 == {"seq": 1}
    assert elapsed < 0.04
    assert _wait_until(lambda: len(calls) == 2)
    assert _wait_until(lambda: cache.get().get("seq") == 2)
    assert len(calls) == 2


def test_snapshot_freshness_starts_at_completion():
    def source():
        time.sleep(0.05)
        return {"ok": True}

    cache = SnapshotCache(source, ttl_sec=0.5)
    cache.prewarm()
    assert cache.get() == {"ok": True}
    age = cache.status()["age_sec"]
    assert age is not None
    assert age < 0.20


def test_p25_main_wires_cache_after_runtime_attachments():
    text = Path("p25_main.py").read_text(encoding="utf-8")
    attach_reconciler = text.index("engine.attach_paper_reconciler")
    attach_clob = text.index("engine.attach_clob")
    install_cache = text.index("snapshot_cache = SnapshotCache")
    prewarm = text.index("snapshot_cache.prewarm()")

    assert attach_reconciler < attach_clob < install_cache < prewarm
    assert "engine.snapshot = snapshot_cache.get" in text
