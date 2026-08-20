"""Independent Chainlink RTDS persistence sidecar for P2.6.

This process opens its own public RTDS connection and writes only to the isolated
P2.6 research database.  The P2.5 runtime is not imported or modified beyond
reusing its proven payload parser.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import time
from collections import defaultdict, deque
from typing import Optional

import aiohttp

from p26_config import P26Settings, get_p26_settings
from p26_oracle_store import OracleTick, OracleTickStore, iter_rtds_ticks

log = logging.getLogger("direction_engine.p26.oracle")


class OracleBatchWriter:
    def __init__(self, settings: P26Settings, store: OracleTickStore) -> None:
        self.settings = settings
        self.store = store
        self.queue: asyncio.Queue[OracleTick] = asyncio.Queue(
            maxsize=settings.oracle_queue_max
        )
        self.enqueued = 0
        self.inserted = 0
        self.duplicates = 0
        self.backpressure_events = 0
        self.flushes = 0
        self.last_flush_ms = 0
        self._recent: dict[str, deque[OracleTick]] = defaultdict(
            lambda: deque(maxlen=4_096)
        )

    async def enqueue(self, tick: OracleTick) -> None:
        if self.queue.full():
            self.backpressure_events += 1
        # Do not silently drop official oracle observations. Backpressure the WS
        # consumer; reconnect logic will recover if the connection times out.
        await self.queue.put(tick)
        self.enqueued += 1

    async def run(self, stop: asyncio.Event) -> None:
        interval = max(0.01, self.settings.oracle_flush_interval_ms / 1000.0)
        batch_size = max(1, self.settings.oracle_batch_size)
        while not stop.is_set() or not self.queue.empty():
            batch: list[OracleTick] = []
            deadline = asyncio.get_running_loop().time() + interval
            while len(batch) < batch_size:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    tick = await asyncio.wait_for(self.queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                batch.append(tick)
                self.queue.task_done()
            if not batch:
                continue
            inserted = await asyncio.to_thread(self.store.insert_many, batch)
            self.inserted += inserted
            self.duplicates += len(batch) - inserted
            self.flushes += 1
            self.last_flush_ms = int(time.time() * 1000)
            for tick in batch:
                self._recent[tick.asset].append(tick)

    def rehydrate(self) -> int:
        since = int(time.time() * 1000) - self.settings.oracle_rehydrate_minutes * 60_000
        restored = self.store.rehydrate(since_ts_ms=since)
        count = 0
        for asset, ticks in restored.items():
            self._recent[asset].extend(ticks)
            count += len(ticks)
        return count

    def health(self) -> dict:
        return {
            "queue_size": self.queue.qsize(),
            "queue_max": self.queue.maxsize,
            "enqueued": self.enqueued,
            "inserted": self.inserted,
            "duplicates": self.duplicates,
            "backpressure_events": self.backpressure_events,
            "flushes": self.flushes,
            "last_flush_ms": self.last_flush_ms,
            "recent_points": {asset: len(points) for asset, points in self._recent.items()},
        }


class OracleRTDSSidecar:
    def __init__(self, settings: P26Settings, writer: OracleBatchWriter) -> None:
        self.settings = settings
        self.writer = writer
        self.connected = False
        self.reconnects = 0
        self.messages = 0
        self.parsed_ticks = 0

    @staticmethod
    def subscribe_message() -> dict:
        return {
            "action": "subscribe",
            "subscriptions": [
                {"topic": "crypto_prices_chainlink", "type": "*", "filters": ""}
            ],
        }

    async def _ping_loop(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        stop: asyncio.Event,
    ) -> None:
        while not stop.is_set() and not ws.closed:
            await ws.send_str("PING")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.settings.rtds_ping_sec)
            except asyncio.TimeoutError:
                pass

    async def run(self, stop: asyncio.Event) -> None:
        attempt = 0
        async with aiohttp.ClientSession() as session:
            while not stop.is_set():
                ping_task: Optional[asyncio.Task] = None
                try:
                    async with session.ws_connect(
                        self.settings.rtds_ws_url,
                        heartbeat=None,
                        receive_timeout=self.settings.rtds_recv_timeout_sec,
                    ) as ws:
                        self.connected = True
                        attempt = 0
                        await ws.send_str(json.dumps(self.subscribe_message()))
                        ping_task = asyncio.create_task(self._ping_loop(ws, stop))
                        log.info("P2.6 oracle sidecar connected")
                        async for message in ws:
                            if stop.is_set():
                                break
                            if message.type == aiohttp.WSMsgType.TEXT:
                                raw = str(message.data).strip()
                                if raw == "PING":
                                    await ws.send_str("PONG")
                                    continue
                                if raw == "PONG":
                                    continue
                                self.messages += 1
                                try:
                                    obj = json.loads(raw)
                                except json.JSONDecodeError:
                                    continue
                                recv_ms = int(time.time() * 1000)
                                ticks = list(iter_rtds_ticks(obj, recv_ms))
                                self.parsed_ticks += len(ticks)
                                for tick in ticks:
                                    await self.writer.enqueue(tick)
                            elif message.type in (
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.reconnects += 1
                    delay = min(30.0, 2.0**min(attempt, 5))
                    attempt += 1
                    log.warning("P2.6 oracle reconnect in %.1fs: %s", delay, exc)
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=delay)
                    except asyncio.TimeoutError:
                        pass
                finally:
                    self.connected = False
                    if ping_task is not None:
                        ping_task.cancel()
                        try:
                            await ping_task
                        except (asyncio.CancelledError, Exception):  # noqa: BLE001
                            pass

    def health(self) -> dict:
        return {
            "connected": self.connected,
            "reconnects": self.reconnects,
            "messages": self.messages,
            "parsed_ticks": self.parsed_ticks,
            "writer": self.writer.health(),
        }


async def run(settings: P26Settings) -> None:
    settings.validate_research_safety()
    store = OracleTickStore(settings.p26_db_path)
    writer = OracleBatchWriter(settings, store)
    restored = writer.rehydrate()
    log.info("P2.6 oracle rehydrated %d ticks", restored)
    sidecar = OracleRTDSSidecar(settings, writer)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    writer_task = asyncio.create_task(writer.run(stop), name="p26-oracle-writer")
    sidecar_task = asyncio.create_task(sidecar.run(stop), name="p26-oracle-rtds")
    try:
        await stop.wait()
    finally:
        stop.set()
        await asyncio.gather(sidecar_task, writer_task, return_exceptions=True)
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    asyncio.run(run(get_p26_settings()))


if __name__ == "__main__":
    main()
