from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

import p3_daemon


def test_research_backlog_slice_uses_fresh_engines_and_runtime_cap(monkeypatch):
    events: list[tuple] = []

    class FakeGeneric:
        def __init__(self, settings):
            events.append(("generic_init", settings.replay_runtime_batch_size))

        def process_ready(self, *, batch_size):
            events.append(("generic_process", batch_size))
            return {
                "replays_created": 2,
                "legacy_replays_purged": 0,
                "opportunities_scanned": 1,
                "batch_size": batch_size,
            }

        def close(self):
            events.append(("generic_close",))

    class FakeEntry:
        def __init__(self, settings):
            events.append(("entry_init", settings.replay_runtime_batch_size))

        def process_ready(self):
            events.append(("entry_process",))
            return {"entry_replays_created": 1}

        def close(self):
            events.append(("entry_close",))

    monkeypatch.setattr(p3_daemon, "P3ReplayEngine", FakeGeneric)
    monkeypatch.setattr(p3_daemon, "P3EntryReplayEngine", FakeEntry)
    settings = SimpleNamespace(replay_runtime_batch_size=7)

    result = p3_daemon._run_research_backlog_once(settings)

    assert result["generic"]["batch_size"] == 7
    assert result["entry"]["entry_replays_created"] == 1
    assert events == [
        ("generic_init", 7),
        ("generic_process", 7),
        ("generic_close",),
        ("entry_init", 7),
        ("entry_process",),
        ("entry_close",),
    ]


@pytest.mark.asyncio
async def test_research_replay_loop_runs_blocking_work_off_event_loop(monkeypatch):
    main_thread = threading.get_ident()
    worker_threads: list[int] = []

    def fake_slice(_settings):
        worker_threads.append(threading.get_ident())
        time.sleep(0.05)
        return {
            "generic": {"replays_created": 0, "legacy_replays_purged": 0},
            "entry": {"entry_replays_created": 0},
        }

    monkeypatch.setattr(p3_daemon, "_run_research_backlog_once", fake_slice)
    stop = asyncio.Event()
    task = asyncio.create_task(p3_daemon.research_replay_loop(object(), stop))

    # If fake_slice ran directly on the asyncio thread, this sleep could not wake
    # until the blocking 50ms work completed.
    started = time.monotonic()
    await asyncio.sleep(0.01)
    elapsed = time.monotonic() - started
    assert elapsed < 0.04

    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert worker_threads
    assert worker_threads[0] != main_thread
