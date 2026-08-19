"""Polymarket CLOB market WebSocket feed.

Maintains per-token top-of-book plus a lightweight local book from:
- ``book`` full snapshots,
- ``price_change`` messages (official ``price_changes[]`` wire format),
- ``best_bid_ask`` messages when custom features are enabled.

All quote timestamps use local receive time.  Token routing is always based on
``asset_id``; a top-level market/condition id is never treated as a token id.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from typing import Optional

import aiohttp

from config import Settings
from models import ClobQuote
from wsbase import ReconnectingWSClient

log = logging.getLogger("direction_engine.clob")


def _num(v: object) -> Optional[float]:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _ps(lvl: object) -> tuple[Optional[float], Optional[float]]:
    if isinstance(lvl, dict):
        return _num(lvl.get("price")), _num(lvl.get("size"))
    if isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
        return _num(lvl[0]), _num(lvl[1])
    return None, None


class ClobQuoteStore:
    """token_id -> top quote + local book used by incremental updates."""

    def __init__(self) -> None:
        self.quotes: dict[str, ClobQuote] = {}
        self._books: dict[str, dict[str, dict[float, float]]] = {}
        self.counters = {
            "clob_book_events": 0,
            "clob_price_change_events": 0,
            "clob_best_bid_ask_events": 0,
            "clob_quote_updates": 0,
        }

    def _set_quote(self, token: str, bid: Optional[float], ask: Optional[float]) -> None:
        self.quotes[token] = ClobQuote(token, bid, ask)
        self.counters["clob_quote_updates"] += 1

    def apply_book(self, token: str, bids: list, asks: list) -> None:
        b: dict[float, float] = {}
        a: dict[float, float] = {}
        for lvl in bids or []:
            p, s = _ps(lvl)
            if p is not None and s is not None and s > 0:
                b[p] = s
        for lvl in asks or []:
            p, s = _ps(lvl)
            if p is not None and s is not None and s > 0:
                a[p] = s
        self._books[token] = {"bids": b, "asks": a}
        self.counters["clob_book_events"] += 1
        self._refresh_from_book(token)

    def apply_price_change(self, token: str, changes: list) -> None:
        """Apply one token's price/size deltas to its local book."""
        book = self._books.setdefault(token, {"bids": {}, "asks": {}})
        for ch in changes or []:
            if not isinstance(ch, dict):
                continue
            p, s = _num(ch.get("price")), _num(ch.get("size"))
            if p is None or s is None:
                continue
            side = str(ch.get("side", "")).upper()
            if side in ("BUY", "BID"):
                levels = book["bids"]
            elif side in ("SELL", "ASK"):
                levels = book["asks"]
            else:
                continue
            if s == 0:
                levels.pop(p, None)
            else:
                levels[p] = s
        self.counters["clob_price_change_events"] += 1
        self._refresh_from_book(token)

    def apply_best(
        self,
        token: str,
        bid: Optional[float],
        ask: Optional[float],
        *,
        count_event: bool = True,
    ) -> None:
        """Apply a reliable top-of-book quote without requiring a local book."""
        self._set_quote(token, bid, ask)
        if count_event:
            self.counters["clob_best_bid_ask_events"] += 1

    def _refresh_from_book(self, token: str) -> None:
        book = self._books.get(token)
        if book is None:
            return
        bid = max(book["bids"]) if book["bids"] else None
        ask = min(book["asks"]) if book["asks"] else None
        self._set_quote(token, bid, ask)

    def update(self, token_id: str, bid: Optional[float], ask: Optional[float]) -> None:
        """Backward-compatible direct quote setter used by tests."""
        self._set_quote(token_id, bid, ask)

    def get(self, token_id: str) -> Optional[ClobQuote]:
        return self.quotes.get(token_id)


