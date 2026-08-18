"""Paylasilan WS taban sinifi — kopma/bozuk-veri toleransli, exponential backoff.

Hem `binance_feed` hem `clob_feed` bu tabani kullanir. `run` calisirken hicbir
istisna surecleri dusurmez; her hata backoff ile yeniden baglanmayla sonuclanir.
(dual-arbitraj `ReconnectingWSClient` deseninin sadelestirilmis kopyasi.)
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable, Optional

import aiohttp

log = logging.getLogger("direction_engine.ws")


def backoff_delay(attempt: int, base: float, factor: float, cap: float) -> float:
    """min(cap, base * factor**attempt)."""
    return min(cap, base * (factor ** max(0, attempt)))


class ReconnectingWSClient:
    """Alt siniflar `_subscribe_payload` (ops.) ve `_handle` uygular."""

    def __init__(
        self,
        url: str,
        name: str,
        session: aiohttp.ClientSession,
        *,
        backoff_base: float = 1.0,
        backoff_factor: float = 2.0,
        backoff_cap: float = 30.0,
        recv_timeout: float = 30.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.url = url
        self.name = name
        self._session = session
        self._backoff_base = backoff_base
        self._backoff_factor = backoff_factor
        self._backoff_cap = backoff_cap
        self._recv_timeout = recv_timeout
        self._sleep = sleep
        self.reconnects = 0
        self.messages_handled = 0
        self.connected = False

    async def _subscribe_payload(self) -> Optional[str]:
        return None

    async def _on_connect(self) -> None:
        """Baglanti kurulunca (abonelikten once) cagrilir. Ops. override."""
        return None

    async def _handle(self, raw: str) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    async def _safe_handle(self, raw: str) -> None:
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
                    self.url, heartbeat=20, receive_timeout=self._recv_timeout
                ) as ws:
                    attempt = 0
                    self.connected = True
                    await self._on_connect()
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
                    attempt, self._backoff_base, self._backoff_factor, self._backoff_cap
                )
                attempt += 1
                log.warning(
                    "%s: baglanti hatasi (%s); %.1fs sonra yeniden", self.name, exc, delay
                )
                await self._sleep(delay)
            finally:
                self.connected = False
        log.info("%s: durduruldu", self.name)
