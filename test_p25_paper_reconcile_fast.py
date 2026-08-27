"""Regression tests for low-latency authoritative paper settlement."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from p25_paper_reconcile import PaperTradeReconciler


class _Recorder:
    def open_paper_trades(self):
        return []


class _Discovery:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def _fetch_json(self, url, params=None):
        self.calls.append((url, params))
        return self.responses.get((url, None if params is None else tuple(sorted(params.items()))))


def _cfg(poll=10):
    return SimpleNamespace(gamma_host="https://gamma.test", resolution_poll_sec=poll)


def test_exact_market_id_is_preferred_over_event_slug_and_condition_filter():
    condition = "0xabc"
    exact_url = "https://gamma.test/markets/123"
    discovery = _Discovery({(exact_url, None): {"conditionId": condition, "closed": True}})
    reconciler = PaperTradeReconciler(_cfg(), discovery, _Recorder())

    market, source = asyncio.run(
        reconciler._market_for_record(
            {
                "market_id": "123",
                "slug": "btc-updown-5m-123",
                "condition_id": condition,
            }
        )
    )

    assert market == {"conditionId": condition, "closed": True}
    assert source == "market_id+condition_id"
    assert discovery.calls == [(exact_url, None)]


def test_market_id_mismatch_falls_back_to_event_slug():
    condition = "0xabc"
    exact_url = "https://gamma.test/markets/123"
    event_url = "https://gamma.test/events/slug/btc-updown-5m-123"
    discovery = _Discovery(
        {
            (exact_url, None): {"conditionId": "wrong"},
            (event_url, None): {
                "markets": [{"conditionId": condition, "closed": True}]
            },
        }
    )
    reconciler = PaperTradeReconciler(_cfg(), discovery, _Recorder())

    market, source = asyncio.run(
        reconciler._market_for_record(
            {
                "market_id": "123",
                "slug": "btc-updown-5m-123",
                "condition_id": condition,
            }
        )
    )

    assert market == {"conditionId": condition, "closed": True}
    assert source == "event_slug+condition_id"
    assert reconciler.condition_mismatch == 1
    assert [call[0] for call in discovery.calls] == [exact_url, event_url]


def test_ten_second_resolution_poll_is_respected():
    reconciler = PaperTradeReconciler(_cfg(10), _Discovery({}), _Recorder())
    assert reconciler.interval_sec == 10.0


def test_poll_interval_cannot_be_more_aggressive_than_ten_seconds():
    reconciler = PaperTradeReconciler(_cfg(1), _Discovery({}), _Recorder())
    assert reconciler.interval_sec == 10.0
