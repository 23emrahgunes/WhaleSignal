"""Runtime hardening for the BTC/ETH/SOL/XRP 5m SMC V3 cohort.

The SMC strategy is five-minute-only.  This module keeps that contract explicit,
trims feature history to the longest window actually consumed by the 5m feature
engine, retains a still-current short market through a transient Gamma miss, and
makes the DRY probe actively reacquire a missing current 5m market before failing.

No order is created by the market-acquisition path.  The normal DRY implementation
still performs the authenticated account and eight UP/DOWN book requests and still
fails closed unless all four current markets are verified.
"""
from __future__ import annotations

import asyncio
import bisect
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from collections.abc import Iterable
from typing import Any

import features
import p25_main
import p25_smc
import p25_smc_patch
from discovery import build_slug, parse_event_markets, window_start
from models import Asset, AssetHorizon, DiscoveryStatus, Horizon
from p25_live_all5m_market import All5mMarketBuyController
from p25_short_rollover import (
    P25MarketDiscovery,
    is_current_short_ref,
    select_current_short_ref,
    short_series_slug,
)

log = logging.getLogger("direction_engine.p25.smc_runtime")

_ASSETS = ("BTC", "ETH", "SOL", "XRP")
_FEATURE_LOOKBACK_MS = 185_000
_SMC_LOOKBACK_MS = 100_000
_INSTALLED = False
_ORIGINAL_FEATURE_UPDATE = None
_ORIGINAL_DISCOVER_ONCE = None


def _trim_sorted_prices(
    prices: Iterable[tuple[int, float]],
    *,
    now_ms: int,
    lookback_ms: int,
) -> list[tuple[int, float]]:
    """Return only the source-timestamped tail required by a consumer.

    Binance's feature series is monotonically ordered.  ``bisect`` avoids scanning
    the complete 24k ring on every 500ms engine tick.  A defensive filtering path is
    retained for tests or non-list iterables.
    """
    rows = prices if isinstance(prices, list) else list(prices)
    if not rows:
        return []
    cutoff = int(now_ms) - int(lookback_ms)
    try:
        start = bisect.bisect_left(rows, (cutoff, float("-inf")))
        return list(rows[start:])
    except (TypeError, ValueError):
        out: list[tuple[int, float]] = []
        for raw_ts, raw_price in rows:
            try:
                ts = int(raw_ts)
                price = float(raw_price)
            except (TypeError, ValueError):
                continue
            if ts >= cutoff:
                out.append((ts, price))
        return out


def _asset_from_ref(ref: Any) -> str | None:
    try:
        asset = str(ref.combo.asset.value).upper()
        horizon = str(ref.combo.horizon.value).lower()
    except Exception:  # noqa: BLE001
        return None
    if asset in _ASSETS and horizon == "5m":
        return asset
    return None


def _refs_by_asset(refs: Iterable[Any]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for ref in refs:
        asset = _asset_from_ref(ref)
        if asset is not None and asset not in found:
            found[asset] = ref
    return found


class ResilientAll5mMarketBuyController(All5mMarketBuyController):
    """ALL-5m controller with a fail-closed current-market DRY reacquisition."""

    @staticmethod
    def _http_json(url: str, *, timeout: float) -> object:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "WhaleSignal-SMC-V3-DRY/1.0"},
        )
        with urllib.request.urlopen(request, timeout=max(0.5, timeout)) as response:
            return json.loads(response.read().decode("utf-8"))

    def _current_ref_from_gamma(self, asset: str, *, timeout: float) -> Any | None:
        combo = AssetHorizon(Asset(asset), Horizon.H5M)
        now = time.time()
        slug = build_slug(combo.asset, combo.horizon, window_start(combo.horizon, now))
        base = str(self.cfg.gamma_host).rstrip("/")

        try:
            event = self._http_json(
                f"{base}/events/slug/{slug}",
                timeout=timeout,
            )
        except Exception:  # noqa: BLE001
            event = None
        if isinstance(event, dict) and not bool(event.get("closed")):
            ref = parse_event_markets(event, combo)
            if ref is not None and is_current_short_ref(ref, time.time()):
                return ref

        params = urllib.parse.urlencode(
            {
                "series_slug": short_series_slug(combo),
                "closed": "false",
                "limit": "100",
                "order": "endDate",
                "ascending": "true",
            }
        )
        try:
            events = self._http_json(
                f"{base}/events?{params}",
                timeout=timeout,
            )
        except Exception:  # noqa: BLE001
            return None
        return select_current_short_ref(events, combo, now=time.time())

    def dry_probe(self, refs):  # noqa: ANN001,ANN201
        collected = _refs_by_asset(list(refs))
        initially_missing = [asset for asset in _ASSETS if asset not in collected]
        wait_sec_raw = os.getenv("P25_DRY_MARKET_WAIT_SEC", "22")
        try:
            wait_sec = max(0.0, min(30.0, float(wait_sec_raw)))
        except ValueError:
            wait_sec = 22.0

        started = time.monotonic()
        attempts = 0
        deadline = started + wait_sec
        while any(asset not in collected for asset in _ASSETS):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            attempts += 1
            missing = [asset for asset in _ASSETS if asset not in collected]
            for asset in missing:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                ref = self._current_ref_from_gamma(
                    asset,
                    timeout=min(3.0, max(0.5, remaining)),
                )
                if ref is not None:
                    collected[asset] = ref
                    log.info(
                        "DRY current 5m market reacquired asset=%s slug=%s",
                        asset,
                        getattr(ref, "slug", None),
                    )
            if any(asset not in collected for asset in _ASSETS):
                time.sleep(min(1.5, max(0.0, deadline - time.monotonic())))

        result = super().dry_probe(list(collected.values()))
        checks = result.setdefault("checks", {})
        checks["market_acquisition"] = {
            "initially_missing": initially_missing,
            "reacquired": [
                asset
                for asset in initially_missing
                if asset in collected
            ],
            "still_missing": [asset for asset in _ASSETS if asset not in collected],
            "attempts": attempts,
            "waited_sec": round(time.monotonic() - started, 3),
            "fail_closed": True,
        }
        return result


