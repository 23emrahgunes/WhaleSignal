"""Fresh-economic LIVE gateway for P3 structural arbitrage.

The STRICT scanner proves that a structural opportunity existed and survived its
confirmation window.  LIVE execution must then price the *current* pair as a whole;
it must not reject a still-profitable pair merely because one leg moved above that
leg's historical scanner limit while the other leg improved by more.

This gateway therefore consumes the current visible ask depth without the historical
per-leg price cap.  The executor still applies all fresh pair-level net-profit/ROI,
single-leg notional, unwind-depth/loss, collateral and FOK controls before any order
is submitted.
"""
from __future__ import annotations

from typing import Any

from p3_live_gateway_v2 import RiskAwarePolymarketLiveGateway
from p3_live_sizing import DepthQuote, consume_depth


class FreshEconomicPolymarketLiveGateway(RiskAwarePolymarketLiveGateway):
    """Reprice BUY+MERGE from the fresh pair book instead of stale leg caps."""

    def quote_buy_from_book(
        self,
        book: Any,
        *,
        shares: float,
        max_price: float,
    ) -> DepthQuote:
        # `max_price` remains in the signature for compatibility with V2.  It is
        # deliberately not used here: the current pair-level economics gate decides
        # whether the fresh UP+DOWN combination is still an arbitrage.
        return consume_depth(
            self._levels(book, "asks"),
            shares=float(shares),
            buy=True,
            price_limit=None,
            min_order_size=self._min_order_size(book),
        )

    def buy_capacity_from_book(self, book: Any, *, max_price: float) -> dict[str, float]:
        # Fresh capacity is all executable visible ask depth.  We do not chase it
        # blindly: after Q selection, V2 recomputes exact fresh VWAP+fees+buffer and
        # rejects the pair unless LIVE_MIN_NET_PROFIT and LIVE_MIN_NET_ROI still pass.
        levels = self._levels(book, "asks")
        capacity = sum(max(0.0, float(size)) for _price, size in levels)
        return {
            "capacity_shares": max(0.0, float(capacity)),
            "min_order_size": self._min_order_size(book),
        }
