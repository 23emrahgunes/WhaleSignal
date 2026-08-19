"""Chainlink OFFICIAL reference collector — Polygon ON-CHAIN aggregator.

5m/15m up/down marketleri **Chainlink** referansiyla resolve olur. Bu modul, marketlerin
resolve oldugu authoritative kaynagi — **Polygon zincirindeki Chainlink price aggregator**
kontratlarini — public RPC ile okur (`latestRoundData`). Binance DEGIL. Geoblock disi
(Polygon RPC her yerden erisilebilir), bu yuzden lokalde de dogrulanabilir.

Per-asset canli Chainlink state: value / source_ts (aggregator updatedAt) / recv_ts / age.
Yeni 5m/15m market rotate olurken bu degerden **opening reference** yakalanir
(reference/__init__ `CHAINLINK_ONCHAIN_CAPTURE`).

Not: aggregator heartbeat/deviation ile guncellenir; yakalanan deger acilis anindaki
Chainlink fiyatidir (kaynak Chainlink; Polymarket'in kesin resolve round'undan minik sapma
olabilir ama Binance proxy DEGIL).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp

from config import Settings

log = logging.getLogger("direction_engine.chainlink")

# Polygon mainnet Chainlink USD aggregator adresleri (8 decimals)
AGGREGATORS = {
    "BTC": "0xc907E116054Ad103354f2D350FD2514433D57F6f",
    "ETH": "0xF9680D99D6C9589e2a93a78A04A279e509205945",
    "SOL": "0x10C8264C0935b3B9870013e057f330Ff3e9C56dC",
    "XRP": "0x785ba89291f676b5386652eB12b30cF361020694",
}
_LATEST_ROUND_DATA = "0xfeaf968c"  # latestRoundData() selector
_DECIMALS = 1e8


@dataclass
class ChainlinkState:
    value: float
    source_ts: float  # aggregator updatedAt (Chainlink kaynak zamani)
    recv_ts: float  # yerel varis

    def age_ms(self, now: Optional[float] = None) -> float:
        now = time.time() if now is None else now
        return max(0.0, (now - self.recv_ts) * 1000.0)

    def source_age_ms(self, now: Optional[float] = None) -> float:
        now = time.time() if now is None else now
        return max(0.0, (now - self.source_ts) * 1000.0)


def decode_round_data(hex_result: str) -> Optional[tuple[float, int]]:
    """latestRoundData ABI ciktisini (answer/1e8, updatedAt) cozer."""
    if not hex_result or hex_result == "0x":
        return None
    h = hex_result[2:]
    if len(h) < 256:
        return None
    try:
        answer = int(h[64:128], 16)
        updated_at = int(h[192:256], 16)
    except ValueError:
        return None
    # int256 negatif kontrolu (fiyat pozitif olmali)
    if answer <= 0:
        return None
    return answer / _DECIMALS, updated_at


class ChainlinkFeed:
    """Polygon Chainlink aggregator poller (BTC/ETH/SOL/XRP). RPC failover'li."""

    def __init__(self, settings: Settings, session: aiohttp.ClientSession) -> None:
        self.settings = settings
        self._session = session
        self.state: dict[str, ChainlinkState] = {}
        self._rpcs = settings.polygon_rpc_urls()
        self._rpc_idx = 0
        self.connection_status = "init"
        self.poll_count = 0

    async def _eth_call(self, addr: str) -> Optional[tuple[float, int]]:
        """latestRoundData eth_call; RPC hata verirse sonrakine gecer (failover)."""
        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": addr, "data": _LATEST_ROUND_DATA}, "latest"],
        }
        for _ in range(max(1, len(self._rpcs))):
            rpc = self._rpcs[self._rpc_idx]
            try:
                async with self._session.post(rpc, json=payload, timeout=10) as r:
                    j = await r.json()
                if isinstance(j, dict) and j.get("result"):
                    dec = decode_round_data(j["result"])
                    if dec is not None:
                        return dec
                # error / bos -> RPC dondur
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass
            self._rpc_idx = (self._rpc_idx + 1) % len(self._rpcs)
        return None

    async def poll_once(self) -> None:
        now = time.time()
        got = 0
        for asset, addr in AGGREGATORS.items():
            dec = await self._eth_call(addr)
            if dec is not None:
                value, updated_at = dec
                self.state[asset] = ChainlinkState(value=value, source_ts=float(updated_at), recv_ts=now)
                got += 1
        self.poll_count += 1
        self.connection_status = "ok" if got == len(AGGREGATORS) else (
            "partial" if got else "no_data"
        )
        if self.poll_count == 1:
            log.info(
                "CHAINLINK on-chain baglandi (Polygon aggregator): %s",
                {a: round(s.value, 4) for a, s in self.state.items()},
            )

    async def run(self, stop: asyncio.Event) -> None:
        if not self.settings.chainlink_enabled or not self._rpcs:
            log.info("ChainlinkFeed devre disi (chainlink_enabled/polygon_rpc yok)")
            return
        while not stop.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("Chainlink poll hatasi: %s", exc)
                self.connection_status = "error"
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.settings.chainlink_poll_sec)
            except asyncio.TimeoutError:
                pass
        log.info("ChainlinkFeed durduruldu")

    def get_state(self, asset: str) -> Optional[ChainlinkState]:
        return self.state.get(asset)

    def status(self) -> dict:
        now = time.time()
        return {
            "connection": self.connection_status,
            "polls": self.poll_count,
            "feeds": {
                a: {"value": round(s.value, 6), "source_age_ms": round(s.source_age_ms(now), 0)}
                for a, s in self.state.items()
            },
        }