def _install_feature_tail_trim() -> None:
    global _ORIGINAL_FEATURE_UPDATE
    if _ORIGINAL_FEATURE_UPDATE is not None:
        return
    _ORIGINAL_FEATURE_UPDATE = features.FeatureEngine.update

    def update_with_tail(self, *args, **kwargs):  # noqa: ANN001,ANN002,ANN003
        now = kwargs.get("now")
        if now is None and len(args) >= 8:
            now = args[7]
        now = float(now or time.time())

        horizon = str(getattr(getattr(self, "combo", None), "horizon", ""))
        horizon = str(getattr(getattr(self, "combo", None), "horizon", horizon))
        horizon_value = str(
            getattr(getattr(getattr(self, "combo", None), "horizon", None), "value", horizon)
        ).lower()
        if horizon_value != "5m":
            return _ORIGINAL_FEATURE_UPDATE(self, *args, **kwargs)

        if "prices" in kwargs:
            updated_kwargs = dict(kwargs)
            updated_kwargs["prices"] = _trim_sorted_prices(
                kwargs.get("prices") or [],
                now_ms=int(now * 1000.0),
                lookback_ms=_FEATURE_LOOKBACK_MS,
            )
            return _ORIGINAL_FEATURE_UPDATE(self, *args, **updated_kwargs)

        if args:
            updated_args = list(args)
            updated_args[0] = _trim_sorted_prices(
                updated_args[0] or [],
                now_ms=int(now * 1000.0),
                lookback_ms=_FEATURE_LOOKBACK_MS,
            )
            return _ORIGINAL_FEATURE_UPDATE(self, *updated_args, **kwargs)
        return _ORIGINAL_FEATURE_UPDATE(self, *args, **kwargs)

    features.FeatureEngine.update = update_with_tail


def _install_fast_smc_compute() -> None:
    native_compute = p25_smc.compute_smc_state

    def compute_fast(prices, *, now_ms):  # noqa: ANN001,ANN202
        tail = _trim_sorted_prices(
            prices or [],
            now_ms=int(now_ms),
            lookback_ms=_SMC_LOOKBACK_MS,
        )
        return native_compute(tail, now_ms=int(now_ms))

    p25_smc_patch.compute_smc_state = compute_fast


def _install_short_market_retention() -> None:
    global _ORIGINAL_DISCOVER_ONCE
    if _ORIGINAL_DISCOVER_ONCE is not None:
        return
    _ORIGINAL_DISCOVER_ONCE = P25MarketDiscovery.discover_once

    async def discover_once_with_retention(self):  # noqa: ANN001
        previous = self.snapshot_active()
        await _ORIGINAL_DISCOVER_ONCE(self)
        now = time.time()
        restored: list[str] = []
        async with self._lock:
            for key, ref in previous.items():
                if key in self.active:
                    continue
                try:
                    is_5m = ref.combo.horizon == Horizon.H5M
                except Exception:  # noqa: BLE001
                    is_5m = False
                if is_5m and is_current_short_ref(ref, now):
                    self.active[key] = ref
                    self.status[key] = DiscoveryStatus.FOUND
                    restored.append(key)
        if restored:
            log.warning(
                "Transient Gamma miss; retained still-current 5m refs=%s",
                ",".join(sorted(restored)),
            )

    P25MarketDiscovery.discover_once = discover_once_with_retention


def install_smc_v3_runtime_hardening() -> None:
    """Install five-minute-only runtime hardening exactly once."""
    global _INSTALLED
    if _INSTALLED:
        return

    # The SMC V3 cohort trades only 5m.  Running 15m/1h feature engines was wasted
    # CPU and could starve aiohttp after the 24k Binance history warmed up.
    os.environ["ASSETS"] = ",".join(_ASSETS)
    os.environ["HORIZONS"] = "5m"
    os.environ.setdefault("P25_DRY_MARKET_WAIT_SEC", "22")

    _install_feature_tail_trim()
    _install_fast_smc_compute()
    _install_short_market_retention()

    # p25_main imported the controller class at module import time; replace the
    # module-global factory target before p25_main.run constructs the controller.
    p25_main.All5mMarketBuyController = ResilientAll5mMarketBuyController
    _INSTALLED = True
