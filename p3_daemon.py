"""P3 structural-arbitrage SHADOW daemon.

Runs the model-free complete-set scanner, opportunity lifetime tracker, generic
observation replay, strict confirmation-time entry replay and read-only dashboard.
It never signs or submits orders.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import time

from p3_config import get_p3_settings
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


async def replay_loop(replay: P3ReplayEngine, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            result = replay.process_ready()
            if result["replays_created"] or result.get("legacy_replays_purged"):
                log.info("P3 replay %s", result)
        except Exception:  # noqa: BLE001
            log.exception("P3 replay iteration failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass


async def entry_replay_loop(replay: P3EntryReplayEngine, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            result = replay.process_ready()
            if result["entry_replays_created"]:
                log.info("P3 strict entry replay %s", result)
        except Exception:  # noqa: BLE001
            log.exception("P3 strict entry replay iteration failed")
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
        "P3 Arbitrage Lab starting SHADOW only p26_db=%s p3_db=%s scan=%dms web=%s:%d",
        settings.p26_db_path,
        settings.p3_db_path,
        settings.scan_interval_ms,
        settings.web_host,
        settings.web_port,
    )
    stop = asyncio.Event()
    install_handlers(asyncio.get_running_loop(), stop)
    scanner = StructuralArbScanner(settings)
    replay = P3ReplayEngine(settings)
    entry_replay = P3EntryReplayEngine(settings)
    tasks = []
    if settings.scanner_enabled:
        tasks.append(asyncio.create_task(scanner_loop(scanner, settings.scan_interval_ms, stop)))
        tasks.append(asyncio.create_task(replay_loop(replay, stop)))
        tasks.append(asyncio.create_task(entry_replay_loop(entry_replay, stop)))
    if settings.web_enabled:
        tasks.append(asyncio.create_task(run_web(settings, stop)))
    if not tasks:
        raise RuntimeError("P3 has no enabled tasks")
    try:
        await asyncio.gather(*tasks)
    finally:
        stop.set()
        scanner.close()
        replay.close()
        entry_replay.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
