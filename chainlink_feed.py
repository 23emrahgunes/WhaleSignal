"""Chainlink official reference collector — Polymarket RTDS.

5m/15m up/down marketleri **Chainlink** referansiyla resolve olur. Bu modul Polymarket'in
**RTDS** (real-time data service) canli fiyat akisina baglanir ve BTC/ETH/SOL/XRP USD icin
canli Chainlink state tutar. Marketin OPENING referansi, yeni market rotate olurken bu
canli degerden yakalanir (reference/__init__ `CHAINLINK_RTDS_CAPTURE`).

⚠️ RTDS wire-format buradan (geoblock) dogrulanamadi. Bu yuzden:
  - Endpoint + subscribe mesaji **configurable** (`RTDS_WS_URL`, `RTDS_SUBSCRIBE_JSON`).
  - `RTDS_DEBUG_RAW=true` iken ilk ham mesajlar loglanir -> AWS'te gercek format gorulup
    parser/subscribe ayarlanabilir.
  - Parser cok-sekil savunmali (symbol/price/timestamp anahtarlari esnek).

Placeholder DEGIL: gercek WS baglantisi + state; yalniz mesaj sema detayi AWS'te netlesir.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp

from config import Settings
from wsbase import ReconnectingWSClient

log = logging.getLogger("direction_engine.chainlink")

# RTDS sembol -> ic asset. Cok-sekil (BTC, BTC/USD, BTCUSD, bitcoin...).
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


@dataclass
class ChainlinkState:
    value: float
    source_ts: float  # kaynak (feed) zaman damgasi (sn); yoksa recv_ts
    recv_ts: float  # yerel varis (sn)

    def age_ms(self, now: Optional[float] = None) -> float:
        now = time.time() if now is None else now
        return max(0.0, (now - self.recv_ts) * 1000.0)


def parse_price_message(msg: dict) -> Optional[tuple[str, float, float]]:
    """RTDS mesajindan (asset, value, source_ts) cikar. Cok-sekil savunmali."""
    if not isinstance(msg, dict):
        return None
    # symbol
    sym = None
    for k in ("symbol", "asset", "pair", "feed", "ticker", "market", "instrument"):
        if k in msg:
            sym = map_symbol(msg.get(k))
            if sym:
                break
    if sym is None:
        return None
    # value
    val = None
    for k in ("value", "price", "p", "px", "answer", "last", "close"):
        v = msg.get(k)
        if v is not None:
            try:
                val = float(v)
                break
            except (TypeError, ValueError):
                continue
    if val is None or val <= 0:
        return None
    # source timestamp (ms veya sn)
    src_ts = time.time()
    for k in ("timestamp", "ts", "time", "source_ts", "updatedAt", "T"):
        v = msg.get(k)
        if v is None:
            continue
        try:
            fv = float(v)
            src_ts = fv / 1000.0 if fv > 1e12 else fv
            break
        except (TypeError, ValueError):
            continue
    return sym, val, src_ts


class ChainlinkFeed(ReconnectingWSClient):
    """RTDS WS -> per-asset canli Chainlink state (BTC/ETH/SOL/XRP)."""

    def __init__(self, settings: Settings, session: aiohttp.ClientSession) -> None:
        super().__init__(
            settings.rtds_ws_url,
            "ChainlinkRTDS",
            session,
            backoff_base=settings.backoff_base_sec,
            backoff_factor=settings.backoff_factor,
            backoff_cap=settings.backoff_cap_sec,
            recv_timeout=settings.ws_recv_timeout_sec,
        )
        self.settings = settings
        self.state: dict[str, ChainlinkState] = {}
        self._raw_logged = 0

    async def _subscribe_payload(self) -> Optional[str]:
        # configurable subscribe; default best-effort (AWS'te gercek formata ayarlanir)
        custom = getattr(self.settings, "rtds_subscribe_json", "") or ""
        if custom.strip():
            return custom
        return json.dumps({
            "action": "subscribe",
            "subscriptions": [
                {"topic": "crypto_prices", "symbols": ["BTC", "ETH", "SOL", "XRP"]}
            ],
        })

    async def _handle(self, raw: str) -> None:
        if self.settings.rtds_debug_raw and self._raw_logged < 8:
            self._raw_logged += 1
            log.info("RTDS RAW[%d]: %s", self._raw_logged, raw[:400])
        data = json.loads(raw)
        msgs = data if isinstance(data, list) else [data]
        now = time.time()
        for m in msgs:
            # bazen fiyat 'data'/'payload' altinda gelir
            candidate = m
            if isinstance(m, dict) and not any(
                k in m for k in ("price", "value", "p", "answer")
            ):
                inner = m.get("data") or m.get("payload") or m.get("message")
                if isinstance(inner, dict):
                    candidate = inner
            parsed = parse_price_message(candidate)
            if parsed is None:
                continue
            sym, val, src_ts = parsed
            self.state[sym] = ChainlinkState(value=val, source_ts=src_ts, recv_ts=now)

    def get_state(self, asset: str) -> Optional[ChainlinkState]:
        return self.state.get(asset)

    def status(self) -> dict:
        now = time.time()
        return {
            a: {
                "value": s.value,
                "source_ts": s.source_ts,
                "age_ms": round(s.age_ms(now), 0),
            }
            for a, s in self.state.items()
        }
