"""Periodic reconciliation for OPEN paper trades orphaned by a restart.

A paper entry may be created before a deployment restart and Gamma may resolve the
market after the old process has gone away.  The normal resolution callback cannot
fire for a market reference that no longer exists in memory.  This service scans
persisted OPEN rows, fetches the exact immutable event slug, verifies condition ID,
and settles only from an authoritative final Gamma result.

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

    async def _market_for_record(self, record: dict) -> tuple[Optional[dict], str]:
        slug = str(record.get("slug") or "").strip()
        condition_id = str(record.get("condition_id") or "").strip()
        if not slug:
            return None, "no_slug"

        event = await self.discovery._fetch_json(  # noqa: SLF001
            f"{self.cfg.gamma_host}/events/slug/{slug}"
        )
        if isinstance(event, dict):
            markets = event.get("markets") or []
            if isinstance(markets, list):
                for market in markets:
                    if not isinstance(market, dict):
                        continue
                    if str(market.get("conditionId") or "") == condition_id:
                        return market, "event_slug+condition_id"
                if markets:
                    self.condition_mismatch += 1

        # Compatibility fallback.  This endpoint was unreliable for some resolved
        # rolling markets, so it is never the primary source.
        if condition_id:
            fallback = await self.discovery._fetch_json(  # noqa: SLF001
                f"{self.cfg.gamma_host}/markets",
                {"condition_ids": condition_id},
            )
            if isinstance(fallback, list):
                for market in fallback:
                    if (
                        isinstance(market, dict)
                        and str(market.get("conditionId") or "") == condition_id
                    ):
                        return market, "condition_filter"
            elif (
                isinstance(fallback, dict)
                and str(fallback.get("conditionId") or "") == condition_id
            ):
                return fallback, "condition_filter"
        return None, "event_or_condition_empty"

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
