"""Asenkron veri akisi katmani.

Polymarket CLOB WS, Binance kline WS, Deribit IV (REST) ve Polymarket Gamma
(REST) kaynaklarini paralel calistirir. Tum WS baglantilarinda bozuk JSON veya
kopma durumunda **cokmeden exponential backoff** ile yeniden baglanilir.
`DataHub` en guncel durumu tek noktada birlestirir; strateji buradan okur.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Awaitable, Callable, Optional

import aiohttp

from config import Settings
from models import BookLevel, Candle, MarketMeta, OrderBook

log = logging.getLogger("dual_arbitraj.ingest")


def backoff_delay(attempt: int, base: float, factor: float, cap: float) -> float:
    """Exponential backoff gecikmesi: min(cap, base * factor**attempt)."""
    return min(cap, base * (factor ** max(0, attempt)))


# ----------------------------------------------------------------------------
# DataHub — birlesik anlik durum
# ----------------------------------------------------------------------------


class DataHub:
    """Tum kaynaklarin en guncel durumunu tutan tek okuma noktasi."""

    def __init__(self, max_candles: int = 300) -> None:
        self.book_up: Optional[OrderBook] = None
        self.book_down: Optional[OrderBook] = None
        self.candles: deque[Candle] = deque(maxlen=max_candles)
        self.implied_vol: float = 0.0
        self.meta: Optional[MarketMeta] = None
        self.updated_at: float = 0.0
        self._lock = asyncio.Lock()

    async def set_book(self, token_id: str, book: OrderBook) -> None:
        async with self._lock:
            if self.meta and token_id == self.meta.up_token_id:
                self.book_up = book
            elif self.meta and token_id == self.meta.down_token_id:
                self.book_down = book
            else:
                # meta henuz yoksa ilk gelen up, ikinci down varsay.
                if self.book_up is None:
                    self.book_up = book
                elif self.book_down is None and (
                    self.book_up.token_id != token_id
                ):
                    self.book_down = book
            self.updated_at = time.time()

    async def add_candle(self, candle: Candle) -> None:
        async with self._lock:
            if self.candles and self.candles[-1].open_time == candle.open_time:
                self.candles[-1] = candle
            else:
                self.candles.append(candle)
            self.updated_at = time.time()

    async def set_iv(self, iv: float) -> None:
        async with self._lock:
            self.implied_vol = iv

    async def set_meta(self, meta: MarketMeta) -> None:
        async with self._lock:
            self.meta = meta

    def snapshot(self) -> tuple[
        Optional[MarketMeta], Optional[OrderBook], Optional[OrderBook], list[Candle], float
    ]:
        return (
            self.meta,
            self.book_up,
            self.book_down,
            list(self.candles),
            self.implied_vol,
        )


# ----------------------------------------------------------------------------
# Yeniden baglanan WS taban sinifi
# ----------------------------------------------------------------------------


class ReconnectingWSClient:
    """Kopma/bozuk-veri toleransli WS istemci tabani.

    Alt siniflar `_subscribe_payload` ve `_handle` uygular. `run` calisirken
    hicbir istisna surecleri dusurmez; her hata exponential backoff ile
    yeniden baglanmayla sonuclanir.
    """

    def __init__(
        self,
        url: str,
        settings: Settings,
        name: str,
        session: aiohttp.ClientSession,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.url = url
        self.settings = settings
        self.name = name
        self._session = session
        self._sleep = sleep
        self.reconnects = 0
        self.messages_handled = 0

    async def _subscribe_payload(self) -> Optional[str]:  # noqa: D401
        return None

    async def _handle(self, raw: str) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    async def _safe_handle(self, raw: str) -> None:
        """Tek mesaji isle; bozuk JSON veya handler hatasi baglantiyi dusurmez."""
        try:
            await self._handle(raw)
            self.messages_handled += 1
        except json.JSONDecodeError:
            log.warning("%s: bozuk JSON atlandi", self.name)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: mesaj isleme hatasi: %s", self.name, exc)

    async def run(self, stop: asyncio.Event) -> None:
        attempt = 0
        while not stop.is_set():
            try:
                async with self._session.ws_connect(
                    self.url, heartbeat=20, receive_timeout=self.settings.ws_recv_timeout_sec
                ) as ws:
                    attempt = 0  # basarili baglanti -> backoff sifirla
                    sub = await self._subscribe_payload()
                    if sub:
                        await ws.send_str(sub)
                    async for msg in ws:
                        if stop.is_set():
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._safe_handle(msg.data)
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.reconnects += 1
                delay = backoff_delay(
                    attempt,
                    self.settings.backoff_base_sec,
                    self.settings.backoff_factor,
                    self.settings.backoff_cap_sec,
                )
                attempt += 1
                log.warning(
                    "%s: baglanti hatasi (%s); %.1fs sonra yeniden denenecek",
                    self.name,
                    exc,
                    delay,
                )
                await self._sleep(delay)
        log.info("%s: durduruldu", self.name)


# ----------------------------------------------------------------------------
# Polymarket CLOB order book akisi
# ----------------------------------------------------------------------------


def _levels(raw_levels: object) -> list[BookLevel]:
    out: list[BookLevel] = []
    if not isinstance(raw_levels, list):
        return out
    for lvl in raw_levels:
        try:
            out.append(BookLevel(price=float(lvl["price"]), size=float(lvl["size"])))
        except (KeyError, TypeError, ValueError):
            continue
    # bids fiyat azalan, asks artan siralanir
    return out


class PolymarketOrderbookStream(ReconnectingWSClient):
    """CLOB `market` kanalindan canli emir defteri (bids/asks/midpoint)."""

    def __init__(
        self,
        settings: Settings,
        hub: DataHub,
        asset_ids: list[str],
        session: aiohttp.ClientSession,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        super().__init__(settings.clob_ws_url, settings, "PolyBook", session, sleep)
        self.hub = hub
        self.asset_ids = asset_ids

    async def _subscribe_payload(self) -> Optional[str]:
        return json.dumps({"assets_ids": self.asset_ids, "type": "market"})

    async def _handle(self, raw: str) -> None:
        data = json.loads(raw)
        events = data if isinstance(data, list) else [data]
        for ev in events:
            if not isinstance(ev, dict):
                continue
            etype = ev.get("event_type") or ev.get("type")
            asset_id = ev.get("asset_id") or ev.get("market")
            if not asset_id:
                continue
            if etype in ("book", None) and ("bids" in ev or "asks" in ev):
                bids = _levels(ev.get("bids"))
                asks = _levels(ev.get("asks"))
                bids.sort(key=lambda x: x.price, reverse=True)
                asks.sort(key=lambda x: x.price)
                book = OrderBook(token_id=str(asset_id), bids=bids, asks=asks)
                await self.hub.set_book(str(asset_id), book)


# ----------------------------------------------------------------------------
# Binance kline (1m) akisi -> ATR/ADX/hiz
# ----------------------------------------------------------------------------


class BinanceKlineStream(ReconnectingWSClient):
    def __init__(
        self,
        settings: Settings,
        hub: DataHub,
        session: aiohttp.ClientSession,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        stream = f"{settings.binance_ws_base}/{settings.symbol.lower()}@kline_1m"
        super().__init__(stream, settings, "BinanceKline", session, sleep)
        self.hub = hub

    async def _handle(self, raw: str) -> None:
        data = json.loads(raw)
        k = data.get("k") if isinstance(data, dict) else None
        if not isinstance(k, dict):
            return
        candle = Candle(
            open_time=int(k["t"]),
            open=float(k["o"]),
            high=float(k["h"]),
            low=float(k["l"]),
            close=float(k["c"]),
            volume=float(k["v"]),
            closed=bool(k.get("x", False)),
        )
        await self.hub.add_candle(candle)


# ----------------------------------------------------------------------------
# Deribit IV (DVOL) — REST polling
# ----------------------------------------------------------------------------


class DeribitVolatilityPoller:
    """Deribit DVOL volatilite indeksini periyodik olarak ceker (auxiliary)."""

    def __init__(
        self,
        settings: Settings,
        hub: DataHub,
        session: aiohttp.ClientSession,
        interval_sec: float = 30.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self.hub = hub
        self._session = session
        self.interval = interval_sec
        self._sleep = sleep

    async def _fetch_once(self) -> Optional[float]:
        end = int(time.time() * 1000)
        start = end - 6 * 3600 * 1000
        url = f"{self.settings.deribit_rest_base}/public/get_volatility_index_data"
        params = {
            "currency": self.settings.deribit_currency,
            "start_timestamp": start,
            "end_timestamp": end,
            "resolution": "3600",
        }
        async with self._session.get(url, params=params, timeout=10) as resp:
            payload = await resp.json()
        data = (payload.get("result") or {}).get("data") or []
        if not data:
            return None
        # her satir: [timestamp, open, high, low, close]
        return float(data[-1][4])

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                iv = await self._fetch_once()
                if iv is not None:
                    await self.hub.set_iv(iv)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("Deribit IV cekilemedi: %s", exc)
            await self._sleep(self.interval)


# ----------------------------------------------------------------------------
# Polymarket Gamma — market metadata / kalan sure
# ----------------------------------------------------------------------------


class GammaMetadataPoller:
    """Gamma API'den market metadata (token id'leri + endDate) cozer."""

    def __init__(
        self,
        settings: Settings,
        hub: DataHub,
        session: aiohttp.ClientSession,
        interval_sec: Optional[float] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self.hub = hub
        self._session = session
        self.interval = interval_sec if interval_sec is not None else settings.gamma_poll_sec
        self._sleep = sleep

    async def _fetch_btc_5m(self) -> Optional[MarketMeta]:
        """Aktif 5dk BTC up/down marketini /events/slug/btc-updown-5m-<pencere>
        ile cozer. Pencere = simdi 300 sn'ye yuvarlanmis unix; 5dk'da bir doner."""
        now = time.time()
        window_start = int(now) - (int(now) % 300)
        slug = f"btc-updown-5m-{window_start}"
        url = f"{self.settings.gamma_host}/events/slug/{slug}"
        async with self._session.get(url, timeout=10) as resp:
            if resp.status != 200:
                return None
            ev = await resp.json()
        if not isinstance(ev, dict) or ev.get("closed") or not ev.get("active"):
            return None
        markets = ev.get("markets") or []
        gm = next((m for m in markets if m.get("active") and not m.get("closed")), None)
        if gm is None:
            return None
        meta = _parse_gamma_market(gm)  # end_ts = Polymarket'in GERCEK endDate'i
        if meta is None:
            return None
        # start = gercek bitisten 5dk geri (duration/kalan-sure gercek endDate'e gore)
        meta.start_ts = meta.end_ts - 300.0
        if not meta.question:
            meta.question = str(ev.get("title", "BTC 5m Up/Down"))
        return meta

    async def _fetch_once(self) -> Optional[MarketMeta]:
        url = f"{self.settings.gamma_host}/markets"
        params = {"active": "true", "closed": "false", "limit": "50"}
        if self.settings.gamma_market_slug:
            params["slug"] = self.settings.gamma_market_slug
        async with self._session.get(url, params=params, timeout=10) as resp:
            markets = await resp.json()
        if not isinstance(markets, list):
            return None
        for m in markets:
            meta = _parse_gamma_market(m)
            if meta is not None:
                return meta
        return None

    async def run(self, stop: asyncio.Event) -> None:
        # Manuel token id verilmisse Gamma'yi atla.
        if self.settings.manual_up_token_id and self.settings.manual_down_token_id:
            now = time.time()
            end_ts = self.settings.manual_end_ts or (now + self.settings.manual_duration_sec)
            await self.hub.set_meta(
                MarketMeta(
                    condition_id="manual",
                    question="manual",
                    up_token_id=self.settings.manual_up_token_id,
                    down_token_id=self.settings.manual_down_token_id,
                    start_ts=end_ts - self.settings.manual_duration_sec,
                    end_ts=end_ts,
                )
            )
            return
        while not stop.is_set():
            try:
                if self.settings.btc_5m:
                    meta = await self._fetch_btc_5m()
                else:
                    meta = await self._fetch_once()
                if meta is not None:
                    await self.hub.set_meta(meta)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("Gamma metadata cekilemedi: %s", exc)
            await self._sleep(self.interval)


def _parse_gamma_market(m: object) -> Optional[MarketMeta]:
    """Gamma market kaydini MarketMeta'ya cevirir (UP/DOWN token eslesmesi)."""
    if not isinstance(m, dict):
        return None
    try:
        token_ids = m.get("clobTokenIds")
        outcomes = m.get("outcomes")
        if isinstance(token_ids, str):
            token_ids = json.loads(token_ids)
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        if not token_ids or not outcomes or len(token_ids) < 2:
            return None
        up_idx, down_idx = 0, 1
        low = [str(o).strip().lower() for o in outcomes]
        for i, name in enumerate(low):
            if name in ("up", "yes"):
                up_idx = i
            elif name in ("down", "no"):
                down_idx = i
        end_ts = _iso_to_ts(m.get("endDate"))
        start_ts = _iso_to_ts(m.get("startDate")) or (end_ts - 300)
        if end_ts <= 0:
            return None
        return MarketMeta(
            condition_id=str(m.get("conditionId", "")),
            question=str(m.get("question", "")),
            up_token_id=str(token_ids[up_idx]),
            down_token_id=str(token_ids[down_idx]),
            start_ts=start_ts,
            end_ts=end_ts,
        )
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _iso_to_ts(value: object) -> float:
    """ISO8601 (or Unix) -> unix saniye. Cozulemezse 0."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        from datetime import datetime

        s = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return 0.0
