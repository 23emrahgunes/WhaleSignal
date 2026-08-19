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


def _best(levels: object, side: str) -> Optional[float]:
    """bids -> en yuksek fiyat; asks -> en dusuk fiyat."""
    if not isinstance(levels, list) or not levels:
        return None
    prices: list[float] = []
    for lvl in levels:
        try:
            prices.append(float(lvl["price"]))
        except (KeyError, TypeError, ValueError):
            continue
    if not prices:
        return None
    return max(prices) if side == "bids" else min(prices)


class ClobQuoteStore:
    """token_id -> ClobQuote (thread-safe degil; tek event-loop icinde kullanilir)."""

    def __init__(self) -> None:
        self.quotes: dict[str, ClobQuote] = {}

    def update(self, token_id: str, bid: Optional[float], ask: Optional[float]) -> None:
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
        return json.dumps({"assets_ids": self.asset_ids, "type": "market"})

    async def _handle(self, raw: str) -> None:
        data = json.loads(raw)
        events = data if isinstance(data, list) else [data]
        for ev in events:
            if not isinstance(ev, dict):
                continue
            asset_id = ev.get("asset_id") or ev.get("market")
            if not asset_id:
                continue
            if "bids" in ev or "asks" in ev:
                bid = _best(ev.get("bids"), "bids")
                ask = _best(ev.get("asks"), "asks")
                self.store.update(str(asset_id), bid, ask)


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
