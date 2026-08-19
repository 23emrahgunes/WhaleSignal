"""Polymarket CLOB WS feed — aktif marketlerin UP/DOWN token kotalari (teyit).

Aktif marketlerin token id'lerine CLOB `market` kanalindan abone olur; her token
icin best bid/ask/mid tutar. Aktif token kumesi degistikce (market donusu) abonelik
yeniden kurulur. Bu, yon modelinin Polymarket **teyit** girdisidir (edge degil).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
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
    """Bir level'dan (price, size)."""
    if isinstance(lvl, dict):
        return _num(lvl.get("price")), _num(lvl.get("size"))
    if isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
        return _num(lvl[0]), _num(lvl[1])
    return None, None


class ClobQuoteStore:
    """token_id -> ClobQuote + per-token LOCAL BOOK (incremental price_change uygulanir).

    `book` snapshot + `price_change` delta + `best_bid_ask` event'leri islenir; her
    guncellemede quote timestamp = LOCAL RECEIVE TIME ile yenilenir. Sayaclar incremental
    akisin islendiginin kanitidir.
    """

    def __init__(self) -> None:
        self.quotes: dict[str, ClobQuote] = {}
        self._books: dict[str, dict[str, dict[float, float]]] = {}  # token -> {bids,asks}
        self.counters = {
            "clob_book_events": 0,
            "clob_price_change_events": 0,
            "clob_best_bid_ask_events": 0,
            "clob_quote_updates": 0,
        }

    def apply_book(self, token: str, bids: list, asks: list) -> None:
        b: dict[float, float] = {}
        a: dict[float, float] = {}
        for lvl in bids or []:
            p, s = _ps(lvl)
            if p is not None and s and s > 0:
                b[p] = s
        for lvl in asks or []:
            p, s = _ps(lvl)
            if p is not None and s and s > 0:
                a[p] = s
        self._books[token] = {"bids": b, "asks": a}
        self.counters["clob_book_events"] += 1
        self._refresh_from_book(token)

    def apply_price_change(self, token: str, changes: list) -> None:
        book = self._books.setdefault(token, {"bids": {}, "asks": {}})
        for ch in changes or []:
            if not isinstance(ch, dict):
                continue
            p, s = _num(ch.get("price")), _num(ch.get("size"))
            if p is None or s is None:
                continue
            side = str(ch.get("side", "")).upper()
            d = book["bids"] if side in ("BUY", "BID") else book["asks"]
            if s == 0:
                d.pop(p, None)
            else:
                d[p] = s
        self.counters["clob_price_change_events"] += 1
        self._refresh_from_book(token)

    def apply_best(self, token: str, bid: Optional[float], ask: Optional[float]) -> None:
        """best_bid_ask event: book olmadan dogrudan best (LOCAL RECEIVE TIME)."""
        self.quotes[token] = ClobQuote(token, bid, ask)  # ts=now
        self.counters["clob_best_bid_ask_events"] += 1
        self.counters["clob_quote_updates"] += 1

    def _refresh_from_book(self, token: str) -> None:
        book = self._books.get(token)
        if book is None:
            return
        bid = max(book["bids"]) if book["bids"] else None
        ask = min(book["asks"]) if book["asks"] else None
        self.quotes[token] = ClobQuote(token, bid, ask)  # ts=now (local receive time)
        self.counters["clob_quote_updates"] += 1

    def update(self, token_id: str, bid: Optional[float], ask: Optional[float]) -> None:
        """Geriye uyum (test/basit yol)."""
        self.quotes[token_id] = ClobQuote(token_id, bid, ask)

    def get(self, token_id: str) -> Optional[ClobQuote]:
        return self.quotes.get(token_id)


class ClobOrderbookStream(ReconnectingWSClient):
    """Verilen token kumesine CLOB `market` kanalindan abone olur."""

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
        # market kanali; incremental event'ler icin feature flag
        return json.dumps(
            {"assets_ids": self.asset_ids, "type": "market", "custom_feature_enabled": True}
        )

    async def _handle(self, raw: str) -> None:
        data = json.loads(raw)
        events = data if isinstance(data, list) else [data]
        for ev in events:
            if not isinstance(ev, dict):
                continue
            et = str(ev.get("event_type") or ev.get("type") or "").lower()
            asset_id = ev.get("asset_id") or ev.get("market")
            if not asset_id:
                continue
            aid = str(asset_id)
            # 1) book (full snapshot)
            if et == "book" or "bids" in ev or "asks" in ev:
                self.store.apply_book(aid, ev.get("bids") or [], ev.get("asks") or [])
                continue
            # 2) price_change (delta veya best tasiyabilir)
            if et == "price_change" or "changes" in ev:
                changes = ev.get("changes")
                if isinstance(changes, list):
                    self.store.apply_price_change(aid, changes)
                else:
                    bb = _num(ev.get("best_bid") or ev.get("bestBid"))
                    ba = _num(ev.get("best_ask") or ev.get("bestAsk"))
                    if bb is not None or ba is not None:
                        self.store.apply_best(aid, bb, ba)
                continue
            # 3) best_bid_ask
            if et == "best_bid_ask" or "best_bid" in ev or "bestBid" in ev:
                bb = _num(ev.get("best_bid") or ev.get("bestBid"))
                ba = _num(ev.get("best_ask") or ev.get("bestAsk"))
                self.store.apply_best(aid, bb, ba)


class ClobSupervisor:
    """Aktif token kumesi degistikce CLOB akisini yeniden baslatir.

    `token_provider()` -> mevcut aktif token id listesi (discovery'den). Degisince
    eski akis durdurulur, yeni token'lara abone olunur.
    """

    def __init__(
        self,
        settings: Settings,
        store: ClobQuoteStore,
        session: aiohttp.ClientSession,
        token_provider,  # Callable[[], list[str]]
    ) -> None:
        self.settings = settings
        self.store = store
        self._session = session
        self._token_provider = token_provider
        self.last_change_ts: float = 0.0
        self._stream: Optional[ClobOrderbookStream] = None  # aktif WS (transport health)

    @property
    def transport_healthy(self) -> bool:
        """CLOB WS bagli mi (transport). Usable-quote health'ten AYRI kavram."""
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
                stream = ClobOrderbookStream(self.settings, self.store, ids, self._session)
                self._stream = stream
                child_task = asyncio.create_task(stream.run(child_stop))
                self.last_change_ts = time.time()
                log.info("CLOB abone (yeni token kumesi): %d token", len(ids))
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
        log.info("CLOB supervisor durduruldu")
