"""Direct Binance WS feed — hizli kaynak (~124ms), 4 varlik.

Her sembol icin:
  - `<sym>@trade`      -> agresif islem akisi (signed flow, ring buffer)
  - `<sym>@depth@100ms`-> **diff-depth** olaylari -> REST snapshot ile senkronize
                          **LOCAL ORDER BOOK** (gercek OFI icin; partial depth20 DEGIL)

Diff-depth senkronizasyonu (Binance resmi algoritma):
  1. depth akisini dinle, olaylari tamponla
  2. REST `/depth?limit=1000` snapshot al -> lastUpdateId
  3. u < lastUpdateId+1 olan tampon olaylarini at
  4. ilk uygulanan olay: U <= lastUpdateId+1 <= u
  5. sonraki her olay U == onceki_u + 1 olmali (degilse yeniden senkron)
  6. miktar 0 -> seviye sil; degilse ata

Tek birlesik WS (`/stream?streams=...`) ile tum semboller dinlenir; mesajlar
`{"stream": "...", "data": {...}}` sarmalindadir.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Optional

import aiohttp

from config import Settings
from models import LocalBook, Trade
from wsbase import ReconnectingWSClient

log = logging.getLogger("direction_engine.binance")


class SymbolFeed:
    """Tek sembolun yerel defteri + islem/fiyat ring buffer'lari."""

    def __init__(self, symbol: str, ring_max: int) -> None:
        self.symbol = symbol.upper()
        self.book = LocalBook(symbol=self.symbol)
        self.trades: deque[Trade] = deque(maxlen=ring_max)
        self.prices: deque[tuple[int, float]] = deque(maxlen=ring_max)  # (ts_ms, price)
        self.last_trade_ts_ms: int = 0
        self.last_depth_ts_ms: int = 0
        self._pending: deque[dict] = deque()  # senkron oncesi tamponlanan diff olaylari
        self._prev_u: int = 0

    # ---- trade akisi ----
    def on_trade(self, data: dict) -> None:
        try:
            price = float(data["p"])
            qty = float(data["q"])
            ts_ms = int(data.get("T") or data.get("E") or 0)
            is_buyer_maker = bool(data.get("m", False))
        except (KeyError, TypeError, ValueError):
            return
        self.trades.append(Trade(price, qty, ts_ms, is_buyer_maker))
        self.prices.append((ts_ms, price))
        self.last_trade_ts_ms = ts_ms

    # ---- diff-depth akisi ----
    def on_depth(self, data: dict) -> None:
        self.last_depth_ts_ms = int(data.get("E") or 0)
        if not self.book.synced:
            self._pending.append(data)
            return
        self._apply_diff(data)

    def apply_snapshot(self, snap: dict) -> None:
        """REST snapshot uygula + tamponlanan diff'leri oynat (senkron kur)."""
        try:
            last_id = int(snap["lastUpdateId"])
        except (KeyError, TypeError, ValueError):
            return
        bids: dict[float, float] = {}
        asks: dict[float, float] = {}
        for p, s in snap.get("bids", []):
            fp, fs = float(p), float(s)
            if fs > 0:
                bids[fp] = fs
        for p, s in snap.get("asks", []):
            fp, fs = float(p), float(s)
            if fs > 0:
                asks[fp] = fs
        self.book.bids = bids
        self.book.asks = asks
        self.book.last_update_id = last_id
        self.book.ts = time.time()
        # tamponu oynat: u < last_id+1 at; kalanini uygula
        replay = [ev for ev in self._pending if int(ev.get("u", 0)) >= last_id + 1]
        self._pending.clear()
        self._prev_u = last_id
        first = True
        for ev in replay:
            U, u = int(ev.get("U", 0)), int(ev.get("u", 0))
            if first:
                if not (U <= last_id + 1 <= u):
                    continue  # bosluk: bu olayi atla, senkron bir sonraki uygun olayda
                first = False
            self._apply_diff(ev, mark_synced=False)
        self.book.synced = True

    def _apply_diff(self, data: dict, mark_synced: bool = True) -> None:
        U, u = int(data.get("U", 0)), int(data.get("u", 0))
        # sureklilik kontrolu: kopukluk varsa yeniden senkron gerek
        if self.book.synced and self._prev_u and U > self._prev_u + 1:
            log.warning("%s: depth bosluk (U=%d prev_u=%d) -> yeniden senkron", self.symbol, U, self._prev_u)
            self.book.synced = False
            self._pending.clear()
            self._pending.append(data)
            return
        for p, s in data.get("b", []):
            fp, fs = float(p), float(s)
            if fs == 0:
                self.book.bids.pop(fp, None)
            else:
                self.book.bids[fp] = fs
        for p, s in data.get("a", []):
            fp, fs = float(p), float(s)
            if fs == 0:
                self.book.asks.pop(fp, None)
            else:
                self.book.asks[fp] = fs
        self._prev_u = u
        self.book.last_update_id = u
        self.book.ts = time.time()

    # ---- okuyucular ----
    def spot_price(self) -> tuple[Optional[float], Optional[float]]:
        """(son_fiyat, yas_ms). Son trade ile book-mid'den DAHA TAZE olani sec.

        Dusuk hacimli varliklarda (SOL/XRP) trade'ler seyrek gelir; book diff-depth
        ile ~100ms'de guncellendiginden book-mid genelde daha tazedir -> spot bayat
        sanilmaz. Fiyat ring'i yine trade-tabanli (returns/vol icin)."""
        now_ms = time.time() * 1000
        candidates: list[tuple[float, float]] = []  # (price, age_ms)
        if self.prices:
            ts_ms, price = self.prices[-1]
            candidates.append((price, max(0.0, now_ms - ts_ms)))
        if self.book.synced and self.book.mid is not None and self.last_depth_ts_ms:
            candidates.append((self.book.mid, max(0.0, now_ms - self.last_depth_ts_ms)))
        if not candidates:
            return None, None
        price, age = min(candidates, key=lambda c: c[1])  # en taze
        return price, age

    def book_age_ms(self) -> Optional[float]:
        if not self.book.synced or self.last_depth_ts_ms == 0:
            return None
        return max(0.0, time.time() * 1000 - self.last_depth_ts_ms)


