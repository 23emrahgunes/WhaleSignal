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

    def __init__(self, settings, chainlink=None) -> None:  # noqa: ANN001
        self.settings = settings
        self.chainlink = chainlink  # ChainlinkFeed (5m/15m official + current)
        self._official: dict[str, tuple[float, float, str]] = {}  # mid -> (open, open_time, src)
        self._proxy: dict[str, tuple[float, float, str]] = {}
        self._capture_logged: set[str] = set()

    def attach_chainlink(self, chainlink) -> None:  # noqa: ANN001
        self.chainlink = chainlink

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

    async def reference_for(self, ref, feed, session) -> ReferencePrice:  # noqa: ANN001
        now = time.time()
        ref.reference_symbol = ref.combo.binance_symbol
        # canli reference_current: 5m/15m -> Chainlink current; 1h -> Binance spot
        if ref.combo.horizon == Horizon.H1H:
            if feed is not None:
                spot, age = feed.spot_price()
                if spot is not None and spot > 0:
                    ref.reference_current = spot
                    ref.reference_current_time = now
                    ref.reference_current_age_ms = age
                    ref.reference_current_source_ts = now - (age or 0) / 1000.0
        else:
            cl = self.chainlink.get_state(ref.combo.asset.value) if self.chainlink else None
            if cl is not None:
                ref.reference_current = cl.value
                ref.reference_current_time = now
                ref.reference_current_source_ts = cl.source_ts
                ref.reference_current_age_ms = cl.age_ms(now)
            elif feed is not None:
                # Chainlink yoksa dashboard current bos kalmasin diye Binance spot (analytics)
                spot, age = feed.spot_price()
                if spot is not None:
                    ref.reference_current = spot
                    ref.reference_current_age_ms = age

        if ref.combo.horizon == Horizon.H1H:
            await self._acquire_1h_official(ref, session, now)
        else:
            self._acquire_5m15m_official(ref, now)
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

    # ---- 5m/15m: official = CHAINLINK path (metadata -> RTDS capture); Binance DEGIL ----
    def _acquire_5m15m_official(self, ref, now) -> None:  # noqa: ANN001
        """Precedence: (1) metadata (discovery'de set), (2) CHAINLINK_RTDS_CAPTURE (acilista
        canli RTDS), (3) yoksa OPEN_REFERENCE_UNAVAILABLE. market_id anahtarli cache."""
        mid = ref.market_id
        # PRIORITY 1: metadata (discovery)
        if ref.official_reference_open is not None:
            self._log_capture(ref, "OFFICIAL_OK")
            return
        cached = self._official.get(mid)
        if cached:
            ref.official_reference_open, ref.official_reference_open_time, ref.official_reference_source = cached
            return
        # PRIORITY 2: CHAINLINK_RTDS_CAPTURE — yalniz market ACILISINDA (open capture window)
        in_open = ref.market_age_sec(now) <= self.settings.open_capture_window_sec
        cl = self.chainlink.get_state(ref.combo.asset.value) if self.chainlink else None
        if in_open and cl is not None:
            fresh = cl.age_ms(now) <= self.settings.max_reference_source_age_ms
            if fresh and cl.value > 0:
                twap = ref.resolution_type == ResolutionType.CHAINLINK_TWAP
                src = "CHAINLINK_TWAP_ONCHAIN" if twap else "CHAINLINK_ONCHAIN_CAPTURE"
                ref.official_reference_open = cl.value
                ref.official_reference_open_time = ref.market_start_ts
                ref.official_reference_source = src
                ref.official_reference_capture_time = now
                self._official[mid] = (cl.value, ref.market_start_ts, src)
                self._log_capture(ref, "OFFICIAL_OK")
                return
        # official yok -> PTB_MISSING (proxy ASLA official yerine gecmez)
        reason = "OPEN_REFERENCE_UNAVAILABLE" if not in_open else (
            "NO_CHAINLINK_STATE" if cl is None else "SOURCE_STALE"
        )
        self._log_capture(ref, "PTB_MISSING", reason)

    def _log_capture(self, ref, status: str, reason: str = "") -> None:  # noqa: ANN001
        if ref.market_id in self._capture_logged and status == "OFFICIAL_OK":
            return
        self._capture_logged.add(ref.market_id)
        gap = None
        if ref.official_reference_open and ref.proxy_reference_open:
            try:
                import math as _m
                gap = round(10000.0 * _m.log(ref.proxy_reference_open / ref.official_reference_open), 2)
            except Exception:  # noqa: BLE001
                gap = None
        log.info(
            "REFERENCE_OPEN_CAPTURE combo=%s market_id=%s resolution=%s official_open=%s "
            "official_source=%s proxy_open=%s proxy_gap_bps=%s STATUS=%s%s",
            ref.combo.key, (ref.market_id or "")[-8:], ref.resolution_type.value,
            ref.official_reference_open, ref.official_reference_source,
            ref.proxy_reference_open, gap, status,
            (f" reason={reason}" if reason else ""),
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
