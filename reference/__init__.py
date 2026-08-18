"""Horizon-bazli reference (PTB) adaptorleri.

5m/15m -> Chainlink-oriented; 1h -> Binance-candle-oriented. `ReferenceRouter`
combo'ya gore dogru adaptore yonlendirir.
"""
from __future__ import annotations

from models import Horizon

from .base import Reference, ReferencePrice
from .ref_binance import BinanceCandleReference
from .ref_chainlink import ChainlinkReference


class ReferenceRouter:
    """Combo horizon'una gore PTB adaptorunu secer (settlement-uyumlu)."""

    def __init__(self, settings) -> None:  # noqa: ANN001
        self.chainlink = ChainlinkReference(settings)
        self.binance = BinanceCandleReference(settings)

    def adapter_for(self, horizon: Horizon) -> Reference:
        # 5m/15m -> Chainlink; 1h -> Binance candle
        if horizon == Horizon.H1H:
            return self.binance
        return self.chainlink

    async def reference_for(self, ref, feed, session) -> ReferencePrice:  # noqa: ANN001
        return await self.adapter_for(ref.combo.horizon).reference_for(ref, feed, session)


__all__ = [
    "Reference",
    "ReferencePrice",
    "ReferenceRouter",
    "ChainlinkReference",
    "BinanceCandleReference",
]
