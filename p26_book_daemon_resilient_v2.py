"""P2.6 resilient CLOB book collector V2.

The V1 resilient collector fixed needless 30s socket rotation, but its reconnect hot
path still ran synchronous SQLite history pruning before reconnecting. With a large
research DB or a competing writer this could block for SQLite's busy timeout and make
P3 observe a stale DOWN transport for tens of seconds.

V2 keeps reconnect latency independent of retention maintenance: the socket loop
reconnects immediately, while pruning runs in a low-priority background thread with a
short SQLite busy timeout. SHADOW/PAPER data collection only; no credentials,
signing, order construction or order submission.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sqlite3
import time

import aiohttp

from p26_book_daemon_resilient import RECONNECT_BACKOFF_SEC, ResilientBookCollector
from p26_book_store import BookSnapshotStore
from p26_config import P26Settings, get_p26_settings


log = logging.getLogger("direction_engine.p26.book.resilient_v2")
PRUNE_INTERVAL_SEC = 600.0
PRUNE_BUSY_TIMEOUT_MS = 1000
PRUNE_BATCH_SIZE = 5_000


class ResilientBookCollectorV2(ResilientBookCollector):
    """Reconnect immediately; never prune synchronously on the reconnect path."""

    async def run(self, stop: asyncio.Event) -> None:
        async with aiohttp.ClientSession() as session:
            while not stop.is_set():
                try:
                    if not self.token_meta:
                        await self.refresh_registry(session)
                    await self.run_socket(session, stop)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    self._write_health(connected=False, force=True)
                    log.exception("P2.6 resilient-v2 book collector cycle failed")
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass

                # IMPORTANT: no synchronous DB maintenance here. Reconnect latency
                # must not depend on SQLite retention work.
                if not stop.is_set():
                    try:
                        await asyncio.wait_for(
                            stop.wait(), timeout=RECONNECT_BACKOFF_SEC
                        )
                    except asyncio.TimeoutError:
                        pass


def _prune_once(settings: P26Settings) -> int:
    """Best-effort retention prune on a dedicated connection/thread."""
    store = BookSnapshotStore(settings.p26_db_path)
    try:
        store.conn.execute(f"PRAGMA busy_timeout={PRUNE_BUSY_TIMEOUT_MS}")
        cutoff = (
            int(time.time() * 1000)
            - settings.book_history_retention_hours * 3_600_000
        )
        return store.prune(before_ts_ms=cutoff, batch_size=PRUNE_BATCH_SIZE)
    finally:
        store.close()


async def _prune_loop(settings: P26Settings, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=PRUNE_INTERVAL_SEC)
            return
        except asyncio.TimeoutError:
            pass

        try:
            deleted = await asyncio.to_thread(_prune_once, settings)
            if deleted:
                log.info("P2.6 background book prune deleted=%d", deleted)
        except sqlite3.OperationalError as exc:
            # Retention is maintenance, never a reason to disturb a healthy feed.
            log.warning("P2.6 background book prune skipped error=%r", exc)
        except Exception:  # noqa: BLE001
            log.exception("P2.6 background book prune failed")


async def _run() -> None:
    settings: P26Settings = get_p26_settings()
    collector = ResilientBookCollectorV2(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    prune_task = asyncio.create_task(_prune_loop(settings, stop))
    try:
        await collector.run(stop)
    finally:
        stop.set()
        prune_task.cancel()
        try:
            await prune_task
        except asyncio.CancelledError:
            pass
        collector.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    asyncio.run(_run())


if __name__ == "__main__":
    main()
