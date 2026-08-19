"""P2.5 resolution poller using authoritative Gamma event/market payloads.

The generic P1 poller queried ``/markets?condition_ids=<condition-id>``.  Live
rolling-market diagnostics showed that endpoint can return an empty list for an
already resolved market, while ``/events/slug/{slug}`` and ``/markets/{numeric-id}``
contain the final ``closed``, ``umaResolutionStatus`` and ``outcomePrices`` fields.

This subclass keeps discovery unchanged and replaces only the settlement lookup.
It is fail-closed: a market is labeled only when explicit winner metadata exists,
or when Gamma marks it resolved/automatically resolved and the final outcome prices
are decisive.  Live execution is not part of this module.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from discovery import MarketDiscovery, parse_official_result, parse_resolved_outcome
from models import Decision, LabelStatus, MarketRef

log = logging.getLogger("direction_engine.p25_discovery")


def authoritative_official_result(
    market: dict,
    up_token_id: str = "",
    down_token_id: str = "",
) -> tuple[Optional[Decision], str]:
    """Return an authoritative outcome or ``(None, reason)``.

    Priority:
    1. Explicit winner/status handled by the proven P1 parser.
    2. Gamma automatic-resolution final state: ``closed=true`` plus
       ``automaticallyResolved=true`` and decisive final ``outcomePrices``.

    A merely extreme live price is never accepted; ``closed`` is mandatory.
    """
    official, source = parse_official_result(
        market,
        up_token_id,
        down_token_id,
    )
    if official is not None:
        return official, source

    if not isinstance(market, dict):
        return None, "invalid_market_payload"
    if not bool(market.get("closed")):
        return None, "market_not_closed"

    automatic = bool(market.get("automaticallyResolved"))
    status = str(
        market.get("umaResolutionStatus")
        or market.get("resolutionStatus")
        or ""
    ).strip().lower()
    resolved_status = status in {
        "resolved",
        "finalized",
        "settled",
        "complete",
        "completed",
    }
    if not (automatic or resolved_status or bool(market.get("resolved"))):
        return None, "closed_but_not_authoritatively_resolved"

    outcome = parse_resolved_outcome(market)
    if outcome is None:
        return None, "resolved_but_outcome_not_decisive"
    source = (
        "automatic_resolved+outcomePrices"
        if automatic
        else "resolved_status+outcomePrices"
    )
    return outcome, source


class P25MarketDiscovery(MarketDiscovery):
    """MarketDiscovery with robust, observable resolution reconciliation."""

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002,ANN003
        super().__init__(*args, **kwargs)
        self.resolution_polls = 0
        self.resolution_fetch_empty = 0
        self.resolution_waiting = 0
        self.resolution_resolved = 0
        self.resolution_errors = 0
        self.last_resolution_poll_ts = 0.0
        self.last_resolution_source: Optional[str] = None

    async def _market_from_event_slug(
        self,
        ref: MarketRef,
    ) -> tuple[Optional[dict], str]:
        """Fetch the exact market from its immutable event slug."""
        if not ref.slug:
            return None, "no_slug"
        event = await self._fetch_json(
            f"{self.settings.gamma_host}/events/slug/{ref.slug}"
        )
        if not isinstance(event, dict):
            return None, "event_slug_empty"
        markets = event.get("markets") or []
        if not isinstance(markets, list):
            return None, "event_markets_invalid"
        for market in markets:
            if not isinstance(market, dict):
                continue
            condition_id = str(market.get("conditionId") or "")
            if ref.condition_id and condition_id == ref.condition_id:
                return market, "event_slug+condition_id"
        # Single-market rolling events are safe to use when conditionId is absent
        # from an older locally persisted reference, but not when multiple markets exist.
        valid = [m for m in markets if isinstance(m, dict)]
        if len(valid) == 1 and not ref.condition_id:
            return valid[0], "event_slug+single_market"
        return None, "event_market_not_matched"

    async def _market_from_condition_filter(
        self,
        ref: MarketRef,
    ) -> tuple[Optional[dict], str]:
        """Compatibility fallback; the live bug was that this may return []."""
        if not ref.condition_id:
            return None, "no_condition_id"
        data = await self._fetch_json(
            f"{self.settings.gamma_host}/markets",
            {"condition_ids": ref.condition_id},
        )
        if isinstance(data, list):
            for market in data:
                if (
                    isinstance(market, dict)
                    and str(market.get("conditionId") or "")
                    == ref.condition_id
                ):
                    return market, "condition_filter"
        elif isinstance(data, dict):
            if str(data.get("conditionId") or "") == ref.condition_id:
                return data, "condition_filter"
        return None, "condition_filter_empty"

    async def _fetch_resolution_market(
        self,
        ref: MarketRef,
    ) -> tuple[Optional[dict], str]:
        market, source = await self._market_from_event_slug(ref)
        if market is not None:
            return market, source
        fallback, fallback_source = await self._market_from_condition_filter(ref)
        if fallback is not None:
            return fallback, fallback_source
        return None, f"{source}|{fallback_source}"

    async def _poll_resolutions(self) -> None:
        """Poll every tracked expired market until Gamma exposes final resolution."""
        now = time.time()
        self.last_resolution_poll_ts = now
        pending = [
            ref
            for condition_id, ref in list(self._tracked.items())
            if condition_id
            and condition_id not in self._resolved_seen
            and ref.remaining_sec(now) < 30
        ]
        self.resolution_waiting = len(pending)

        for ref in pending:
            self.resolution_polls += 1
            try:
                market, fetch_source = await self._fetch_resolution_market(ref)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.resolution_errors += 1
                log.warning(
                    "RESOLUTION_FETCH_ERROR combo=%s slug=%s error=%s",
                    ref.combo.key,
                    ref.slug,
                    exc,
                )
                continue

            if market is None:
                self.resolution_fetch_empty += 1
                continue

            official, result_source = authoritative_official_result(
                market,
                ref.up_token_id,
                ref.down_token_id,
            )
            if official is None:
                # Expected while Gamma has not finalized the rolling market yet.
                continue

            sanity = parse_resolved_outcome(market)
            ref.label_status = (
                LabelStatus.MISMATCH
                if sanity is not None and sanity != official
                else LabelStatus.MATCH
            )
            ref.resolved = True
            ref.official_result = official
            ref.resolved_outcome = official
            ref.official_result_source = f"{fetch_source}:{result_source}"
            ref.official_resolved_at = now

            self._resolved_seen.add(ref.condition_id)
            self.resolved_log.appendleft(ref)
            self.resolution_resolved += 1
            self.resolution_waiting = max(0, self.resolution_waiting - 1)
            self.last_resolution_source = ref.official_result_source
            log.info(
                "RESOLVED %s -> %s source=%s slug=%s",
                ref.combo.key,
                official.value,
                ref.official_result_source,
                ref.slug,
            )

            for callback in self._on_resolved:
                try:
                    result = callback(ref)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:  # noqa: BLE001
                    self.resolution_errors += 1
                    log.exception(
                        "on_resolved callback failed combo=%s condition=%s: %s",
                        ref.combo.key,
                        ref.condition_id,
                        exc,
                    )
