"""Protocol-level regression tests for P1 market-data plumbing.

These tests deliberately exercise the public wire shapes rather than calling
store helpers directly, so a parser regression cannot hide behind green unit
helpers.
"""
from __future__ import annotations

import json
import time

import pytest

from chainlink_feed import ChainlinkFeed, ChainlinkState
from clob_feed import ClobOrderbookStream, ClobQuoteStore
from config import Settings
from models import Asset, AssetHorizon, Horizon, MarketRef, ResolutionType
from reference import ReferenceRouter


def _btc5m_ref(start: float) -> MarketRef:
    return MarketRef(
        combo=AssetHorizon(Asset.BTC, Horizon.H5M),
        condition_id="0xabc",
        slug=f"btc-updown-5m-{int(start)}",
        question="Bitcoin Up or Down",
        up_token_id="up-token",
        down_token_id="down-token",
        start_ts=start,
        end_ts=start + 300,
        market_start_ts=start,
        market_end_ts=start + 300,
        resolution_source=(
            "https://data.chain.link/streams/btc-usd | "
            "resolution source is the Chainlink BTC/USD data stream"
        ),
        # Legacy metadata may classify this as TWAP; the explicit Data Stream URL
        # must still route to the documented Chainlink RTDS lineage.
        resolution_type=ResolutionType.CHAINLINK_TWAP,
    )


def test_chainlink_subscription_matches_documented_rtds_shape():
    msg = ChainlinkFeed.subscribe_message()
    assert msg["action"] == "subscribe"
    assert msg["subscriptions"] == [
        {"topic": "crypto_prices_chainlink", "type": "*", "filters": ""}
    ]


@pytest.mark.asyncio
async def test_chainlink_documented_update_wire_format_is_parsed():
    feed = ChainlinkFeed(Settings(), None)
    ts_ms = int(time.time() * 1000)
    raw = json.dumps(
        {
            "topic": "crypto_prices_chainlink",
            "type": "update",
            "timestamp": ts_ms + 5,
            "payload": {
                "symbol": "btc/usd",
                "timestamp": ts_ms,
                "value": 64321.125,
            },
        }
    )
    parsed = await feed._handle_text(raw)
    assert parsed == 1
    state = feed.get_state("BTC")
    assert state is not None
    assert state.value == pytest.approx(64321.125)
    assert state.source_ts == pytest.approx(ts_ms / 1000.0)


@pytest.mark.asyncio
async def test_chainlink_full_accuracy_value_never_leaks_fixed_point_scale():
    """RTDS may include both decimal value and 18-decimal full_accuracy_value."""
    feed = ChainlinkFeed(Settings(), None)
    ts_ms = int(time.time() * 1000)
    raw = json.dumps(
        {
            "topic": "crypto_prices_chainlink",
            "type": "update",
            "payload": {
                "symbol": "btc/usd",
                "timestamp": ts_ms,
                "value": "64332.108538296714",
                "full_accuracy_value": "64332108538296714000000",
            },
        }
    )
    assert await feed._handle_text(raw) == 1
    state = feed.get_state("BTC")
    assert state is not None
    assert state.value == pytest.approx(64332.108538296714)
    assert state.value < 1_000_000.0  # guard against the previous ~6.4e22 bug


@pytest.mark.asyncio
async def test_chainlink_full_accuracy_fallback_is_scaled_by_1e18():
    feed = ChainlinkFeed(Settings(), None)
    ts_ms = int(time.time() * 1000)
    raw = json.dumps(
        {
            "topic": "crypto_prices_chainlink",
            "type": "update",
            "payload": {
                "symbol": "eth/usd",
                "timestamp": ts_ms,
                "full_accuracy_value": "1911558224865000000000",
            },
        }
    )
    assert await feed._handle_text(raw) == 1
    state = feed.get_state("ETH")
    assert state is not None
    assert state.value == pytest.approx(1911.558224865)