class BinanceFeed(ReconnectingWSClient):
    """Tum semboller icin tek birlesik WS + REST snapshot senkronizasyonu."""

    def __init__(
        self,
        settings: Settings,
        symbols: list[str],
        session: aiohttp.ClientSession,
    ) -> None:
        depth_ms = settings.binance_depth_ms
        streams: list[str] = []
        for sym in symbols:
            s = sym.lower()
            streams.append(f"{s}@trade")
            streams.append(f"{s}@depth@{depth_ms}ms")
        url = f"{settings.binance_ws_base}?streams={'/'.join(streams)}"
        super().__init__(
            url,
            "Binance",
            session,
            backoff_base=settings.backoff_base_sec,
            backoff_factor=settings.backoff_factor,
            backoff_cap=settings.backoff_cap_sec,
            recv_timeout=settings.ws_recv_timeout_sec,
        )
        self.settings = settings
        self.feeds: dict[str, SymbolFeed] = {
            sym.upper(): SymbolFeed(sym, settings.ring_buffer_max) for sym in symbols
        }

    async def _on_connect(self) -> None:
        # yeni baglantida her sembol icin snapshot'i yeniden al (senkron sifirla)
        for feed in self.feeds.values():
            feed.book.synced = False
            feed._pending.clear()
        for sym in list(self.feeds):
            asyncio.create_task(self._snapshot(sym))

    async def _snapshot(self, symbol: str) -> None:
        """REST snapshot al ve local book'u senkronize et."""
        url = f"{self.settings.binance_rest_base}/api/v3/depth"
        params = {"symbol": symbol, "limit": str(self.settings.binance_book_snapshot_limit)}
        # diff akisinin birkac olay tamponlamasi icin minik gecikme
        await asyncio.sleep(0.5)
        try:
            async with self._session.get(url, params=params, timeout=12) as resp:
                if resp.status != 200:
                    log.warning("%s snapshot HTTP %s", symbol, resp.status)
                    return
                snap = await resp.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("%s snapshot alinamadi: %s", symbol, exc)
            return
        feed = self.feeds.get(symbol)
        if feed is not None and isinstance(snap, dict):
            feed.apply_snapshot(snap)
            log.info("%s local book senkron (lastUpdateId=%s)", symbol, snap.get("lastUpdateId"))

    async def _handle(self, raw: str) -> None:
        msg = json.loads(raw)
        stream = msg.get("stream") if isinstance(msg, dict) else None
        data = msg.get("data") if isinstance(msg, dict) else None
        if not stream or not isinstance(data, dict):
            return
        # stream: "btcusdt@trade" | "btcusdt@depth@100ms"
        sym_part, _, kind = stream.partition("@")
        feed = self.feeds.get(sym_part.upper())
        if feed is None:
            return
        if kind == "trade":
            feed.on_trade(data)
        elif kind.startswith("depth"):
            feed.on_depth(data)

    def get_feed(self, symbol: str) -> Optional[SymbolFeed]:
        return self.feeds.get(symbol.upper())
