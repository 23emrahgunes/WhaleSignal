"""P3 structural-arbitrage daemon with DRY default and guarded LIVE arming.

Research scanner/replay always remains available. LIVE state is process-local and
starts DRY after every restart. Optional authenticated execution runs only while a
localhost operator has armed the process and all preflight gates remain valid.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import time

from p3_config import P3Settings, get_p3_settings
from p3_entry_replay import P3EntryReplayEngine
from p3_live_control import run_live_control
from p3_live_executor_resolved import P3LiveExecutor
from p3_live_state import LiveState
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


async def live_executor_loop(
    settings: P3Settings,
    state: LiveState,
    stop: asyncio.Event,
) -> None:
    """Poll process-local LIVE state and execute at most one candidate per iteration."""
    while not stop.is_set():
        if state.can_auto_execute():
            try:
                result = await asyncio.to_thread(P3LiveExecutor(settings, state).process_once)
                status = str(result.get("status") or "")
                if status not in {"NO_CONFIRMED_WINDOW", "IDLE_NOT_AUTO_ARMED", "IDLE_NOT_ARMED"}:
                    log.warning("P3 LIVE result=%s", result)
            except Exception:  # noqa: BLE001
                state.halt("LIVE_LOOP_EXCEPTION")
                log.exception("P3 LIVE loop failed closed")
        try:
            await asyncio.wait_for(
                stop.wait(),
                timeout=max(0.02, settings.live_poll_interval_ms / 1000.0),
            )
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
    live_state = LiveState(
        live_feature_enabled=settings.live_feature_enabled,
        auto_execute_enabled=settings.live_auto_execute_enabled,
    )
    log.info(
        "P3 starting mode=DRY p26_db=%s p3_db=%s scan=%dms web=%s:%d "
        "live_feature=%s live_auto=%s live_control=%s:%d",
        settings.p26_db_path,
        settings.p3_db_path,
        settings.scan_interval_ms,
        settings.web_host,
        settings.web_port,
        settings.live_feature_enabled,
        settings.live_auto_execute_enabled,
        settings.live_control_host,
        settings.live_control_port,
    )
    stop = asyncio.Event()
    install_handlers(asyncio.get_running_loop(), stop)
    tasks: list[asyncio.Task] = []
    scanner: StructuralArbScanner | None = None

    if settings.web_enabled:
        tasks.append(asyncio.create_task(run_web(settings, stop, live_state=live_state)))
        await asyncio.sleep(0.10)
    if settings.live_control_enabled:
        tasks.append(asyncio.create_task(run_live_control(settings, live_state, stop)))
    if settings.scanner_enabled:
        scanner = StructuralArbScanner(settings)
        tasks.append(asyncio.create_task(scanner_loop(scanner, settings.scan_interval_ms, stop)))
        tasks.append(asyncio.create_task(research_replay_loop(settings, stop)))

    # Safe to run without optional SDK deps because gateway imports happen lazily only
    # after LIVE is explicitly armed and auto-execution is enabled.
    tasks.append(asyncio.create_task(live_executor_loop(settings, live_state, stop)))

    if not tasks:
        raise RuntimeError("P3 has no enabled tasks")
    try:
        await asyncio.gather(*tasks)
    finally:
        live_state.disarm("process_shutdown")
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
