"""Robust 5m/15m rollover discovery for P2.5.

Short Polymarket crypto events are heavily pre-listed and can disappear from the
small generic Gamma active-event page.  The exact deterministic slug remains the
fast path; this wrapper adds a series-scoped fallback and refuses far-future short
markets from the generic fallback.

SHADOW/PAPER discovery only.  No credentials, signing or order submission.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from discovery import parse_event_markets
from models import AssetHorizon, Horizon, MarketRef
from p25_discovery import P25MarketDiscovery as _BaseP25MarketDiscovery

log = logging.getLogger("direction_engine.p25_short_rollover")

_SHORT_HORIZONS = {Horizon.H5M, Horizon.H15M}


def short_series_slug(combo: AssetHorizon) -> str:
    """Return Gamma recurring-series slug for a 5m/15m crypto combo."""
    if combo.horizon not in _SHORT_HORIZONS:
        raise ValueError("short_series_slug only supports 5m/15m")
    return f"{combo.asset.value.lower()}-up-or-down-{combo.horizon.value}"


def is_current_short_ref(ref: MarketRef, now: float) -> bool:
    """True only for the market trading at *now*, never a prefetched future one."""
    start = float(ref.market_start_ts if ref.market_start_ts is not None else ref.start_ts)
    end = float(ref.market_end_ts if ref.market_end_ts is not None else ref.end_ts)
    return start <= float(now) < end


def select_current_short_ref(
    events: object,
    combo: AssetHorizon,
    *,
    now: float,
) -> Optional[MarketRef]:
    """Select the current market from a series-scoped Gamma event payload."""
    if combo.horizon not in _SHORT_HORIZONS or not isinstance(events, list):
        return None
    candidates: list[MarketRef] = []
    for event in events:
        if not isinstance(event, dict) or bool(event.get("closed")):
            continue
        ref = parse_event_markets(event, combo)
        if ref is not None and is_current_short_ref(ref, now):
            candidates.append(ref)
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda ref: float(
            ref.market_end_ts if ref.market_end_ts is not None else ref.end_ts
        ),
    )


class P25MarketDiscovery(_BaseP25MarketDiscovery):
    """P2.5 discovery with fail-closed current-window short-market rollover."""

    async def _series_path_short(self, combo: AssetHorizon) -> Optional[MarketRef]:
        if combo.horizon not in _SHORT_HORIZONS:
            return None
        series = short_series_slug(combo)
        payload = await self._fetch_json(
            f"{self.settings.gamma_host}/events",
            {
                "series_slug": series,
                "closed": "false",
                "limit": "100",
                "order": "endDate",
                "ascending": "true",
            },
        )
        now = time.time()
        ref = select_current_short_ref(payload, combo, now=now)
        if ref is not None:
            log.info(
                "%s short SERIES fallback FOUND series=%s slug=%s TTE=%.0fs",
                combo.key,
                series,
                ref.slug,
                ref.remaining_sec(now),
            )
        else:
            log.warning(
                "%s short SERIES fallback found no current market series=%s",
                combo.key,
                series,
            )
        return ref

    async def _fast_path_slug(self, combo: AssetHorizon) -> Optional[MarketRef]:
        """Exact current slug first; series-scoped current-window fallback second."""
        ref = await super()._fast_path_slug(combo)
        now = time.time()
        if ref is not None and is_current_short_ref(ref, now):
            return ref
        return await self._series_path_short(combo)

    async def _active_event_discovery(self) -> dict[str, MarketRef]:
        """Never let generic discovery replace short combos with future pre-listings."""
        found = await super()._active_event_discovery()
        now = time.time()
        for key, ref in list(found.items()):
            if ref.combo.horizon in _SHORT_HORIZONS and not is_current_short_ref(ref, now):
                log.warning(
                    "%s generic short candidate rejected as non-current slug=%s start=%s end=%s",
                    key,
                    ref.slug,
                    ref.market_start_ts,
                    ref.market_end_ts,
                )
                found.pop(key, None)
        return found
