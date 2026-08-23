"""P3 structural-arbitrage SHADOW daemon.

Runs the model-free complete-set scanner, opportunity lifetime tracker, generic
observation replay, strict confirmation-time entry replay and read-only dashboard.
It never signs or submits orders.

Historical replay can be CPU/SQLite heavy when a backlog exists. Runtime replay is
therefore executed in a worker thread with fresh SQLite connections and a bounded
batch. The asyncio event loop remains available for health/dashboard traffic and
SIGTERM handling.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import time

from p3_config import P3Settings, get_p3_settings
from p3_entry_replay import P3EntryReplayEngine
from p3_replay_scheduler import P3ReplayEngine
from p3_scanner_resilient import ReconnectAwareStructuralArbScanner as StructuralArbScanner
from p3_web import run_web


log = logging.getLogger("direction_engine.p3.arbitrage")


async def scanner_loop(scanner: StructuralArbScanner, interval_ms: int, stop: asyncio.Event) -> None:
    while not stop.is_set():
        started = time.monotonic()
        try:
            stats = scanner.scan_once()
            if stats.inserted or stats.windows_closed:
                log.info("P3 scan %s", stats)
        except Exception:  # noqa: BLE001
            log.exception("P3 scanner iteration failed")
        elapsed = time.monotonic() - started
        wait = max(0.01, interval_ms / 1000.0 - elapsed)
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass


def _run_research_backlog_once(settings: P3Settings) -> dict:
    """Run one bounded replay slice on a worker thread.

    Engines are constructed and closed inside the worker thread so sqlite3
    connections are never moved across threads. Generic and strict-entry replay run
    sequentially on the same worker to avoid unnecessary concurrent P3 writers.
    """
    generic = P3ReplayEngine(settings)
    try:
        generic_result = generic.process_ready(
            batch_size=int(settings.replay_runtime_batch_size)
        )
    finally:
        generic.close()

    entry = P3EntryReplayEngine(settings)
    try:
        entry_result = entry.process_ready()
    finally:
        entry.close()

    return {"generic": generic_result, "entry": entry_result}


async def research_replay_loop(settings: P3Settings, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            result = await asyncio.to_thread(_run_research_backlog_once, settings)
            generic = result["generic"]
            entry = result["entry"]
            if generic["replays_created"] or generic.get("legacy_replays_purged"):
                log.info("P3 replay %s", generic)
            if entry["entry_replays_created"]:
                log.info("P3 strict entry replay %s", entry)
        except Exception:  # noqa: BLE001
            log.exception("P3 research replay iteration failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass


def install_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass


async def run() -> None:
    settings = get_p3_settings()
    settings.validate_research_safety()
    settings.ensure_directories()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    log.info(
        "P3 Arbitrage Lab starting SHADOW only p26_db=%s p3_db=%s scan=%dms "
        "web=%s:%d replay_runtime_batch=%d",
        settings.p26_db_path,
        settings.p3_db_path,
        settings.scan_interval_ms,
        settings.web_host,
        settings.web_port,
        settings.replay_runtime_batch_size,
    )
    stop = asyncio.Event()
    install_handlers(asyncio.get_running_loop(), stop)

    tasks: list[asyncio.Task] = []
    scanner: StructuralArbScanner | None = None

    # Bind the health/dashboard server before launching any research backlog work.
    # The short yield gives aiohttp a chance to complete site.start() before the
    # scanner/replay loops begin; replay itself is then offloaded from this event loop.
    if settings.web_enabled:
        tasks.append(asyncio.create_task(run_web(settings, stop)))
        await asyncio.sleep(0.10)

    if settings.scanner_enabled:
        scanner = StructuralArbScanner(settings)
        tasks.append(asyncio.create_task(scanner_loop(scanner, settings.scan_interval_ms, stop)))
        tasks.append(asyncio.create_task(research_replay_loop(settings, stop)))

    if not tasks:
        raise RuntimeError("P3 has no enabled tasks")
    try:
        await asyncio.gather(*tasks)
    finally:
        stop.set()
        if scanner is not None:
            scanner.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
