"""Regression tests for the P2.5 rolling-market settlement fix."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from models import (
    Asset,
    AssetHorizon,
    Decision,
    Horizon,
    LabelStatus,
    MarketRef,
    ResolutionType,
)
from p25_discovery import P25MarketDiscovery, authoritative_official_result


COMBO = AssetHorizon(Asset.BTC, Horizon.H15M)


def _ref(condition_id: str = "0xcondition") -> MarketRef:
    end = time.time() - 60.0
    return MarketRef(
        combo=COMBO,
        condition_id=condition_id,
        slug="btc-updown-15m-1787173200",
        question="Bitcoin Up or Down",
        up_token_id="up-token",
        down_token_id="down-token",
        start_ts=end - 900.0,
        end_ts=end,
        market_start_ts=end - 900.0,
        market_end_ts=end,
        resolution_source="Chainlink BTC/USD",
        resolution_type=ResolutionType.CHAINLINK_TWAP,
    )


def _resolved_market(condition_id: str = "0xcondition") -> dict:
    # Shape captured from the real Gamma rolling-market payload on 2026-08-19.
    return {
        "id": "3710010",
        "conditionId": condition_id,
        "slug": "btc-updown-15m-1787173200",
        "closed": True,
        "closedTime": "2026-08-19 21:15:54+00",
        "automaticallyResolved": True,
        "umaResolutionStatus": "resolved",
        "outcomes": '["Up", "Down"]',
        "outcomePrices": '["1", "0"]',
        "clobTokenIds": '["up-token", "down-token"]',
    }


def test_extreme_live_price_is_not_a_label():
    market = {
        "closed": False,
        "automaticallyResolved": False,
        "outcomes": '["Up", "Down"]',
        "outcomePrices": '["0.9995", "0.0005"]',
    }
    result, source = authoritative_official_result(market)
    assert result is None
    assert source == "market_not_closed"


def test_automatic_closed_gamma_payload_is_authoritative():
    result, source = authoritative_official_result(_resolved_market())
    assert result == Decision.UP
    # Existing explicit UMA status path has priority and is equally authoritative.
    assert source in {
        "resolved_status+outcomePrices",
        "automatic_resolved+outcomePrices",
    }


def test_closed_without_resolution_marker_remains_fail_closed():
    market = _resolved_market()
    market.pop("umaResolutionStatus")
    market["automaticallyResolved"] = False
    result, source = authoritative_official_result(market)
    assert result is None
    assert source == "closed_but_not_authoritatively_resolved"


@pytest.mark.asyncio
async def test_event_slug_lookup_resolves_when_condition_filter_would_be_empty():
    ref = _ref()
    discovery = P25MarketDiscovery(
        SimpleNamespace(gamma_host="https://gamma.invalid"),
        None,
        [COMBO],
    )
    discovery._tracked[ref.condition_id] = ref
    callbacks: list[MarketRef] = []

    async def fake_fetch(url: str, params=None):  # noqa: ANN001
        if "/events/slug/" in url:
            return {"markets": [_resolved_market(ref.condition_id)]}
        if url.endswith("/markets"):
            return []  # exact live failure mode of condition_ids lookup
        raise AssertionError(f"unexpected URL: {url}")

    async def callback(resolved_ref: MarketRef) -> None:
        callbacks.append(resolved_ref)

    discovery._fetch_json = fake_fetch  # type: ignore[method-assign]
    discovery.on_resolved(callback)
    await discovery._poll_resolutions()

    assert ref.resolved is True
    assert ref.official_result == Decision.UP
    assert ref.label_status == LabelStatus.MATCH
    assert ref.condition_id in discovery._resolved_seen
    assert discovery.resolution_resolved == 1
    assert discovery.resolution_fetch_empty == 0
    assert callbacks == [ref]
    assert ref.official_result_source.startswith("event_slug+condition_id:")


@pytest.mark.asyncio
async def test_wrong_condition_in_slug_payload_does_not_cross_label():
    ref = _ref("0xwanted")
    discovery = P25MarketDiscovery(
        SimpleNamespace(gamma_host="https://gamma.invalid"),
        None,
        [COMBO],
    )
    discovery._tracked[ref.condition_id] = ref

    async def fake_fetch(url: str, params=None):  # noqa: ANN001
        if "/events/slug/" in url:
            return {"markets": [_resolved_market("0xother")]}
        return []

    discovery._fetch_json = fake_fetch  # type: ignore[method-assign]
    await discovery._poll_resolutions()

    assert ref.resolved is False
    assert ref.condition_id not in discovery._resolved_seen
    assert discovery.resolution_fetch_empty == 1
