"""Regression tests for deterministic P2.6 current-session book seeding."""
from __future__ import annotations

from p26_book_daemon import LocalBook
from p26_book_daemon_resilient_v2 import (
    ResilientBookCollectorV2,
    _source_timestamp_ms,
)


class _FakeStore:
    def __init__(self) -> None:
        self.calls = []

    def insert(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls.append(kwargs)
        return False


def _collector() -> ResilientBookCollectorV2:
    c = ResilientBookCollectorV2.__new__(ResilientBookCollectorV2)
    c.token_meta = {
        "up-token": ("cond", "BTC:5m", "UP"),
        "down-token": ("cond", "BTC:5m", "DOWN"),
    }
    c.local_books = {
        "up-token": LocalBook(),
        "down-token": LocalBook(),
    }
    c.books = _FakeStore()
    c.persisted = 0
    c.last_persist_ms = {}
    return c


def _book(token: str, ts: str = "1800000000123") -> dict:
    return {
        "market": "0xcond",
        "asset_id": token,
        "timestamp": ts,
        "bids": [{"price": "0.49", "size": "10"}],
        "asks": [{"price": "0.51", "size": "12"}],
        "hash": "x",
        "min_order_size": "1",
        "tick_size": "0.01",
        "neg_risk": False,
        "last_trade_price": "0.50",
    }


def test_session_seed_observes_both_legs_at_or_after_session_start():
    c = _collector()
    seeded, missing = c._apply_session_seed(
        [_book("up-token"), _book("down-token")],
        recv_ms=1_800_000_000_500,
        session_started_ms=1_800_000_000_400,
    )

    assert seeded == 2
    assert missing == 0
    assert len(c.books.calls) == 2
    assert {call["side"] for call in c.books.calls} == {"UP", "DOWN"}
    assert all(call["recv_ts_ms"] >= 1_800_000_000_400 for call in c.books.calls)
    assert c.last_persist_ms == {
        "up-token": 1_800_000_000_500,
        "down-token": 1_800_000_000_500,
    }


def test_session_seed_preserves_exchange_source_timestamp():
    c = _collector()
    c._apply_session_seed(
        [_book("up-token", "1800000000123")],
        recv_ms=1_800_000_009_999,
        session_started_ms=1_800_000_009_000,
    )
    snapshot = c.books.calls[0]["snapshot"]
    assert snapshot.ts_ms == 1_800_000_000_123
    assert c.books.calls[0]["recv_ts_ms"] == 1_800_000_009_999


def test_session_seed_is_fail_closed_for_unusable_ask_book():
    c = _collector()
    bad = _book("up-token")
    bad["asks"] = []
    seeded, missing = c._apply_session_seed(
        [bad, _book("down-token")],
        recv_ms=1_800_000_000_500,
        session_started_ms=1_800_000_000_400,
    )
    assert seeded == 1
    assert missing == 1
    assert len(c.books.calls) == 1
    assert c.books.calls[0]["side"] == "DOWN"


def test_source_timestamp_normalization():
    assert _source_timestamp_ms("1800000000", 1) == 1_800_000_000_000
    assert _source_timestamp_ms("1800000000123", 1) == 1_800_000_000_123
    assert _source_timestamp_ms("bad", 123) == 123
