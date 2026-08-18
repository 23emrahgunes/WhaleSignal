"""Horizon-bazli reference (PTB) adaptorleri.

5m/15m -> Chainlink-oriented; 1h -> Binance-candle-oriented. `ReferenceRouter`
combo'ya gore dogru adaptore yonlendirir.
"""
from __future__ import annotations

import time

from models import Horizon, ResolutionType

from .base import Reference, ReferencePrice
from .ref_binance import BinanceCandleReference
from .ref_chainlink import ChainlinkReference


class ReferenceRouter:
    """PTB adaptorunu marketin **resolution_type**'ina gore secer (generic openPrice DEGIL).

    Resolution bilinmiyorsa horizon'a duser (5m/15m->Chainlink, 1h->Binance candle).
    """

    def __init__(self, settings) -> None:  # noqa: ANN001
        self.chainlink = ChainlinkReference(settings)
        self.binance = BinanceCandleReference(settings)

    def adapter_for(self, horizon: Horizon, resolution_type: ResolutionType = None) -> Reference:  # type: ignore[assignment]
        # once resolution_type; yoksa horizon
        if resolution_type in (ResolutionType.CHAINLINK, ResolutionType.CHAINLINK_TWAP):
            return self.chainlink
        if resolution_type == ResolutionType.BINANCE_CANDLE:
            return self.binance
        if horizon == Horizon.H1H:
            return self.binance
        return self.chainlink

    async def reference_for(self, ref, feed, session) -> ReferencePrice:  # noqa: ANN001
        adapter = self.adapter_for(ref.combo.horizon, ref.resolution_type)
        rp = await adapter.reference_for(ref, feed, session)
        # market bazinda reference_open/current/updated_at yaz
        if rp.ok:
            if ref.reference_open is None:
                ref.reference_open = rp.price  # pencere aciliminda sabitle
            ref.reference_current = rp.price
            ref.reference_updated_at = time.time()
            # Chainlink TWAP: settlement penceresi + gozlem ani sakla
            if ref.resolution_type == ResolutionType.CHAINLINK_TWAP:
                ref.twap_window_sec = ref.combo.horizon.seconds
                ref.twap_observation_ts = ref.market_end_ts
        return rp


__all__ = [
    "Reference",
    "ReferencePrice",
    "ReferenceRouter",
    "ChainlinkReference",
    "BinanceCandleReference",
]
