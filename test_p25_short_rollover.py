"""Regression tests for P2.5 5m/15m current-market rollover discovery."""
from __future__ import annotations

import json

from models import Asset, AssetHorizon, Horizon
from p25_short_rollover import (
    is_current_short_ref,
    select_current_short_ref,
    short_series_slug,
)


BTC5 = AssetHorizon(Asset.BTC, Horizon.H5M)
ETH15 = AssetHorizon(Asset.ETH, Horizon.H15M)


def _event(combo: AssetHorizon, start: int, condition: str) -> dict:
    return {
        "slug": f"{combo.asset.value.lower()}-updown-{combo.horizon.value}-{start}",
        "title": f"{combo.asset.value} Up or Down",
        "closed": False,
        "description": "Resolves according to Chainlink TWAP.",
        "markets": [
            {
                "conditionId": condition,
                "closed": False,
                "clobTokenIds": json.dumps([f"{condition}-up", f"{condition}-down"]),
                "outcomes": json.dumps(["Up", "Down"]),
                "description": "Resolves according to Chainlink TWAP.",
            }
        ],
    }


def test_short_series_slug_is_series_scoped():
    assert short_series_slug(BTC5) == "btc-up-or-down-5m"
    assert short_series_slug(ETH15) == "eth-up-or-down-15m"


def test_series_selector_chooses_current_not_far_future():
    now = 1_800_000_125.0
    current_start = 1_800_000_000
    future_start = current_start + 86_400
    ref = select_current_short_ref(
        [
            _event(BTC5, future_start, "0xfuture"),
            _event(BTC5, current_start, "0xcurrent"),
        ],
        BTC5,
        now=now,
    )
    assert ref is not None
    assert ref.condition_id == "0xcurrent"
    assert is_current_short_ref(ref, now)
    assert ref.market_start_ts == current_start
    assert ref.market_end_ts == current_start + 300


def test_series_selector_returns_none_when_only_future_is_listed():
    now = 1_800_000_125.0
    ref = select_current_short_ref(
        [_event(BTC5, 1_800_086_400, "0xfuture")],
        BTC5,
        now=now,
    )
    assert ref is None
