"""Reference/PTB — OFFICIAL (resolution kaynagi) vs PROXY (analytics) AYRI.

TEMEL KURAL: proxy asla official PTB yerine gecmez.
  - **5m/15m official = CHAINLINK / CHAINLINK_TWAP** — market metadata/rules'tan
    (discovery `extract_official_reference`) veya Poly Chainlink-price ucu (runtime hook).
    Binance kline-open yalnizca `proxy_reference_open` (analytics/feature).
    Official yoksa -> PTB_MISSING.
  - **1h official = BINANCE_1H_CANDLE** — canonical market_start'a TAM HIZALI Binance saatlik
    mum OPEN'i (`openTime == market_start`). Hizalanmazsa REFERENCE_TIME_MISMATCH.
  - `reference_current` = canli direct Binance spot (her iki horizon).
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from models import Horizon, ResolutionType

from .base import Reference, ReferencePrice
from .ref_binance import find_candle_open

log = logging.getLogger("direction_engine.reference")

_INTERVAL = {"5m": "5m", "15m": "15m", "1h": "1h"}
_ALIGN_TOL_MS = 1500  # 1h candle openTime == market_start toleransi


class ReferenceRouter:
    """Official + proxy referansi market'e (market_id) gore doldurur/onbellekler."""

    def __init__(self, settings) -> None:  # noqa: ANN001
        self.settings = settings
        self._official: dict[str, tuple[float, float, str]] = {}  # mid -> (open, open_time, src)
        self._proxy: dict[str, tuple[float, float, str]] = {}

    async def _candle_open(
        self, session, symbol: str, interval: str, window_start_ms: int, horizon_sec: int
    ):  # noqa: ANN001
        url = f"{self.settings.binance_rest_base}/api/v3/klines"
        params = {
            "symbol": symbol, "interval": interval,
            "startTime": str(window_start_ms - horizon_sec * 1000), "limit": "3",
        }
        try:
            async with session.get(url, params=params, timeout=12) as resp:
                if resp.status != 200:
                    return None, None
                rows = await resp.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("%s candle-open alinamadi: %s", symbol, exc)
            return None, None
        if not isinstance(rows, list):
            return None, None
        return find_candle_open(rows, window_start_ms)

    async def _try_poly_price(self, ref, session) -> Optional[float]:  # noqa: ANN001
        """5m/15m Chainlink-price ucu (runtime hook). Su an yapilandirilmis uc yoksa None."""
        if not self.settings.chainlink_ref_enabled:
            return None
        return None  # AWS'te gercek Chainlink/Poly-price ucu netlesince doldurulacak

    async def reference_for(self, ref, feed, session) -> ReferencePrice:  # noqa: ANN001
        now = time.time()
        mid = ref.market_id
        ref.reference_symbol = ref.combo.binance_symbol
        # canli reference_current = direct Binance spot
        if feed is not None:
            spot, _age = feed.spot_price()
            if spot is not None and spot > 0:
                ref.reference_current = spot
                ref.reference_current_time = now

        if ref.combo.horizon == Horizon.H1H:
            await self._acquire_1h_official(ref, session, now)
        else:
            await self._acquire_5m15m_official(ref, session, now)
            await self._acquire_proxy(ref, session)

        ok = ref.official_reference_open is not None
        return ReferencePrice(ref.official_reference_open, ref.official_reference_source or "", now if ok else 0.0)

    # ---- 1h: official = Binance saatlik mum open (hizali) ----
    async def _acquire_1h_official(self, ref, session, now) -> None:  # noqa: ANN001
        mid = ref.market_id
        if ref.official_reference_open is not None:
            return
        cached = self._official.get(mid)
        if cached:
            ref.official_reference_open, ref.official_reference_open_time, ref.official_reference_source = cached
            return
        if ref.market_start_ts is None:
            return
        ws_ms = int(ref.market_start_ts * 1000)
        symbol = ref.resolution_symbol or ref.combo.binance_symbol
        open_px, open_time = await self._candle_open(session, symbol, "1h", ws_ms, 3600)
        if open_px is None or open_time is None:
            log.info("%s 1h REFERENCE debug: STATUS=WAITING reason=NO_OFFICIAL_REFERENCE", ref.combo.key)
            return
        # ALIGNMENT invariant: candle openTime == canonical market_start
        if abs(open_time - ws_ms) > _ALIGN_TOL_MS:
            log.warning(
                "%s 1h REFERENCE_TIME_MISMATCH open_time=%d market_start=%d -> PTB_MISSING",
                ref.combo.key, open_time, ws_ms,
            )
            return
        ref.official_reference_open = open_px
        ref.official_reference_open_time = open_time / 1000.0
        ref.official_reference_source = ResolutionType.BINANCE_1H_CANDLE.value
        self._official[mid] = (open_px, ref.official_reference_open_time, ref.official_reference_source)
        log.info(
            "%s 1h REFERENCE debug: open=%.4f open_time=%d symbol=%s STATUS=OFFICIAL",
            ref.combo.key, open_px, open_time, symbol,
        )

    # ---- 5m/15m: official = Chainlink (metadata/poly hook); Binance BURADA DEGIL ----
    async def _acquire_5m15m_official(self, ref, session, now) -> None:  # noqa: ANN001
        if ref.official_reference_open is not None:
            return  # discovery metadata'dan geldi (CHAINLINK...)
        px = await self._try_poly_price(ref, session)
        if px is not None and px > 0:
            ref.official_reference_open = px
            ref.official_reference_open_time = ref.market_start_ts
            src = "CHAINLINK_TWAP" if ref.resolution_type == ResolutionType.CHAINLINK_TWAP else "CHAINLINK"
            ref.official_reference_source = f"{src}:poly_price"
            return
        # official yok -> PTB_MISSING (proxy asla official yerine gecmez)
        log.info(
            "%s REFERENCE debug: STATUS=WAITING reason=NO_OFFICIAL_REFERENCE (resolution=%s)",
            ref.combo.key, ref.resolution_type.value,
        )

    # ---- proxy (5m/15m): Binance kline-open, YALNIZ analytics ----
    async def _acquire_proxy(self, ref, session) -> None:  # noqa: ANN001
        mid = ref.market_id
        cached = self._proxy.get(mid)
        if cached:
            ref.proxy_reference_open, ref.proxy_reference_open_time, ref.proxy_reference_source = cached
            return
        if ref.market_start_ts is None:
            return
        ws_ms = int(ref.market_start_ts * 1000)
        interval = _INTERVAL.get(ref.combo.horizon.value, "5m")
        open_px, open_time = await self._candle_open(
            session, ref.combo.binance_symbol, interval, ws_ms, ref.combo.horizon.seconds
        )
        if open_px is None:
            return
        ref.proxy_reference_open = open_px
        ref.proxy_reference_open_time = (open_time / 1000.0) if open_time else None
        ref.proxy_reference_source = "BINANCE"
        self._proxy[mid] = (open_px, ref.proxy_reference_open_time or 0.0, "BINANCE")


__all__ = ["Reference", "ReferencePrice", "ReferenceRouter", "find_candle_open"]
