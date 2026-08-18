"""DataHub — tum feed'lerin birlesik anlik durumu (combo bazli FeatureSnapshot).

Kaynaklar: discovery (aktif marketler), binance_feed (spot + local book), clob_feed
(UP/DOWN kotalari), reference (horizon adaptorunun PTB'si). PTB pencerede sabit bir
capa oldugundan `reference_cache`'te tutulur ve ayri bir gorevle tazelenir.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import aiohttp

from binance_feed import BinanceFeed
from clob_feed import ClobQuoteStore
from config import Settings
from discovery import MarketDiscovery
from models import FeatureSnapshot, MarketRef
from reference import ReferenceRouter

log = logging.getLogger("direction_engine.hub")


class DataHub:
    def __init__(
        self,
        settings: Settings,
        discovery: MarketDiscovery,
        binance: BinanceFeed,
        clob_store: ClobQuoteStore,
        reference: ReferenceRouter,
    ) -> None:
        self.settings = settings
        self.discovery = discovery
        self.binance = binance
        self.clob_store = clob_store
        self.reference = reference
        self.reference_cache: dict[str, object] = {}  # condition_id -> ReferencePrice
        self.started_at = time.time()

    def active_token_ids(self) -> list[str]:
        ids: list[str] = []
        for ref in self.discovery.snapshot_active().values():
            if ref.up_token_id:
                ids.append(ref.up_token_id)
            if ref.down_token_id:
                ids.append(ref.down_token_id)
        return ids

    async def refresh_references(self, session: aiohttp.ClientSession) -> None:
        """Aktif her market icin PTB'yi (horizon adaptoru) cek/onbellekle."""
        for ref in self.discovery.snapshot_active().values():
            feed = self.binance.get_feed(ref.combo.binance_symbol)
            try:
                rp = await self.reference.reference_for(ref, feed, session)
            except Exception as exc:  # noqa: BLE001
                log.warning("%s PTB tazeleme hatasi: %s", ref.combo.key, exc)
                continue
            if rp.ok and ref.condition_id:
                self.reference_cache[ref.condition_id] = rp

    def build_snapshot(self, ref: MarketRef, now: Optional[float] = None) -> FeatureSnapshot:
        now = time.time() if now is None else now
        feed = self.binance.get_feed(ref.combo.binance_symbol)
        spot, spot_age = (None, None)
        book_age = transport_age = source_age = None
        if feed is not None:
            spot, spot_age = feed.spot_price()
            book_age = feed.book_age_ms()
            transport_age = feed.transport_age_ms()
            source_age = feed.source_event_age_ms()

        # PTB = market'e sabitlenen reference_open (resolution-tipine gore). Sabit capa.
        reference_price = ref.reference_open
        reference_age = 0.0 if reference_price is not None else None

        distance_usd = None
        distance_bps = None
        if spot is not None and reference_price:
            distance_usd = spot - reference_price
            distance_bps = (distance_usd / reference_price) * 10000.0

        # CLOB: up VE down token'in gercek bid/ask/mid. **0.505 fallback YOK** —
        # bid/ask yoksa None. token_id -> market_id reverse index (store token-anahtarli).
        up_q = self.clob_store.get(ref.up_token_id)
        down_q = self.clob_store.get(ref.down_token_id)
        up_bid = up_q.best_bid if up_q else None
        up_ask = up_q.best_ask if up_q else None
        up_mid = up_q.mid if up_q else None  # ClobQuote.mid None-guard'li
        down_bid = down_q.best_bid if down_q else None
        down_ask = down_q.best_ask if down_q else None
        down_mid = down_q.mid if down_q else None
        clob_spread = None
        clob_age = None
        if up_q is not None:
            if up_bid is not None and up_ask is not None:
                clob_spread = up_ask - up_bid
            clob_age = max(0.0, now * 1000 - up_q.ts * 1000)

        tte = ref.remaining_sec(now)
        return FeatureSnapshot(
            combo=ref.combo,
            ts=now,
            seconds_remaining=tte,
            market_start=ref.market_start_ts,
            market_end=ref.market_end_ts,
            tte_sec=tte,
            spot_price=spot,
            reference_price=reference_price,
            distance_usd=distance_usd,
            distance_bps=distance_bps,
            up_bid=up_bid,
            up_ask=up_ask,
            up_mid=up_mid,
            down_bid=down_bid,
            down_ask=down_ask,
            down_mid=down_mid,
            clob_spread=clob_spread,
            spot_age_ms=spot_age,
            book_age_ms=book_age,
            transport_age_ms=transport_age,
            source_age_ms=source_age,
            clob_age_ms=clob_age,
            reference_age_ms=reference_age,
        )