class ClobOrderbookStream(ReconnectingWSClient):
    """Subscribe to the CLOB market channel for the active token set."""

    def __init__(
        self,
        settings: Settings,
        store: ClobQuoteStore,
        asset_ids: list[str],
        session: aiohttp.ClientSession,
    ) -> None:
        super().__init__(
            settings.clob_ws_url,
            "ClobBook",
            session,
            backoff_base=settings.backoff_base_sec,
            backoff_factor=settings.backoff_factor,
            backoff_cap=settings.backoff_cap_sec,
            recv_timeout=settings.ws_recv_timeout_sec,
        )
        self.store = store
        self.asset_ids = asset_ids

    async def _subscribe_payload(self) -> Optional[str]:
        return json.dumps(
            {
                "assets_ids": self.asset_ids,
                "type": "market",
                "custom_feature_enabled": True,
            }
        )

    def _handle_price_changes(self, ev: dict) -> None:
        """Handle the official price_change shape.

        Wire format:
        {event_type:"price_change", market:<condition>,
         price_changes:[{asset_id,price,size,side,best_bid,best_ask}, ...]}
        """
        raw_changes = ev.get("price_changes")
        # Legacy compatibility, but official 2025+ field is price_changes.
        if not isinstance(raw_changes, list):
            raw_changes = ev.get("changes")
        if not isinstance(raw_changes, list):
            return

        grouped: dict[str, list[dict]] = defaultdict(list)
        best_by_token: dict[str, tuple[Optional[float], Optional[float]]] = {}
        for ch in raw_changes:
            if not isinstance(ch, dict):
                continue
            token = ch.get("asset_id") or ch.get("assetId")
            if token is None:
                continue
            token = str(token)
            grouped[token].append(ch)
            bid = _num(ch.get("best_bid") if "best_bid" in ch else ch.get("bestBid"))
            ask = _num(ch.get("best_ask") if "best_ask" in ch else ch.get("bestAsk"))
            if bid is not None or ask is not None:
                best_by_token[token] = (bid, ask)

        for token, changes in grouped.items():
            self.store.apply_price_change(token, changes)
            # The message already provides authoritative best bid/ask for this token.
            # Use it to avoid a partial local book producing an incorrect top-of-book.
            if token in best_by_token:
                bid, ask = best_by_token[token]
                self.store.apply_best(token, bid, ask, count_event=False)

    async def _handle(self, raw: str) -> None:
        data = json.loads(raw)
        events = data if isinstance(data, list) else [data]
        for ev in events:
            if not isinstance(ev, dict):
                continue
            et = str(ev.get("event_type") or ev.get("type") or "").lower()

            # price_change is special: asset_id lives inside price_changes[].
            if et == "price_change" or "price_changes" in ev:
                self._handle_price_changes(ev)
                continue

            asset_id = ev.get("asset_id") or ev.get("assetId")
            if asset_id is None:
                # A condition/market id is not a token id; do not route it into the store.
                continue
            token = str(asset_id)

            if et == "book" or "bids" in ev or "asks" in ev:
                self.store.apply_book(token, ev.get("bids") or [], ev.get("asks") or [])
                continue

            if et == "best_bid_ask" or "best_bid" in ev or "bestBid" in ev:
                bid = _num(ev.get("best_bid") if "best_bid" in ev else ev.get("bestBid"))
                ask = _num(ev.get("best_ask") if "best_ask" in ev else ev.get("bestAsk"))
                self.store.apply_best(token, bid, ask)


class ClobSupervisor:
    """Restart the CLOB child stream when the active token set changes."""

    def __init__(
        self,
        settings: Settings,
        store: ClobQuoteStore,
        session: aiohttp.ClientSession,
        token_provider,
    ) -> None:
        self.settings = settings
        self.store = store
        self._session = session
        self._token_provider = token_provider
        self.last_change_ts: float = 0.0
        self._stream: Optional[ClobOrderbookStream] = None

    @property
    def transport_healthy(self) -> bool:
        return self._stream is not None and self._stream.connected

    async def run(self, stop: asyncio.Event) -> None:
        current: Optional[list[str]] = None
        child_stop: Optional[asyncio.Event] = None
        child_task: Optional[asyncio.Task] = None
        while not stop.is_set():
            ids = sorted(set(self._token_provider() or []))
            if ids and ids != current:
                if child_stop is not None:
                    child_stop.set()
                if child_task is not None:
                    try:
                        await child_task
                    except Exception:  # noqa: BLE001
                        pass
                current = ids
                child_stop = asyncio.Event()
                stream = ClobOrderbookStream(
                    self.settings, self.store, ids, self._session
                )
                self._stream = stream
                child_task = asyncio.create_task(stream.run(child_stop))
                self.last_change_ts = time.time()
                log.info("CLOB subscribed to %d tokens", len(ids))
            try:
                await asyncio.wait_for(stop.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
        if child_stop is not None:
            child_stop.set()
        if child_task is not None:
            try:
                await child_task
            except Exception:  # noqa: BLE001
                pass
        log.info("CLOB supervisor stopped")
