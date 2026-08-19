"""Direct Binance WS feed — fast source for P2.1 feature generation.

Maintains trade flow, a synchronized diff-depth local book and a bounded mark-price
series. The mark series samples trade prices and book mids at source timestamps so
100ms..30m feature windows can warm up without fabricating history.
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
    """Single symbol local book + trade and feature-price buffers."""

    def __init__(self, symbol: str, ring_max: int, feature_ring_max: Optional[int] = None) -> None:
        self.symbol = symbol.upper()
        self.book = LocalBook(symbol=self.symbol)
        self.trades: deque[Trade] = deque(maxlen=ring_max)
        self.prices: deque[tuple[int, float]] = deque(maxlen=ring_max)  # trade-only compatibility
        self.feature_prices: deque[tuple[int, float]] = deque(maxlen=feature_ring_max or max(ring_max, 24000))
        self.last_trade_ts_ms: int = 0
        self.last_depth_ts_ms: int = 0
        self.last_frame_recv_ms: float = 0.0
        self._pending: deque[dict] = deque()
        self._prev_u: int = 0

    def _append_feature_price(self, ts_ms: int, price: Optional[float]) -> None:
        if not ts_ms or price is None or price <= 0:
            return
        if self.feature_prices:
            last_ts, _ = self.feature_prices[-1]
            if ts_ms < last_ts:
                return
            if ts_ms == last_ts:
                self.feature_prices[-1] = (ts_ms, float(price))
                return
        self.feature_prices.append((ts_ms, float(price)))

    def feature_series(self) -> list[tuple[int, float]]:
        return list(self.feature_prices) if self.feature_prices else list(self.prices)

    def on_trade(self, data: dict) -> None:
        self.last_frame_recv_ms = time.time() * 1000
        try:
            price = float(data["p"])
            qty = float(data["q"])
            ts_ms = int(data.get("T") or data.get("E") or 0)
            is_buyer_maker = bool(data.get("m", False))
        except (KeyError, TypeError, ValueError):
            return
        self.trades.append(Trade(price, qty, ts_ms, is_buyer_maker))
        self.prices.append((ts_ms, price))
        self._append_feature_price(ts_ms, price)
        self.last_trade_ts_ms = ts_ms

    def on_depth(self, data: dict) -> None:
        self.last_frame_recv_ms = time.time() * 1000
        self.last_depth_ts_ms = int(data.get("E") or 0)
        if not self.book.synced:
            self._pending.append(data)
            return
        self._apply_diff(data)

    def transport_age_ms(self) -> Optional[float]:
        if self.last_frame_recv_ms <= 0:
            return None
        return max(0.0, time.time() * 1000 - self.last_frame_recv_ms)

    def source_event_age_ms(self) -> Optional[float]:
        newest = max(self.last_trade_ts_ms, self.last_depth_ts_ms)
        if newest <= 0:
            return None
        return max(0.0, time.time() * 1000 - newest)

    def last_trade_age_ms(self) -> Optional[float]:
        if self.last_trade_ts_ms <= 0:
            return None
        return max(0.0, time.time() * 1000 - self.last_trade_ts_ms)

    def apply_snapshot(self, snap: dict) -> None:
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
        replay = [ev for ev in self._pending if int(ev.get("u", 0)) >= last_id + 1]
        self._pending.clear()
        self._prev_u = last_id
        first = True
        for ev in replay:
            U, u = int(ev.get("U", 0)), int(ev.get("u", 0))
            if first:
                if not (U <= last_id + 1 <= u):
                    continue
                first = False
            self._apply_diff(ev, mark_synced=False)
        self.book.synced = True
        self._append_feature_price(self.last_depth_ts_ms or int(time.time() * 1000), self.book.mid)

    def _apply_diff(self, data: dict, mark_synced: bool = True) -> None:
        U, u = int(data.get("U", 0)), int(data.get("u", 0))
        if self.book.synced and self._prev_u and U > self._prev_u + 1:
            log.warning("%s: depth gap (U=%d prev_u=%d) -> resync", self.symbol, U, self._prev_u)
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
        self._append_feature_price(int(data.get("E") or self.last_depth_ts_ms or 0), self.book.mid)

    def spot_price(self) -> tuple[Optional[float], Optional[float]]:
        now_ms = time.time() * 1000
        candidates: list[tuple[float, float]] = []
        if self.prices:
            ts_ms, price = self.prices[-1]
            candidates.append((price, max(0.0, now_ms - ts_ms)))
        if self.book.synced and self.book.mid is not None and self.last_depth_ts_ms:
            candidates.append((self.book.mid, max(0.0, now_ms - self.last_depth_ts_ms)))
        if not candidates:
            return None, None
        return min(candidates, key=lambda c: c[1])

    def book_age_ms(self) -> Optional[float]:
        if not self.book.synced or self.last_depth_ts_ms == 0:
            return None
        return max(0.0, time.time() * 1000 - self.last_depth_ts_ms)


class BinanceFeed(ReconnectingWSClient):
    def __init__(self, settings: Settings, symbols: list[str], session: aiohttp.ClientSession) -> None:
        streams: list[str] = []
        for sym in symbols:
            s = sym.lower()
            streams.extend([f"{s}@trade", f"{s}@depth@{settings.binance_depth_ms}ms"])
        url = f"{settings.binance_ws_base}?streams={'/'.join(streams)}"
        super().__init__(
            url, "Binance", session,
            backoff_base=settings.backoff_base_sec,
            backoff_factor=settings.backoff_factor,
            backoff_cap=settings.backoff_cap_sec,
            recv_timeout=settings.ws_recv_timeout_sec,
        )
        self.settings = settings
        feature_ring_max = int(getattr(settings, "feature_price_ring_max", 24000))
        self.feeds = {sym.upper(): SymbolFeed(sym, settings.ring_buffer_max, feature_ring_max) for sym in symbols}
        self.clock_offset_ms: Optional[float] = None
        self.clock_checked_at: float = 0.0

    @property
    def clock_synced(self) -> bool:
        return self.clock_offset_ms is None or abs(self.clock_offset_ms) <= self.settings.max_clock_skew_ms

    async def refresh_clock(self) -> None:
        url = f"{self.settings.binance_rest_base}/api/v3/time"
        try:
            t0 = time.time() * 1000
            async with self._session.get(url, timeout=8) as resp:
                if resp.status != 200:
                    return
                payload = await resp.json()
            t1 = time.time() * 1000
            server = float(payload.get("serverTime", 0))
            if server > 0:
                self.clock_offset_ms = (t0 + t1) / 2.0 - server
                self.clock_checked_at = time.time()
        except Exception as exc:  # noqa: BLE001
            log.warning("clock offset unavailable: %s", exc)

    async def _on_connect(self) -> None:
        for feed in self.feeds.values():
            feed.book.synced = False
            feed._pending.clear()
        for sym in list(self.feeds):
            asyncio.create_task(self._snapshot(sym))
        asyncio.create_task(self.refresh_clock())

    async def _snapshot(self, symbol: str) -> None:
        url = f"{self.settings.binance_rest_base}/api/v3/depth"
        params = {"symbol": symbol, "limit": str(self.settings.binance_book_snapshot_limit)}
        await asyncio.sleep(0.5)
        try:
            async with self._session.get(url, params=params, timeout=12) as resp:
                if resp.status != 200:
                    log.warning("%s snapshot HTTP %s", symbol, resp.status)
                    return
                snap = await resp.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("%s snapshot unavailable: %s", symbol, exc)
            return
        feed = self.feeds.get(symbol)
        if feed is not None and isinstance(snap, dict):
            feed.apply_snapshot(snap)
            log.info("%s local book synced (lastUpdateId=%s)", symbol, snap.get("lastUpdateId"))

    async def _handle(self, raw: str) -> None:
        msg = json.loads(raw)
        stream = msg.get("stream") if isinstance(msg, dict) else None
        data = msg.get("data") if isinstance(msg, dict) else None
        if not stream or not isinstance(data, dict):
            return
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
