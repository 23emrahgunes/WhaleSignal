"""5m/15m ufku icin **Chainlink-oriented** PTB adaptoru.

5m/15m up/down marketleri Chainlink referansiyla resolve olur. Chainlink fiyati
zincir-uzeri oldugundan basit bir public REST'i yoktur; bu adaptor iki yol dener:

  1. `POLY_PRICE_HOST` altinda bir crypto-price ucu yapilandirilmis/erisilebilirse
     ondan Chainlink-uyumlu referansi cekmeye calis (runtime'da netlesir).
  2. Fallback: pencere aciliminda (ilk saniyeler) canli spot'u yakala ve PTB proxy
     olarak sabitle (`source="spot_proxy"`). Servis pencereye GEC katildiysa (open'i
     kacirdiysa) guvenilir PTB uretemez -> price=None (recorder/quality bunu isaretler).

PTB market omru boyunca sabittir; condition_id ile onbelleklenir.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from .base import ReferencePrice

log = logging.getLogger("direction_engine.ref.chainlink")

# Pencerenin ilk bu kadar saniyesinde spot'u "acilis PTB proxy" say.
_OPEN_CAPTURE_WINDOW_SEC = 20.0


class ChainlinkReference:
    """PTB = Chainlink-oriented referans (ucu varsa) veya acilis-spot proxy."""

    name = "chainlink"

    def __init__(self, settings) -> None:  # noqa: ANN001
        self.settings = settings
        self._cache: dict[str, ReferencePrice] = {}  # condition_id -> PTB

    async def _try_poly_price(self, ref, session) -> Optional[float]:  # noqa: ANN001
        """POLY_PRICE_HOST altinda Chainlink-uyumlu fiyat ucu (best-effort).

        Uc runtime'da dogrulanana kadar sessizce None doner; servis fallback'e gecer.
        """
        if not self.settings.chainlink_ref_enabled:
            return None
        # NOT: kesin uc runtime'da (AWS'ten Polymarket'e erisimle) netlesecek.
        # Su an yapilandirilmis bir uc yoksa None -> fallback spot_proxy.
        return None

    async def reference_for(self, ref, feed, session) -> ReferencePrice:  # noqa: ANN001
        cid = ref.condition_id or ref.slug
        cached = self._cache.get(cid)
        if cached is not None and cached.ok:
            return cached

        # 1) Chainlink-uyumlu uc (varsa)
        px = await self._try_poly_price(ref, session)
        if px is not None and px > 0:
            rp = ReferencePrice(px, "chainlink", ref.start_ts)
            self._cache[cid] = rp
            return rp

        # 2) fallback: acilista canli spot'u yakala (proxy)
        now = time.time()
        in_open_window = ref.remaining_sec(now) >= (ref.duration_sec - _OPEN_CAPTURE_WINDOW_SEC)
        if feed is not None and in_open_window:
            spot, _age = feed.spot_price()
            if spot is not None and spot > 0:
                rp = ReferencePrice(spot, "spot_proxy", now)
                self._cache[cid] = rp
                log.info("%s PTB spot_proxy=%s (acilista yakalandi)", ref.combo.key, spot)
                return rp

        # pencereye gec katildik: guvenilir PTB yok
        return ReferencePrice(None, "", 0.0)
