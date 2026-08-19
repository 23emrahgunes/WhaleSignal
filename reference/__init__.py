"""Reference/PTB routing — OFFICIAL resolution source vs analytics proxy.

Rules:
- 5m/15m official opening reference comes from the same Chainlink Data Stream
  lineage named by the Polymarket market rules.  Polymarket RTDS provides that
  public Chainlink stream.  A short source-timestamped RTDS history is used to
  select the observation nearest the canonical market start.
- Binance kline open for 5m/15m is analytics-only proxy and is never promoted.
- 1h official reference is the Binance 1-hour candle open aligned exactly with
  the canonical ET-slot market start.
"""
from __future__ import annotations

import logging
import math
import time

from models import Horizon, ResolutionType

from .base import Reference, ReferencePrice
from .ref_binance import find_candle_open

log = logging.getLogger("direction_engine.reference")

_INTERVAL = {"5m": "5m", "15m": "15m", "1h": "1h"}
_ALIGN_TOL_MS = 1500
_DEFAULT_CHAINLINK_OPEN_ALIGN_MS = 5000.0


class ReferenceRouter:
    """Populate/cache official and proxy references per market_id."""

    def __init__(self, settings, chainlink=None) -> None:  # noqa: ANN001
        self.settings = settings
        self.chainlink = chainlink
        self._official: dict[str, tuple[float, float, str]] = {}
        self._proxy: dict[str, tuple[float, float, str]] = {}
        self._capture_logged: set[str] = set()

    def attach_chainlink(self, chainlink) -> None:  # noqa: ANN001
        self.chainlink = chainlink

    async def _candle_open(
        self, session, symbol: str, interval: str, window_start_ms: int, horizon_sec: int
    ):  # noqa: ANN001
        url = f"{self.settings.binance_rest_base}/api/v3/klines"
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": str(window_start_ms - horizon_sec * 1000),
            "limit": "3",
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

        # Current reference must follow the official source lineage when available.
        if ref.combo.horizon == Horizon.H1H:
            if feed is not None:
                spot, age = feed.spot_price()
                if spot is not None and spot > 0:
                    ref.reference_current = spot
                    ref.reference_current_time = now
                    ref.reference_current_age_ms = age
                    ref.reference_current_source_ts = now - (age or 0.0) / 1000.0
        else:
            cl = self.chainlink.get_state(ref.combo.asset.value) if self.chainlink else None
            if cl is not None:
                ref.reference_current = cl.value
                ref.reference_current_time = cl.source_ts
                ref.reference_current_source_ts = cl.source_ts
                # Source age, not local poll/receive age.
                ref.reference_current_age_ms = cl.source_age_ms(now)
            elif feed is not None:
                # Dashboard-only analytics fallback. This does NOT satisfy REFERENCE quality.
                spot, age = feed.spot_price()
                if spot is not None:
                    ref.reference_current = spot
                    ref.reference_current_time = now
                    ref.reference_current_age_ms = age

        if ref.combo.horizon == Horizon.H1H:
            await self._acquire_1h_official(ref, session)
        else:
            self._acquire_5m15m_official(ref, now)
            await self._acquire_proxy(ref, session)

        ok = ref.official_reference_open is not None
        return ReferencePrice(
            ref.official_reference_open,
            ref.official_reference_source or "",
            ref.official_reference_open_time if ok else 0.0,
        )

    # ------------------------------------------------------------------
    # 1h: official = Binance hourly candle open
    # ------------------------------------------------------------------
    async def _acquire_1h_official(self, ref, session) -> None:  # noqa: ANN001
        mid = ref.market_id
        if ref.official_reference_open is not None:
            return
        cached = self._official.get(mid)
        if cached:
            (
                ref.official_reference_open,
                ref.official_reference_open_time,
                ref.official_reference_source,
            ) = cached
            return
        if ref.market_start_ts is None:
            return
        ws_ms = int(ref.market_start_ts * 1000)
        symbol = ref.resolution_symbol or ref.combo.binance_symbol
        open_px, open_time = await self._candle_open(session, symbol, "1h", ws_ms, 3600)
        if open_px is None or open_time is None:
            log.info("%s 1h REFERENCE: WAITING NO_OFFICIAL_REFERENCE", ref.combo.key)
            return
        if abs(open_time - ws_ms) > _ALIGN_TOL_MS:
            log.warning(
                "%s 1h REFERENCE_TIME_MISMATCH open_time=%d market_start=%d",
                ref.combo.key,
                open_time,
                ws_ms,
            )
            return
        ref.official_reference_open = open_px
        ref.official_reference_open_time = open_time / 1000.0
        ref.official_reference_source = ResolutionType.BINANCE_1H_CANDLE.value
        self._official[mid] = (
            open_px,
            ref.official_reference_open_time,
            ref.official_reference_source,
        )
        log.info(
            "%s 1h REFERENCE official open=%.8f source=BINANCE_1H_CANDLE",
            ref.combo.key,
            open_px,
        )

    # ------------------------------------------------------------------
    # 5m/15m: official = Polymarket RTDS Chainlink Data Stream
    # ------------------------------------------------------------------
    @staticmethod
    def _rules_name_data_stream(ref) -> bool:  # noqa: ANN001
        source = (ref.resolution_source or "").lower()
        return (
            "data.chain.link/streams" in source
            or "data stream" in source
            or ref.resolution_type == ResolutionType.CHAINLINK
        )

    def _acquire_5m15m_official(self, ref, now: float) -> None:  # noqa: ANN001
        mid = ref.market_id

        # Priority 1: explicit authoritative metadata captured by discovery.
        if ref.official_reference_open is not None:
            self._log_capture(ref, "OFFICIAL_OK")
            return

        cached = self._official.get(mid)
        if cached:
            (
                ref.official_reference_open,
                ref.official_reference_open_time,
                ref.official_reference_source,
            ) = cached
            return

        # Do not silently call a generic Chainlink tick a TWAP.  Current Up/Down
        # rules name Chainlink Data Streams; if a genuinely different TWAP rule is
        # ever encountered, fail closed until a matching official adapter exists.
        if ref.resolution_type == ResolutionType.CHAINLINK_TWAP and not self._rules_name_data_stream(ref):
            self._log_capture(ref, "PTB_MISSING", "UNSUPPORTED_TWAP_REFERENCE")
            return

        if self.chainlink is None or ref.market_start_ts is None:
            self._log_capture(ref, "PTB_MISSING", "NO_CHAINLINK_DATA_STREAM")
            return

        max_align_ms = float(
            getattr(self.settings, "max_reference_open_alignment_ms", _DEFAULT_CHAINLINK_OPEN_ALIGN_MS)
        )
        opening = self.chainlink.opening_state(
            ref.combo.asset.value,
            ref.market_start_ts,
            max_alignment_ms=max_align_ms,
        )
        if opening is None:
            # This is expected after a mid-window restart if no boundary tick exists in history.
            self._log_capture(ref, "PTB_MISSING", "OPEN_REFERENCE_UNAVAILABLE")
            return

        ref.official_reference_open = opening.value
        ref.official_reference_open_time = opening.source_ts
        ref.official_reference_source = "CHAINLINK_DATA_STREAM_RTDS"
        ref.official_reference_capture_time = opening.recv_ts
        self._official[mid] = (
            opening.value,
            opening.source_ts,
            ref.official_reference_source,
        )
        self._log_capture(ref, "OFFICIAL_OK")

    def _log_capture(self, ref, status: str, reason: str = "") -> None:  # noqa: ANN001
        key = f"{ref.market_id}:{status}:{reason}"
        if key in self._capture_logged:
            return
        self._capture_logged.add(key)
        gap = None
        if ref.official_reference_open and ref.proxy_reference_open:
            try:
                gap = round(
                    10000.0
                    * math.log(ref.proxy_reference_open / ref.official_reference_open),
                    3,
                )
            except Exception:  # noqa: BLE001
                gap = None
        alignment_ms = None
        if ref.official_reference_open_time is not None and ref.market_start_ts is not None:
            alignment_ms = round(
                (ref.official_reference_open_time - ref.market_start_ts) * 1000.0,
                1,
            )
        log.info(
            "REFERENCE_OPEN_CAPTURE combo=%s market_id=%s resolution=%s official_open=%s "
            "official_time=%s align_ms=%s official_source=%s proxy_open=%s proxy_gap_bps=%s "
            "STATUS=%s%s",
            ref.combo.key,
            (ref.market_id or "")[-8:],
            ref.resolution_type.value,
            ref.official_reference_open,
            ref.official_reference_open_time,
            alignment_ms,
            ref.official_reference_source,
            ref.proxy_reference_open,
            gap,
            status,
            f" reason={reason}" if reason else "",
        )

    # ------------------------------------------------------------------
    # 5m/15m analytics proxy = Binance kline open
    # ------------------------------------------------------------------
    async def _acquire_proxy(self, ref, session) -> None:  # noqa: ANN001
        mid = ref.market_id
        cached = self._proxy.get(mid)
        if cached:
            (
                ref.proxy_reference_open,
                ref.proxy_reference_open_time,
                ref.proxy_reference_source,
            ) = cached
            return
        if ref.market_start_ts is None:
            return
        ws_ms = int(ref.market_start_ts * 1000)
        interval = _INTERVAL.get(ref.combo.horizon.value, "5m")
        open_px, open_time = await self._candle_open(
            session,
            ref.combo.binance_symbol,
            interval,
            ws_ms,
            ref.combo.horizon.seconds,
        )
        if open_px is None:
            return
        ref.proxy_reference_open = open_px
        ref.proxy_reference_open_time = (open_time / 1000.0) if open_time else None
        ref.proxy_reference_source = "BINANCE"
        self._proxy[mid] = (
            open_px,
            ref.proxy_reference_open_time or 0.0,
            "BINANCE",
        )


__all__ = ["Reference", "ReferencePrice", "ReferenceRouter", "find_candle_open"]
