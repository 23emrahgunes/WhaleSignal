"""Reference (PTB) adaptor arayuzu.

PTB = "price to beat" = marketin resolve edildigi referans fiyat (pencere aciliminda
sabitlenir). Farkli ufuklar farkli settlement kaynagina resolve oldugu icin ADAPTOR
horizon-bazlidir:
  - 5m/15m -> Chainlink-oriented (`ChainlinkReference`)
  - 1h    -> Binance-candle-oriented (`BinanceCandleReference`)

Ortak sozlesme: `reference_for(ref, feed, session) -> ReferencePrice`. PTB market
omru boyunca sabittir; adaptorler condition_id ile onbellekler.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass
class ReferencePrice:
    """Bir marketin PTB referansi + kaynak/etiket + tazelik."""

    price: Optional[float]
    source: str  # "chainlink" | "binance_candle_open" | "spot_proxy" | ""
    ts: float

    @property
    def age_ms(self) -> Optional[float]:
        if self.price is None or self.ts <= 0:
            return None
        return max(0.0, time.time() * 1000 - self.ts * 1000)

    @property
    def ok(self) -> bool:
        return self.price is not None and self.price > 0


class Reference(Protocol):
    """Horizon-bazli PTB adaptor sozlesmesi."""

    name: str

    async def reference_for(self, ref, feed, session) -> ReferencePrice:  # noqa: ANN001
        ...
