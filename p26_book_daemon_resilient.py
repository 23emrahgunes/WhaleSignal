"""High-uptime public CLOB book collector for P2.6/P3 research.

The original collector intentionally closed its WebSocket every registry-refresh
interval.  If public market metadata was slow during the gap, P3 could observe a
long `Book socket = DOWN` period even though the market feed itself was healthy.

This entrypoint keeps the current public WebSocket connected while refreshing the
market/token registry.  It reconnects only when the subscribed token set actually
changes or when the WebSocket itself closes/errors.  Missing public market metadata
is fetched concurrently with a short timeout so a single slow condition cannot
stall the whole collector.

SHADOW/PAPER DATA COLLECTION ONLY.  No credentials, private key, signing, order
construction or order submission exists here.
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from typing import Any

import aiohttp

from p26_book_daemon import BookCollector, LocalBook, load_active_markets
from p26_config import P26Settings, get_p26_settings
from p26_fee import fetch_clob_market_info


log = logging.getLogger("direction_engine.p26.book.resilient")
REGISTRY_HTTP_TIMEOUT_SEC = 2.5
REGISTRY_FETCH_CONCURRENCY = 4
RECONNECT_BACKOFF_SEC = 0.25


class ResilientBookCollector(BookCollector):
    """Book collector that does not rotate a healthy socket on every refresh."""

    async def refresh_registry(self, session: aiohttp.ClientSession) -> bool:
        now_ms = int(time.time() * 1000)
        markets = load_active_markets(self.settings.p25_db_path, now_ms=now_ms)
        active_condition_ids = {
            item.condition_id for item in markets if item.active_at(now_ms)
        }
        self.collected_condition_count = len({item.condition_id for item in markets})
        self.active_condition_count = len(active_condition_ids)

        previous_meta = dict(self.token_meta)
        new_meta: dict[str, tuple[str, str, str]] = {}
        missing = []

        for market in markets:
            mapping = self.fees.mapping(market.condition_id)
            schedules = {
                side: self.fees.get(market.condition_id, token_id)
                for side, token_id in mapping.items()
            }
            complete_cached = (
                set(mapping) == {"UP", "DOWN"}
                and set(schedules) == {"UP", "DOWN"}
                and all(value is not None for value in schedules.values())
            )
            if complete_cached:
                for side, token_id in mapping.items():
                    new_meta[token_id] = (market.condition_id, market.combo_key, side)
            else:
                missing.append(market)

        semaphore = asyncio.Semaphore(REGISTRY_FETCH_CONCURRENCY)

        async def fetch_one(market):  # noqa: ANN001, ANN202
            async with semaphore:
                try:
                    payload = await fetch_clob_market_info(
                        session,
                        base_url=self.settings.clob_http_url,
                        condition_id=market.condition_id,
                        timeout_sec=REGISTRY_HTTP_TIMEOUT_SEC,
                    )
                    return market, payload, None
                except Exception as exc:  # noqa: BLE001
                    return market, None, exc

        if missing:
            results = await asyncio.gather(*(fetch_one(market) for market in missing))
            for market, payload, error in results:
                if error is not None or payload is None:
                    log.warning(
                        "market info unavailable condition=%s error=%r; retaining prior mapping if present",
                        market.condition_id,
                        error,
                    )
                    # A transient public-HTTP failure must not tear down an already
                    # healthy subscription for the same condition.
                    for token_id, meta in previous_meta.items():
                        if meta[0] == market.condition_id:
                            new_meta[token_id] = meta
                    continue

                schedules = self.fees.upsert_market_info(
                    condition_id=market.condition_id,
                    combo_key=market.combo_key,
                    market_end_ts_ms=market.market_end_ts_ms,
                    payload=payload,
                )
                mapping = self.fees.mapping(market.condition_id)
                if set(mapping) != {"UP", "DOWN"} or set(schedules) != {"UP", "DOWN"}:
                    log.warning(
                        "incomplete token/fee mapping condition=%s mapping=%s",
                        market.condition_id,
                        mapping,
                    )
                    for token_id, meta in previous_meta.items():
                        if meta[0] == market.condition_id:
                            new_meta[token_id] = meta
                    continue
                for side, token_id in mapping.items():
                    new_meta[token_id] = (market.condition_id, market.combo_key, side)

        # Prefetched future markets may be subscribed, but only currently trading
        # conditions are exposed to P3 as active.
        self.fees.mark_active_conditions(active_condition_ids)

        changed = new_meta != previous_meta
        if changed:
            self.token_meta = new_meta
            self.local_books = {
                token: self.local_books.get(token, LocalBook()) for token in new_meta
            }
        self._write_health(force=True)
        return changed

    async def run_socket(self, session: aiohttp.ClientSession, stop: asyncio.Event) -> None:
        subscribed_tokens = tuple(sorted(self.token_meta))
        if not subscribed_tokens:
            self._write_health(connected=False, force=True)
            try:
                await asyncio.wait_for(stop.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            return

        refresh_interval = max(1.0, float(self.settings.book_market_refresh_sec))
        next_refresh = time.monotonic() + refresh_interval
        disconnect_reason = "STOP"

        try:
            async with session.ws_connect(
                self.settings.clob_ws_url,
                heartbeat=15,
                receive_timeout=30,
            ) as ws:
                await ws.send_json(
                    {
                        "assets_ids": list(subscribed_tokens),
                        "type": "market",
                        "custom_feature_enabled": True,
                    }
                )
                session_started = int(time.time() * 1000)
                self._write_health(
                    connected=True,
                    session_started_ms=session_started,
                    last_message_recv_ms=0,
                    force=True,
                )
                log.info(
                    "P2.6 resilient book subscribed tokens=%d active_conditions=%d collected_conditions=%d",
                    len(subscribed_tokens),
                    self.active_condition_count,
                    self.collected_condition_count,
                )

                while not stop.is_set():
                    if time.monotonic() >= next_refresh:
                        try:
                            changed = await self.refresh_registry(session)
                        except asyncio.CancelledError:
                            raise
                        except Exception:  # noqa: BLE001
                            # Registry metadata is auxiliary while an existing public
                            # book socket is healthy. Keep consuming the live feed.
                            log.exception("registry refresh failed while socket remained live")
                            self._write_health(connected=True, force=True)
                            changed = False

                        next_refresh = time.monotonic() + refresh_interval
                        current_tokens = tuple(sorted(self.token_meta))
                        if changed or current_tokens != subscribed_tokens:
                            disconnect_reason = "REGISTRY_CHANGED"
                            log.info(
                                "book registry changed; controlled reconnect old_tokens=%d new_tokens=%d",
                                len(subscribed_tokens),
                                len(current_tokens),
                            )
                            break

                    try:
                        message = await asyncio.wait_for(ws.receive(), timeout=1.0)
                    except asyncio.TimeoutError:
                        self._write_health(connected=True)
                        continue

                    received = int(time.time() * 1000)
                    if message.type == aiohttp.WSMsgType.TEXT:
                        self.last_message_recv_ms = received
                        try:
                            payload: Any = json.loads(message.data)
                        except json.JSONDecodeError:
                            self._write_health(
                                connected=True,
                                last_message_recv_ms=received,
                            )
                            continue
                        events = payload if isinstance(payload, list) else [payload]
                        for event in events:
                            if isinstance(event, dict):
                                self.handle_event(event, recv_ms=received)
                        self._write_health(
                            connected=True,
                            last_message_recv_ms=received,
                        )
                    elif message.type == aiohttp.WSMsgType.CLOSED:
                        disconnect_reason = "WS_CLOSED"
                        break
                    elif message.type == aiohttp.WSMsgType.ERROR:
                        disconnect_reason = "WS_ERROR"
                        break
        finally:
            self._write_health(connected=False, force=True)
            log.info("P2.6 book socket ended reason=%s", disconnect_reason)

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
                    log.exception("P2.6 resilient book collector cycle failed")
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass

                cutoff = (
                    int(time.time() * 1000)
                    - self.settings.book_history_retention_hours * 3_600_000
                )
                self.books.prune(before_ts_ms=cutoff, batch_size=10_000)

                if not stop.is_set():
                    try:
                        await asyncio.wait_for(
                            stop.wait(), timeout=RECONNECT_BACKOFF_SEC
                        )
                    except asyncio.TimeoutError:
                        pass


async def _run() -> None:
    settings: P26Settings = get_p26_settings()
    collector = ResilientBookCollector(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    try:
        await collector.run(stop)
    finally:
        collector.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    asyncio.run(_run())


if __name__ == "__main__":
    main()
