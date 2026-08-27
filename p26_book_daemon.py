"""Read-only P2.6 full-depth CLOB and dynamic-fee collector.

The daemon discovers current plus near-future P2.5 conditions, reads public CLOB
market metadata, subscribes to public market WebSocket data and persists UP/DOWN
order books plus fee lineage in the isolated P2.6 database.

`p26_market_tokens.active=1` means *currently trading now*. Near-future markets may
still be prefetched/subscribed, but they are not exposed to P3 as active until their
market_start boundary. A small transport heartbeat is also persisted so consumers
can distinguish an unchanged resting book from a dead/stale WebSocket.

No authentication, signing or order submission code exists here.
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import aiohttp

from p26_book_store import BookSnapshotStore
from p26_config import P26Settings, get_p26_settings
from p26_execution import OrderBookSnapshot
from p26_fee import FeeScheduleStore, fetch_clob_market_info


log = logging.getLogger("direction_engine.p26.book")
BOOK_HEALTH_META_KEY = "book_collector_health_json"
BOOK_PREFETCH_MS = 120_000


@dataclass(frozen=True)
class ActiveMarket:
    condition_id: str
    combo_key: str
    market_start_ts_ms: int
    market_end_ts_ms: int

    def active_at(self, now_ms: int) -> bool:
        return self.market_start_ts_ms <= int(now_ms) < self.market_end_ts_ms


def load_active_markets(
    p25_db_path: str,
    *,
    now_ms: Optional[int] = None,
    prefetch_ms: int = BOOK_PREFETCH_MS,
) -> list[ActiveMarket]:
    """Return currently active plus near-future markets for book prefetch.

    The return set is intentionally broader than `p26_market_tokens.active=1`.
    `BookCollector.refresh_registry()` marks only `market_start <= now < market_end`
    conditions active while retaining near-future token subscriptions for a clean
    boundary hand-off.
    """
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    path = Path(p25_db_path).resolve()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(markets)").fetchall()
        }
        required = {"condition_id", "combo_key", "market_start", "market_end"}
        if not required.issubset(columns):
            raise RuntimeError(f"P2.5 markets schema missing {sorted(required-columns)}")
        resolved_clause = "AND COALESCE(resolved,0)=0" if "resolved" in columns else ""
        rows = conn.execute(
            f"""
            SELECT condition_id,combo_key,market_start,market_end
            FROM markets
            WHERE condition_id IS NOT NULL
              AND market_start IS NOT NULL
              AND market_end IS NOT NULL
              AND CAST(market_end*1000 AS INTEGER) > ?
              AND CAST(market_start*1000 AS INTEGER) <= ?
              {resolved_clause}
            ORDER BY market_start,market_end,condition_id
            """,
            (now - 60_000, now + max(0, int(prefetch_ms))),
        ).fetchall()
        return [
            ActiveMarket(
                str(row["condition_id"]),
                str(row["combo_key"]),
                int(round(float(row["market_start"]) * 1000)),
                int(round(float(row["market_end"]) * 1000)),
            )
            for row in rows
        ]
    finally:
        conn.close()


def _number(value: object) -> Optional[float]:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _timestamp_ms(value: object, fallback: int) -> int:
    try:
        ts = int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return int(fallback)
    return ts * 1000 if ts < 10_000_000_000 else ts


def _levels(values: object) -> dict[float, float]:
    output: dict[float, float] = {}
    if not isinstance(values, list):
        return output
    for item in values:
        if isinstance(item, dict):
            price, size = _number(item.get("price")), _number(item.get("size"))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            price, size = _number(item[0]), _number(item[1])
        else:
            continue
        if price is not None and size is not None and 0 < price < 1 and size > 0:
            output[price] = size
    return output


class LocalBook:
    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}

    def apply_snapshot(self, bids: object, asks: object) -> None:
        self.bids = _levels(bids)
        self.asks = _levels(asks)

    def apply_changes(self, changes: list[dict[str, Any]]) -> None:
        for change in changes:
            price, size = _number(change.get("price")), _number(change.get("size"))
            if price is None or size is None or not 0 < price < 1:
                continue
            side = str(change.get("side") or "").upper()
            levels = self.bids if side in {"BUY", "BID"} else self.asks if side in {"SELL", "ASK"} else None
            if levels is None:
                continue
            if size <= 0:
                levels.pop(price, None)
            else:
                levels[price] = size

    def snapshot(self, *, token_id: str, ts_ms: int, sequence: Optional[int]) -> OrderBookSnapshot:
        return OrderBookSnapshot.from_levels(
            token_id=token_id,
            ts_ms=ts_ms,
            bids=self.bids.items(),
            asks=self.asks.items(),
            sequence=sequence,
        )


class BookCollector:
    def __init__(self, settings: P26Settings) -> None:
        self.settings = settings
        self.fees = FeeScheduleStore(settings.p26_db_path)
        self.books = BookSnapshotStore(settings.p26_db_path)
        self.token_meta: dict[str, tuple[str, str, str]] = {}
        self.local_books: dict[str, LocalBook] = {}
        self.last_persist_ms: dict[str, int] = {}
        self.messages = 0
        self.persisted = 0
        self.socket_connected = False
        self.session_started_ms = 0
        self.last_message_recv_ms = 0
        self.last_health_write_ms = 0
        self.active_condition_count = 0
        self.collected_condition_count = 0

    def _write_health(
        self,
        *,
        connected: Optional[bool] = None,
        session_started_ms: Optional[int] = None,
        last_message_recv_ms: Optional[int] = None,
        force: bool = False,
    ) -> None:
        now_ms = int(time.time() * 1000)
        if connected is not None:
            self.socket_connected = bool(connected)
        if session_started_ms is not None:
            self.session_started_ms = int(session_started_ms)
        if last_message_recv_ms is not None:
            self.last_message_recv_ms = int(last_message_recv_ms)
        if not force and now_ms - self.last_health_write_ms < 750:
            return
        payload = {
            "connected": self.socket_connected,
            "heartbeat_ts_ms": now_ms,
            "session_started_ms": self.session_started_ms,
            "last_message_recv_ms": self.last_message_recv_ms,
            "subscribed_tokens": len(self.token_meta),
            "active_conditions": self.active_condition_count,
            "collected_conditions": self.collected_condition_count,
            "messages": self.messages,
            "persisted": self.persisted,
        }
        self.fees.conn.execute(
            """
            INSERT INTO p26_meta(key,value,updated_at_ms) VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at_ms=excluded.updated_at_ms
            """,
            (BOOK_HEALTH_META_KEY, json.dumps(payload, sort_keys=True, separators=(",", ":")), now_ms),
        )
        self.fees.conn.commit()
        self.last_health_write_ms = now_ms

    async def refresh_registry(self, session: aiohttp.ClientSession) -> bool:
        now_ms = int(time.time() * 1000)
        markets = load_active_markets(self.settings.p25_db_path, now_ms=now_ms)
        collect_condition_ids = {item.condition_id for item in markets}
        active_condition_ids = {
            item.condition_id for item in markets if item.active_at(now_ms)
        }
        self.collected_condition_count = len(collect_condition_ids)
        self.active_condition_count = len(active_condition_ids)
        changed = False
        new_meta: dict[str, tuple[str, str, str]] = {}
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
            if not complete_cached:
                try:
                    payload = await fetch_clob_market_info(
                        session,
                        base_url=self.settings.clob_http_url,
                        condition_id=market.condition_id,
                    )
                    schedules = self.fees.upsert_market_info(
                        condition_id=market.condition_id,
                        combo_key=market.combo_key,
                        market_end_ts_ms=market.market_end_ts_ms,
                        payload=payload,
                    )
                    mapping = self.fees.mapping(market.condition_id)
                except Exception as exc:  # noqa: BLE001
                    log.warning("market info unavailable condition=%s error=%r", market.condition_id, exc)
                    continue
            if set(mapping) != {"UP", "DOWN"} or set(schedules) != {"UP", "DOWN"}:
                log.warning("incomplete token/fee mapping condition=%s mapping=%s", market.condition_id, mapping)
                continue
            for side, token_id in mapping.items():
                new_meta[token_id] = (market.condition_id, market.combo_key, side)

        # Critical semantic split: prefetch subscriptions are allowed, but only
        # currently trading conditions are advertised to P3 as active.
        self.fees.mark_active_conditions(active_condition_ids)
        if new_meta != self.token_meta:
            changed = True
            self.token_meta = new_meta
            self.local_books = {
                token: self.local_books.get(token, LocalBook()) for token in new_meta
            }
        self._write_health(force=True)
        return changed

    def _persist(self, token: str, ts_ms: int, sequence: Optional[int], recv_ms: int) -> None:
        meta = self.token_meta.get(token)
        book = self.local_books.get(token)
        if meta is None or book is None:
            return

        # Empty executable ask depth is a real market state, not "missing data".
        # Dropping this transition leaves the previous non-empty snapshot as the
        # latest P26 truth, so P3 can keep seeing ghost BUY+MERGE liquidity after the
        # exchange has removed the entire ask side. Persist an empty-ask transition
        # immediately, bypassing the normal history-throttle, so downstream scanners
        # fail closed on the current book rather than trading stale depth.
        empty_ask_state = not book.asks
        last = self.last_persist_ms.get(token, 0)
        if (
            not empty_ask_state
            and recv_ms - last < self.settings.book_persist_min_interval_ms
        ):
            return

        condition_id, combo_key, side = meta
        snapshot = book.snapshot(token_id=token, ts_ms=ts_ms, sequence=sequence)
        created = self.books.insert(
            condition_id=condition_id,
            combo_key=combo_key,
            side=side,
            snapshot=snapshot,
            recv_ts_ms=recv_ms,
        )
        if created:
            self.persisted += 1
        # Even a duplicate observation advanced recv_ts_ms in BookSnapshotStore.
        # Record the observation locally as well so normal non-empty persistence
        # remains throttled after the truth state has been written.
        self.last_persist_ms[token] = recv_ms

    def handle_event(self, event: dict[str, Any], *, recv_ms: Optional[int] = None) -> None:
        received = int(time.time() * 1000) if recv_ms is None else int(recv_ms)
        self.messages += 1
        event_type = str(event.get("event_type") or event.get("type") or "").lower()
        ts_ms = _timestamp_ms(event.get("timestamp"), received)
        sequence_raw = event.get("sequence") or event.get("seq")
        try:
            sequence = int(sequence_raw) if sequence_raw is not None else None
        except (TypeError, ValueError):
            sequence = None

        raw_changes = event.get("price_changes")
        if event_type == "price_change" or isinstance(raw_changes, list):
            grouped: dict[str, list[dict[str, Any]]] = {}
            for change in raw_changes or []:
                if not isinstance(change, dict):
                    continue
                token = change.get("asset_id") or change.get("assetId")
                if token is not None and str(token) in self.token_meta:
                    grouped.setdefault(str(token), []).append(change)
            for token, changes in grouped.items():
                self.local_books.setdefault(token, LocalBook()).apply_changes(changes)
                self._persist(token, ts_ms, sequence, received)
            return

        token_raw = event.get("asset_id") or event.get("assetId")
        if token_raw is None:
            return
        token = str(token_raw)
        if token not in self.token_meta:
            return
        if event_type == "book" or "bids" in event or "asks" in event:
            self.local_books.setdefault(token, LocalBook()).apply_snapshot(
                event.get("bids") or [], event.get("asks") or []
            )
            self._persist(token, ts_ms, sequence, received)

    async def run_socket(self, session: aiohttp.ClientSession, stop: asyncio.Event) -> None:
        tokens = sorted(self.token_meta)
        if not tokens:
            self._write_health(connected=False, force=True)
            try:
                await asyncio.wait_for(stop.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            return
        deadline = time.monotonic() + self.settings.book_market_refresh_sec
        try:
            async with session.ws_connect(
                self.settings.clob_ws_url,
                heartbeat=15,
                receive_timeout=30,
            ) as ws:
                await ws.send_json(
                    {"assets_ids": tokens, "type": "market", "custom_feature_enabled": True}
                )
                session_started = int(time.time() * 1000)
                self._write_health(
                    connected=True,
                    session_started_ms=session_started,
                    last_message_recv_ms=0,
                    force=True,
                )
                log.info(
                    "P2.6 book subscribed tokens=%d active_conditions=%d collected_conditions=%d",
                    len(tokens), self.active_condition_count, self.collected_condition_count,
                )
                while not stop.is_set() and time.monotonic() < deadline:
                    try:
                        message = await asyncio.wait_for(ws.receive(), timeout=1.0)
                    except asyncio.TimeoutError:
                        self._write_health(connected=True)
                        continue
                    received = int(time.time() * 1000)
                    if message.type == aiohttp.WSMsgType.TEXT:
                        self.last_message_recv_ms = received
                        try:
                            payload = json.loads(message.data)
                        except json.JSONDecodeError:
                            self._write_health(connected=True, last_message_recv_ms=received)
                            continue
                        events = payload if isinstance(payload, list) else [payload]
                        for event in events:
                            if isinstance(event, dict):
                                self.handle_event(event, recv_ms=received)
                        self._write_health(connected=True, last_message_recv_ms=received)
                    elif message.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                        break
        finally:
            self._write_health(connected=False, force=True)

    async def run(self, stop: asyncio.Event) -> None:
        async with aiohttp.ClientSession() as session:
            while not stop.is_set():
                try:
                    await self.refresh_registry(session)
                    await self.run_socket(session, stop)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    self._write_health(connected=False, force=True)
                    log.exception("P2.6 book collector cycle failed")
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=3.0)
                    except asyncio.TimeoutError:
                        pass
                cutoff = int(time.time() * 1000) - self.settings.book_history_retention_hours * 3_600_000
                self.books.prune(before_ts_ms=cutoff, batch_size=10_000)

    def close(self) -> None:
        self._write_health(connected=False, force=True)
        self.books.close()
        self.fees.close()


async def _run() -> None:
    settings = get_p26_settings()
    collector = BookCollector(settings)
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
