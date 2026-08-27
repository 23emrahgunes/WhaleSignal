"""Fast periodic reconciliation for persisted OPEN paper trades.

Paper entries are settled only from authoritative Gamma resolution metadata.  The
reconciler prefers the exact immutable market id when available, then falls back to
the event slug and finally the condition-id filter.  This avoids waiting on a stale
event payload when the exact market endpoint has already published the final result.

Paper/SHADOW only.  No order execution is performed.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from p25_discovery import authoritative_official_result

log = logging.getLogger("direction_engine.paper_reconcile")


class PaperTradeReconciler:
    def __init__(self, cfg, discovery, recorder) -> None:  # noqa: ANN001
        self.cfg = cfg
        self.discovery = discovery
        self.recorder = recorder
        self.interval_sec = max(10.0, float(getattr(cfg, "resolution_poll_sec", 30)))
        self.runs = 0
        self.rows_seen = 0
        self.settled = 0
        self.unresolved = 0
        self.fetch_empty = 0
        self.condition_mismatch = 0
        self.errors = 0
        self.last_run_ts = 0.0
        self.last_settled_condition: Optional[str] = None
        self.last_source: Optional[str] = None

    def snapshot(self) -> dict:
        return {
            "runs": self.runs,
            "rows_seen": self.rows_seen,
            "settled": self.settled,
            "unresolved": self.unresolved,
            "fetch_empty": self.fetch_empty,
            "condition_mismatch": self.condition_mismatch,
            "errors": self.errors,
            "last_run_ts": self.last_run_ts,
            "last_settled_condition": self.last_settled_condition,
            "last_source": self.last_source,
            "interval_sec": self.interval_sec,
        }

    @staticmethod
    def _condition_matches(market: object, condition_id: str) -> bool:
        return (
            isinstance(market, dict)
            and str(market.get("conditionId") or "") == condition_id
        )

    async def _market_for_record(self, record: dict) -> tuple[Optional[dict], str]:
        slug = str(record.get("slug") or "").strip()
        condition_id = str(record.get("condition_id") or "").strip()
        market_id = str(record.get("market_id") or "").strip()

        # Exact market endpoint is the fastest/least ambiguous path after expiry.
        # It is attempted first because event payloads can lag the final market state.
        if market_id:
            exact = await self.discovery._fetch_json(  # noqa: SLF001
                f"{self.cfg.gamma_host}/markets/{market_id}"
            )
            if self._condition_matches(exact, condition_id):
                return exact, "market_id+condition_id"
            if isinstance(exact, dict):
                self.condition_mismatch += 1

        if slug:
            event = await self.discovery._fetch_json(  # noqa: SLF001
                f"{self.cfg.gamma_host}/events/slug/{slug}"
            )
            if isinstance(event, dict):
                markets = event.get("markets") or []
                if isinstance(markets, list):
                    for market in markets:
                        if self._condition_matches(market, condition_id):
                            return market, "event_slug+condition_id"
                    if markets:
                        self.condition_mismatch += 1

        # Compatibility fallback. This endpoint is known to return [] for some
        # resolved rolling markets, so it remains the final source only.
        if condition_id:
            fallback = await self.discovery._fetch_json(  # noqa: SLF001
                f"{self.cfg.gamma_host}/markets",
                {"condition_ids": condition_id},
            )
            if isinstance(fallback, list):
                for market in fallback:
                    if self._condition_matches(market, condition_id):
                        return market, "condition_filter"
            elif self._condition_matches(fallback, condition_id):
                return fallback, "condition_filter"

        return None, "market_id_event_condition_empty"

    async def reconcile_once(self) -> dict:
        self.runs += 1
        self.last_run_ts = time.time()
        getter = getattr(self.recorder, "open_paper_trades", None)
        if not callable(getter):
            self.errors += 1
            log.error("paper recorder has no open_paper_trades()")
            return self.snapshot()

        rows = getter()
        self.rows_seen += len(rows)
        for record in rows:
            condition_id = str(record.get("condition_id") or "")
            try:
                market, fetch_source = await self._market_for_record(record)
                if market is None:
                    self.fetch_empty += 1
                    continue
                official, result_source = authoritative_official_result(market)
                if official is None:
                    self.unresolved += 1
                    continue
                source = f"{fetch_source}:{result_source}"
                settled_count = self.recorder.settle_open_paper_condition(
                    condition_id,
                    official.value,
                    settled_at=time.time(),
                    source=source,
                )
                if settled_count:
                    self.settled += settled_count
                    self.last_settled_condition = condition_id
                    self.last_source = source
                    log.info(
                        "PAPER RECONCILE resolved condition=%s combo=%s "
                        "result=%s count=%d source=%s",
                        condition_id,
                        record.get("combo_key"),
                        official.value,
                        settled_count,
                        source,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.errors += 1
                log.exception(
                    "PAPER RECONCILE error condition=%s slug=%s: %s",
                    condition_id,
                    record.get("slug"),
                    exc,
                )
        return self.snapshot()

    async def run(self, stop: asyncio.Event) -> None:
        # Run immediately at startup so old OPEN positions do not wait for the first
        # interval after a deployment.
        while not stop.is_set():
            await self.reconcile_once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval_sec)
            except asyncio.TimeoutError:
                continue