@pytest.mark.asyncio
async def test_chainlink_subscribe_snapshot_data_array_is_buffered():
    feed = ChainlinkFeed(Settings(), None)
    base = int(time.time() * 1000)
    raw = json.dumps(
        {
            "topic": "crypto_prices_chainlink",
            "type": "subscribe",
            "payload": {
                "symbol": "btc/usd",
                "data": [
                    {"timestamp": base - 1000, "value": 100.0},
                    {"timestamp": base, "value": 101.0},
                ],
            },
        }
    )
    parsed = await feed._handle_text(raw)
    assert parsed == 2
    assert len(feed.history["BTC"]) == 2
    assert feed.get_state("BTC").value == pytest.approx(101.0)


def test_chainlink_opening_state_is_source_timestamp_aligned():
    feed = ChainlinkFeed(Settings(), None)
    start = 1_800_000_000.0
    feed._record("BTC", ChainlinkState(99.0, start - 6.0, start - 6.0))
    feed._record("BTC", ChainlinkState(100.0, start + 0.8, start + 0.81))
    feed._record("BTC", ChainlinkState(101.0, start + 4.0, start + 4.01))

    opening = feed.opening_state("BTC", start, max_alignment_ms=5000)
    assert opening is not None
    assert opening.value == pytest.approx(100.0)
    assert opening.source_ts == pytest.approx(start + 0.8)

    # Mid-window restart with no boundary observation fails closed.
    assert feed.opening_state("BTC", start - 100.0, max_alignment_ms=5000) is None


def test_reference_router_uses_chainlink_data_stream_not_polygon_or_binance():
    start = time.time() - 1.0
    feed = ChainlinkFeed(Settings(), None)
    feed._record("BTC", ChainlinkState(64001.5, start + 0.4, time.time()))
    ref = _btc5m_ref(start)
    ref.proxy_reference_open = 63990.0

    router = ReferenceRouter(Settings(), chainlink=feed)
    router._acquire_5m15m_official(ref, time.time())

    assert ref.official_reference_open == pytest.approx(64001.5)
    assert ref.official_reference_source == "CHAINLINK_DATA_STREAM_RTDS"
    assert ref.official_reference_open != ref.proxy_reference_open
    assert ref.official_reference_open_time == pytest.approx(start + 0.4)


@pytest.mark.asyncio
async def test_clob_official_price_changes_wire_format_routes_by_inner_asset_id():
    store = ClobQuoteStore()
    stream = ClobOrderbookStream(Settings(), store, ["up-token"], None)
    store.apply_book(
        "up-token",
        [{"price": "0.54", "size": "10"}],
        [{"price": "0.57", "size": "8"}],
    )

    raw = json.dumps(
        {
            "event_type": "price_change",
            "market": "0x-condition-id-not-a-token",
            "price_changes": [
                {
                    "asset_id": "up-token",
                    "price": "0.55",
                    "size": "5",
                    "side": "BUY",
                    "best_bid": "0.55",
                    "best_ask": "0.57",
                }
            ],
        }
    )
    await stream._handle(raw)

    quote = store.get("up-token")
    assert quote is not None
    assert quote.best_bid == pytest.approx(0.55)
    assert quote.best_ask == pytest.approx(0.57)
    assert store.get("0x-condition-id-not-a-token") is None
    assert store.counters["clob_price_change_events"] > 0


@pytest.mark.asyncio
async def test_clob_best_bid_ask_wire_format_updates_quote():
    store = ClobQuoteStore()
    stream = ClobOrderbookStream(Settings(), store, ["down-token"], None)
    raw = json.dumps(
        {
            "event_type": "best_bid_ask",
            "asset_id": "down-token",
            "market": "0x-condition",
            "best_bid": "0.42",
            "best_ask": "0.44",
        }
    )
    await stream._handle(raw)
    quote = store.get("down-token")
    assert quote is not None
    assert quote.mid == pytest.approx(0.43)
    assert store.counters["clob_best_bid_ask_events"] == 1
