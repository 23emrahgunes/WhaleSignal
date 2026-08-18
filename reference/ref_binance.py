"""1h ufku icin **Binance-candle-oriented** PTB adaptoru.

1h up/down marketleri Binance mum (candle) baziyla resolve olur: kapanis fiyati,
saatlik mumun ACILIS fiyatiyla kiyaslanir. Dolayisiyla PTB = pencereyi kapsayan
Binance mumunun **open** fiyati (deterministik). REST klines'tan cekilir,
condition_id ile onbelleklenir.
"""
from __future__ import annotations

import logging
from typing import Optional

from .base import ReferencePrice

log = logging.getLogger("direction_engine.ref.binance")

# horizon.value -> Binance kline interval
_INTERVAL = {"5m": "5m", "15m": "15m", "1h": "1h"}


def pick_candle_open(rows: list, window_start_ms: int) -> Optional[float]:
    """Klines satirlarindan openTime == window_start olan mumun open fiyati.

    rows: Binance /klines ciktisi [[openTime, open, high, low, close, ...], ...].
    Tam eslesme yoksa window_start'i iceren mum (openTime <= ws < closeTime).
    """
    best: Optional[float] = None
    for r in rows:
        try:
            open_time = int(r[0])
            open_px = float(r[1])
            close_time = int(r[6]) if len(r) > 6 else open_time + 1
        except (IndexError, TypeError, ValueError):
            continue
        if open_time == window_start_ms:
            return open_px
        if open_time <= window_start_ms < close_time:
            best = open_px
    return best


class BinanceCandleReference:
    """PTB = saatlik Binance mumunun open fiyati (settlement-uyumlu)."""

    name = "binance_candle"

    def __init__(self, settings) -> None:  # noqa: ANN001
        self.settings = settings
        self._cache: dict[str, ReferencePrice] = {}  # condition_id -> PTB

    async def reference_for(self, ref, feed, session) -> ReferencePrice:  # noqa: ANN001
        cid = ref.condition_id or ref.slug
        cached = self._cache.get(cid)
        if cached is not None and cached.ok:
            return cached
        interval = _INTERVAL.get(ref.combo.horizon.value, "1h")
        window_start_ms = int(ref.start_ts) * 1000 - (
            int(ref.start_ts) * 1000 % (ref.combo.horizon.seconds * 1000)
        )
        # ref.start_ts zaten pencere basi; dogrudan kullan
        window_start_ms = int(ref.start_ts * 1000)
        url = f"{self.settings.binance_rest_base}/api/v3/klines"
        params = {
            "symbol": ref.combo.binance_symbol,
            "interval": interval,
            "startTime": str(window_start_ms - ref.combo.horizon.seconds * 1000),
            "limit": "3",
        }
        try:
            async with session.get(url, params=params, timeout=12) as resp:
                if resp.status != 200:
                    return ReferencePrice(None, "", 0.0)
                rows = await resp.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("%s kline PTB alinamadi: %s", ref.combo.key, exc)
            return ReferencePrice(None, "", 0.0)
        if not isinstance(rows, list):
            return ReferencePrice(None, "", 0.0)
        open_px = pick_candle_open(rows, window_start_ms)
        if open_px is None:
            return ReferencePrice(None, "", 0.0)
        rp = ReferencePrice(open_px, "binance_candle_open", ref.start_ts)
        self._cache[cid] = rp
        return rp
