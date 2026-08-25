"""P2.6 resilient CLOB book collector V2.

The V1 resilient collector fixed needless 30s socket rotation, but its reconnect hot
path still ran synchronous SQLite history pruning before reconnecting. With a large
research DB or a competing writer this could block for SQLite's busy timeout and make
P3 observe a stale DOWN transport for tens of seconds.

V2 keeps reconnect latency independent of retention maintenance and now explicitly
seeds every subscribed token from the public CLOB /books endpoint at socket-session
start.  The seed does not fabricate a new exchange timestamp: BookSnapshotStore
observes an unchanged state by advancing recv_ts_ms, which lets P3 prove both legs
were observed in the current live session even when one resting book never emits a
WebSocket change event.

SHADOW/PAPER data collection only; no credentials, signing, order construction or
order submission.
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import sqlite3
import time
from typing import Any

import aiohttp

from p26_book_daemon import LocalBook
from p26_book_daemon_resilient import RECONNECT_BACKOFF_SEC, ResilientBookCollector
from p26_book_store import BookSnapshotStore
from p26_config import P26Settings, get_p26_settings


log = logging.getLogger("direction_engine.p26.book.resilient_v2")
PRUNE_INTERVAL_SEC = 600.0
PRUNE_BUSY_TIMEOUT_MS = 1000
PRUNE_BATCH_SIZE = 5_000
BOOK_SEED_HTTP_TIMEOUT_SEC = 8.0


def _source_timestamp_ms(value: object, fallback_ms: int) -> int:
    """Normalize the public CLOB order-book timestamp without inventing freshness."""
    try:
        ts = int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return int(fallback_ms)
    return ts * 1000 if ts < 10_000_000_000 else ts


class ResilientBookCollectorV2(ResilientBookCollector):
    """Reconnect immediately, seed session books, and prune only in background."""

    def _apply_session_seed(
        self,
        payload: object,
        *,
        recv_ms: int,
        session_started_ms: int,
    ) -> tuple[int, int]:
        """Observe public REST books in this socket session.

        Returns ``(seeded_tokens, missing_or_unusable_tokens)``.  Persisting an
        unchanged book updates only recv_ts_ms via BookSnapshotStore's conflict path;
        source_ts_ms remains the exchange timestamp from the REST payload.
        """
        if not isinstance(payload, list):
            return 0, len(self.token_meta)

        seeded_tokens: set[str] = set()
        observed_ms = max(int(recv_ms), int(session_started_ms))

        for raw in payload:
            if not isinstance(raw, dict):
                continue
            token = str(raw.get("asset_id") or "")
            meta = self.token_meta.get(token)
            if meta is None:
                continue

            book = self.local_books.setdefault(token, LocalBook())
            book.apply_snapshot(raw.get("bids") or [], raw.get("asks") or [])
            # P3 can only execute BUY+MERGE when the ask side is actually present.
            if not book.asks:
                continue

            source_ts_ms = _source_timestamp_ms(raw.get("timestamp"), int(recv_ms))
            snapshot = book.snapshot(
                token_id=token,
                ts_ms=source_ts_ms,
                sequence=None,
            )
            condition_id, combo_key, side = meta
            created = self.books.insert(
                condition_id=condition_id,
                combo_key=combo_key,
                side=side,
                snapshot=snapshot,
                recv_ts_ms=observed_ms,
            )
            if created:
                self.persisted += 1
            # Prevent an immediate duplicate incremental persist while still allowing
            # the normal minimum interval to elapse after the seed observation.
            self.last_persist_ms[token] = observed_ms
            seeded_tokens.add(token)

        return len(seeded_tokens), max(0, len(self.token_meta) - len(seeded_tokens))

    async def _seed_session_books(
        self,
        session: aiohttp.ClientSession,
        subscribed_tokens: tuple[str, ...],
        *,
        session_started_ms: int,
    ) -> tuple[int, int]:
        """Fetch a public full-depth snapshot for every subscribed token in one batch."""
        if not subscribed_tokens:
            return 0, 0

        url = f"{self.settings.clob_http_url.rstrip('/')}/books"
        body = [{"token_id": token} for token in subscribed_tokens]
        timeout = aiohttp.ClientTimeout(total=BOOK_SEED_HTTP_TIMEOUT_SEC)
        async with session.post(url, json=body, timeout=timeout) as response:
            if response.status != 200:
                text = await response.text()
                raise RuntimeError(
                    f"CLOB /books seed HTTP={response.status} body={text[:200]}"
                )
            payload: Any = await response.json()

        received = int(time.time() * 1000)
        return self._apply_session_seed(
            payload,
            recv_ms=received,
            session_started_ms=session_started_ms,
        )

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

                # WebSocket subscriptions do not guarantee that every unchanged
                # resting book will emit an initial event.  Seed all tokens through
                # the public batch REST endpoint so P3's session-completeness gate is
                # deterministic rather than dependent on the next price change.
                try:
                    seeded, missing = await self._seed_session_books(
                        session,
                        subscribed_tokens,
                        session_started_ms=session_started,
                    )
                    log.info(
                        "P2.6 session book seed seeded=%d missing_or_unusable=%d tokens=%d",
                        seeded,
                        missing,
                        len(subscribed_tokens),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    # Fail closed: keep the public WS alive.  P3 will continue to
                    # reject any leg without a current-session observation until a WS
                    # snapshot/change arrives instead of trusting stale history.
                    log.exception("P2.6 session REST book seed failed; WS remains live")

                self._write_health(connected=True, force=True)
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
