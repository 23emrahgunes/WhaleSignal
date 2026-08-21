"""Read-only P2.6 full-depth CLOB and dynamic-fee collector.

The daemon discovers active P2.5 conditions from the P2.5 SQLite database, reads
public CLOB V2 market metadata, subscribes to public market WebSocket data and
persists UP/DOWN order books plus fee lineage in the isolated P2.6 database.  It
contains no authentication, signing or order submission code.
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


@dataclass(frozen=True)
class ActiveMarket:
    condition_id: str
    combo_key: str
    market_end_ts_ms: int


def load_active_markets(
    p25_db_path: str,
    *,
    now_ms: Optional[int] = None,
) -> list[ActiveMarket]:
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    path = Path(p25_db_path).resolve()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(markets)").fetchall()
        }
        required = {"condition_id", "combo_key", "market_end"}
        if not required.issubset(columns):
            raise RuntimeError(f"P2.5 markets schema missing {sorted(required-columns)}")
        resolved_clause = "AND COALESCE(resolved,0)=0" if "resolved" in columns else ""
        rows = conn.execute(
            f"""
            SELECT condition_id,combo_key,market_end
            FROM markets
            WHERE condition_id IS NOT NULL
              AND market_end IS NOT NULL
              AND CAST(market_end*1000 AS INTEGER) BETWEEN ? AND ?
              {resolved_clause}
            ORDER BY market_end,condition_id
            """,
            (now - 60_000, now + 7_200_000),
        ).fetchall()
        return [
            ActiveMarket(
                str(row["condition_id"]),
                str(row["combo_key"]),
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

    async def refresh_registry(self, session: aiohttp.ClientSession) -> bool:
        markets = load_active_markets(self.settings.p25_db_path)
        condition_ids = {item.condition_id for item in markets}
        changed = False
        new_meta: dict[str, tuple[str, str, str]] = {}
        for market in markets:
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
            except Exception as exc:  # noqa: BLE001
                log.warning("market info unavailable condition=%s error=%r", market.condition_id, exc)
                continue
            mapping = self.fees.mapping(market.condition_id)
            if set(mapping) != {"UP", "DOWN"} or set(schedules) != {"UP", "DOWN"}:
                log.warning("incomplete token/fee mapping condition=%s mapping=%s", market.condition_id, mapping)
                continue
            for side, token_id in mapping.items():
                new_meta[token_id] = (market.condition_id, market.combo_key, side)
        self.fees.mark_active_conditions(condition_ids)
        if new_meta != self.token_meta:
            changed = True
            self.token_meta = new_meta
            self.local_books = {
                token: self.local_books.get(token, LocalBook()) for token in new_meta
            }
        return changed

    def _persist(self, token: str, ts_ms: int, sequence: Optional[int], recv_ms: int) -> None:
        meta = self.token_meta.get(token)
        book = self.local_books.get(token)
        if meta is None or book is None or not book.asks:
            return
        last = self.last_persist_ms.get(token, 0)
        if recv_ms - last < self.settings.book_persist_min_interval_ms:
            return
        condition_id, combo_key, side = meta
        snapshot = book.snapshot(token_id=token, ts_ms=ts_ms, sequence=sequence)
        if self.books.insert(
            condition_id=condition_id,
            combo_key=combo_key,
            side=side,
            snapshot=snapshot,
            recv_ts_ms=recv_ms,
        ):
            self.persisted += 1
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
            try:
                await asyncio.wait_for(stop.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            return
        deadline = time.monotonic() + self.settings.book_market_refresh_sec
        async with session.ws_connect(
            self.settings.clob_ws_url,
            heartbeat=15,
            receive_timeout=30,
        ) as ws:
            await ws.send_json(
                {"assets_ids": tokens, "type": "market", "custom_feature_enabled": True}
            )
            log.info("P2.6 book subscribed tokens=%d", len(tokens))
            while not stop.is_set() and time.monotonic() < deadline:
                try:
                    message = await asyncio.wait_for(ws.receive(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if message.type == aiohttp.WSMsgType.TEXT:
                    try:
                        payload = json.loads(message.data)
                    except json.JSONDecodeError:
                        continue
                    events = payload if isinstance(payload, list) else [payload]
                    for event in events:
                        if isinstance(event, dict):
                            self.handle_event(event)
                elif message.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                    break

    async def run(self, stop: asyncio.Event) -> None:
        async with aiohttp.ClientSession() as session:
            while not stop.is_set():
                try:
                    await self.refresh_registry(session)
                    await self.run_socket(session, stop)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    log.exception("P2.6 book collector cycle failed")
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=3.0)
                    except asyncio.TimeoutError:
                        pass
                cutoff = int(time.time() * 1000) - self.settings.book_history_retention_hours * 3_600_000
                self.books.prune(before_ts_ms=cutoff, batch_size=10_000)

    def close(self) -> None:
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
