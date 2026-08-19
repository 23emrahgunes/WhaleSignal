"""Chainlink official reference collector — Polymarket RTDS Data Stream.

Polymarket's short-duration crypto Up/Down markets resolve from Chainlink Data
Streams (for example BTC/USD), not from a Polygon push-feed aggregator and not
from Binance.  This collector therefore consumes Polymarket's public RTDS
`crypto_prices_chainlink` topic and keeps both the latest point and a short
source-timestamped history per asset.

Important invariants:
- subscription follows the documented RTDS wire format;
- application-level text ``PING`` is sent every 5 seconds;
- opening references are selected from RTDS source timestamps near the canonical
  market start, never from the current price after a mid-window restart;
- Binance remains a separate analytics/proxy source elsewhere in the service.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

import aiohttp

from config import Settings
from wsbase import backoff_delay

log = logging.getLogger("direction_engine.chainlink")

RTDS_URL = "wss://ws-live-data.polymarket.com"
RTDS_PING_SEC = 5.0
_HISTORY_MAX = 4096

_SYMBOL_MAP = {
    "btc": "BTC", "btcusd": "BTC", "btc/usd": "BTC", "btc-usd": "BTC", "bitcoin": "BTC",
    "eth": "ETH", "ethusd": "ETH", "eth/usd": "ETH", "eth-usd": "ETH", "ethereum": "ETH",
    "sol": "SOL", "solusd": "SOL", "sol/usd": "SOL", "sol-usd": "SOL", "solana": "SOL",
    "xrp": "XRP", "xrpusd": "XRP", "xrp/usd": "XRP", "xrp-usd": "XRP", "ripple": "XRP",
}


def map_symbol(raw: object) -> Optional[str]:
    if raw is None:
        return None
    return _SYMBOL_MAP.get(str(raw).strip().lower())


def _to_ts_seconds(raw: object, fallback: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return fallback
    return value / 1000.0 if value > 1e12 else value


def _to_price(payload: dict) -> Optional[float]:
    # Prefer the full-accuracy value when RTDS supplies it.
    for key in ("full_accuracy_value", "value", "price", "p", "px", "answer", "last"):
        value = payload.get(key)
        if value is None:
            continue
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        if price > 0:
            return price
    return None


@dataclass(frozen=True)
class ChainlinkState:
    value: float
    source_ts: float  # Chainlink/RTDS observation timestamp, seconds
    recv_ts: float  # local receipt timestamp, seconds

    def age_ms(self, now: Optional[float] = None) -> float:
        """Local receive age; useful for transport diagnostics."""
        now = time.time() if now is None else now
        return max(0.0, (now - self.recv_ts) * 1000.0)

    def source_age_ms(self, now: Optional[float] = None) -> float:
        """Age of the actual source observation."""
        now = time.time() if now is None else now
        return max(0.0, (now - self.source_ts) * 1000.0)


def parse_chainlink_payload(payload: dict, recv_ts: Optional[float] = None) -> Optional[tuple[str, ChainlinkState]]:
    """Parse a documented ``crypto_prices_chainlink`` payload."""
    if not isinstance(payload, dict):
        return None
    asset = map_symbol(payload.get("symbol") or payload.get("asset") or payload.get("pair"))
    if asset is None:
        return None
    value = _to_price(payload)
    if value is None:
        return None
    recv_ts = time.time() if recv_ts is None else recv_ts
    source_ts = _to_ts_seconds(
        payload.get("timestamp") or payload.get("source_ts") or payload.get("time"), recv_ts
    )
    return asset, ChainlinkState(value=value, source_ts=source_ts, recv_ts=recv_ts)


class ChainlinkFeed:
    """Polymarket RTDS Chainlink stream with per-asset opening-point history."""

    def __init__(self, settings: Settings, session: aiohttp.ClientSession) -> None:
        self.settings = settings
        self._session = session
        self.url = RTDS_URL
        self.connected = False
        self.reconnects = 0
        self.messages_handled = 0
        self.state: dict[str, ChainlinkState] = {}
        self.history: dict[str, deque[ChainlinkState]] = {
            asset: deque(maxlen=_HISTORY_MAX) for asset in ("BTC", "ETH", "SOL", "XRP")
        }
        self._raw_logged = 0

    @staticmethod
    def subscribe_message() -> dict:
        """Official documented subscription: all Chainlink crypto symbols."""
        return {
            "action": "subscribe",
            "subscriptions": [
                {"topic": "crypto_prices_chainlink", "type": "*", "filters": ""}
            ],
        }

    def _record(self, asset: str, point: ChainlinkState) -> None:
        current = self.state.get(asset)
        # Ignore an exact duplicate replay, but keep out-of-order points in history if useful.
        if current is None or point.source_ts >= current.source_ts:
            self.state[asset] = point
        hist = self.history.setdefault(asset, deque(maxlen=_HISTORY_MAX))
        if not hist or hist[-1].source_ts != point.source_ts or hist[-1].value != point.value:
            hist.append(point)

    def _parse_message(self, obj: object, recv_ts: float) -> int:
        if not isinstance(obj, dict):
            return 0
        topic = str(obj.get("topic") or "")
        if topic and topic != "crypto_prices_chainlink":
            return 0

        payload = obj.get("payload", obj)
        parsed_count = 0
        if isinstance(payload, dict):
            # Some subscribe snapshots use a shared symbol plus data[] points.
            data = payload.get("data")
            if isinstance(data, list):
                shared_symbol = payload.get("symbol")
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    candidate = dict(item)
                    if shared_symbol is not None and "symbol" not in candidate:
                        candidate["symbol"] = shared_symbol
                    parsed = parse_chainlink_payload(candidate, recv_ts)
                    if parsed is not None:
                        self._record(*parsed)
                        parsed_count += 1
            else:
                parsed = parse_chainlink_payload(payload, recv_ts)
                if parsed is not None:
                    self._record(*parsed)
                    parsed_count += 1
        elif isinstance(payload, list):
            for item in payload:
                parsed = parse_chainlink_payload(item, recv_ts) if isinstance(item, dict) else None
                if parsed is not None:
                    self._record(*parsed)
                    parsed_count += 1
        return parsed_count

    async def _handle_text(self, raw: str) -> int:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return 0
        recv_ts = time.time()
        if isinstance(data, list):
            return sum(self._parse_message(item, recv_ts) for item in data)
        return self._parse_message(data, recv_ts)

    async def _ping_loop(self, ws: aiohttp.ClientWebSocketResponse, stop: asyncio.Event) -> None:
        """RTDS requires an application-level text PING every 5 seconds."""
        while not stop.is_set() and not ws.closed:
            try:
                await ws.send_str("PING")
            except Exception:  # noqa: BLE001
                return
            try:
                await asyncio.wait_for(stop.wait(), timeout=RTDS_PING_SEC)
            except asyncio.TimeoutError:
                pass

    async def run(self, stop: asyncio.Event) -> None:
        if not getattr(self.settings, "chainlink_enabled", True):
            log.info("Chainlink RTDS disabled")
            return

        attempt = 0
        while not stop.is_set():
            ping_task: Optional[asyncio.Task] = None
            try:
                async with self._session.ws_connect(
                    self.url,
                    heartbeat=None,
                    receive_timeout=self.settings.ws_recv_timeout_sec,
                ) as ws:
                    self.connected = True
                    attempt = 0
                    await ws.send_str(json.dumps(self.subscribe_message()))
                    ping_task = asyncio.create_task(self._ping_loop(ws, stop))
                    log.info("Chainlink RTDS connected: crypto_prices_chainlink")

                    async for msg in ws:
                        if stop.is_set():
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            raw = msg.data.strip()
                            if raw == "PING":
                                await ws.send_str("PONG")
                                continue
                            if raw == "PONG":
                                continue
                            if self._raw_logged < 4:
                                self._raw_logged += 1
                                log.info("CHAINLINK RTDS RAW[%d]: %s", self._raw_logged, raw[:500])
                            parsed = await self._handle_text(raw)
                            self.messages_handled += 1
                            if parsed and len(self.state) == 4 and self.messages_handled < 10:
                                log.info(
                                    "Chainlink RTDS feeds ready: %s",
                                    {a: round(s.value, 6) for a, s in self.state.items()},
                                )
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
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
                log.warning("Chainlink RTDS connection error (%s); retry in %.1fs", exc, delay)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
            finally:
                self.connected = False
                if ping_task is not None:
                    ping_task.cancel()
                    try:
                        await ping_task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
        log.info("Chainlink RTDS stopped")

    def get_state(self, asset: str) -> Optional[ChainlinkState]:
        return self.state.get(asset)

    def opening_state(
        self,
        asset: str,
        market_start_ts: float,
        max_alignment_ms: float = 5000.0,
    ) -> Optional[ChainlinkState]:
        """Return the RTDS observation nearest the canonical market start.

        Only source timestamps within ``max_alignment_ms`` are accepted.  This
        makes a mid-window process restart fail closed instead of inventing a PTB.
        """
        hist = self.history.get(asset)
        if not hist:
            return None
        best = min(hist, key=lambda point: abs(point.source_ts - market_start_ts))
        if abs(best.source_ts - market_start_ts) * 1000.0 > max_alignment_ms:
            return None
        return best

    def status(self) -> dict:
        now = time.time()
        return {
            "connection": "connected" if self.connected else "disconnected",
            "reconnects": self.reconnects,
            "messages": self.messages_handled,
            "feeds": {
                asset: {
                    "value": round(point.value, 8),
                    "source_ts": point.source_ts,
                    "source_age_ms": round(point.source_age_ms(now), 0),
                    "recv_age_ms": round(point.age_ms(now), 0),
                    "history_points": len(self.history.get(asset, ())),
                }
                for asset, point in self.state.items()
            },
        }
