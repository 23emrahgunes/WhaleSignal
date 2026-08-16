"""Birim testleri (pytest + pytest-asyncio).

Spec'in zorunlu 4 senaryosu + ek formul dogrulamalari:
  1. OBI matematiksel dogrulugu
  2. Kalan-sure (expiry) filtresi
  3. Tek-bacak / adverse-selection tetigi
  4. WS bozuk-JSON toleransi + exponential backoff ile yeniden baglanti
"""
from __future__ import annotations

import asyncio
import json

import pytest

from analytics_engine import (
    compute_adx,
    compute_atr,
    compute_obi,
    time_decay_ok,
)
from config import Settings
from data_ingestion import ReconnectingWSClient, backoff_delay
from execution_strategy import AdverseSelectionGuard, GuardAction
from models import BookLevel, Candle, Outcome


# ---------------------------------------------------------------------------
# 1. OBI
# ---------------------------------------------------------------------------


def _levels(*sizes: float) -> list[BookLevel]:
    return [BookLevel(price=0.5, size=s) for s in sizes]


def test_obi_balanced_is_zero():
    assert compute_obi(_levels(100), _levels(100)) == 0.0


def test_obi_three_to_one_is_half():
    # bids 300, asks 100 -> (300-100)/(300+100) = 0.5
    assert compute_obi(_levels(300), _levels(100)) == pytest.approx(0.5)


def test_obi_empty_book_is_zero():
    assert compute_obi([], []) == 0.0


def test_obi_full_ask_is_minus_one():
    assert compute_obi([], _levels(100)) == pytest.approx(-1.0)


# ---------------------------------------------------------------------------
# 2. Kalan sure (expiry) filtresi
# ---------------------------------------------------------------------------


def test_time_decay_allows_when_far_from_expiry():
    # vadeye 1000 sn (toplam 3000 sn) -> %33 > %10 -> True
    assert time_decay_ok(end_ts=1000.0, now=0.0, duration=3000.0, pct=0.10) is True


def test_time_decay_blocks_in_last_10_percent():
    # vadeye 50 sn (toplam 3000 sn) -> %1.6 < %10 -> False
    assert time_decay_ok(end_ts=50.0, now=0.0, duration=3000.0, pct=0.10) is False


def test_time_decay_blocks_after_expiry():
    assert time_decay_ok(end_ts=-5.0, now=0.0, duration=3000.0) is False


# ---------------------------------------------------------------------------
# 3. Tek-bacak / Adverse-Selection
# ---------------------------------------------------------------------------


def test_adverse_selection_triggers_after_timeout():
    guard = AdverseSelectionGuard(timeout_sec=15.0)
    guard.record_fill(Outcome.DOWN, ts=100.0)  # 0.40 DOWN doldu

    # 14 sn sonra: henuz tetiklenmez
    assert guard.check(now=114.0).action == GuardAction.NONE
    # 15 sn sonra: Up dolmadi -> acik emri iptal sinyali
    res = guard.check(now=115.0)
    assert res.action == GuardAction.CANCEL_OPEN
    assert res.missing_side == Outcome.UP


def test_adverse_selection_no_trigger_when_both_filled():
    guard = AdverseSelectionGuard(timeout_sec=15.0)
    guard.record_fill(Outcome.DOWN, ts=100.0)
    guard.record_fill(Outcome.UP, ts=101.0)
    assert guard.check(now=200.0).action == GuardAction.NONE


def test_adverse_selection_triggers_once():
    guard = AdverseSelectionGuard(timeout_sec=15.0)
    guard.record_fill(Outcome.UP, ts=0.0)
    assert guard.check(now=20.0).action == GuardAction.CANCEL_OPEN
    # ayni durum tekrar sorgulanirsa bir daha tetiklenmez
    assert guard.check(now=25.0).action == GuardAction.NONE


# ---------------------------------------------------------------------------
# 4. WS bozuk-JSON toleransi + exponential backoff reconnect
# ---------------------------------------------------------------------------


def test_backoff_is_exponential_and_capped():
    assert backoff_delay(0, base=1.0, factor=2.0, cap=30.0) == 1.0
    assert backoff_delay(1, base=1.0, factor=2.0, cap=30.0) == 2.0
    assert backoff_delay(2, base=1.0, factor=2.0, cap=30.0) == 4.0
    assert backoff_delay(10, base=1.0, factor=2.0, cap=30.0) == 30.0  # cap


class _JsonClient(ReconnectingWSClient):
    async def _handle(self, raw: str) -> None:
        json.loads(raw)  # bozuk JSON burada patlar


@pytest.mark.asyncio
async def test_safe_handle_tolerates_bad_json():
    cfg = Settings(_env_file=None)
    client = _JsonClient("ws://x", cfg, "t", session=None)  # type: ignore[arg-type]
    # bozuk JSON: istisna FIRLATMAZ, isleme sayilmaz
    await client._safe_handle("{bozuk json")
    assert client.messages_handled == 0
    # gecerli JSON: islenir
    await client._safe_handle('{"ok": 1}')
    assert client.messages_handled == 1


class _RaisingCM:
    async def __aenter__(self):
        raise ConnectionError("baglanti koptu")

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self) -> None:
        self.calls = 0

    def ws_connect(self, *args, **kwargs):
        self.calls += 1
        return _RaisingCM()


@pytest.mark.asyncio
async def test_reconnect_uses_exponential_backoff():
    cfg = Settings(_env_file=None)
    cfg.backoff_base_sec = 1.0
    cfg.backoff_factor = 2.0
    cfg.backoff_cap_sec = 30.0
    stop = asyncio.Event()
    delays: list[float] = []

    async def fake_sleep(d: float) -> None:
        delays.append(d)
        if len(delays) >= 3:
            stop.set()  # 3 denemeden sonra dur

    session = _FakeSession()
    client = _JsonClient("ws://x", cfg, "t", session=session, sleep=fake_sleep)  # type: ignore[arg-type]

    await asyncio.wait_for(client.run(stop), timeout=2.0)

    assert session.calls >= 3  # tekrar tekrar baglanmayi denedi
    assert client.reconnects >= 3
    assert delays[:3] == [1.0, 2.0, 4.0]  # exponential


# ---------------------------------------------------------------------------
# Ek: ATR / ADX formul akli-selim testleri
# ---------------------------------------------------------------------------


def _flat_candles(n: int, price: float = 100.0) -> list[Candle]:
    return [Candle(open_time=i, open=price, high=price, low=price, close=price, volume=1.0) for i in range(n)]


def _trend_candles(n: int, start: float = 100.0, step: float = 1.0) -> list[Candle]:
    out = []
    for i in range(n):
        base = start + i * step
        out.append(Candle(open_time=i, open=base, high=base + 0.5, low=base - 0.5, close=base + step, volume=1.0))
    return out


def test_atr_zero_on_flat_and_positive_on_moving():
    assert compute_atr(_flat_candles(30)) == pytest.approx(0.0)
    assert compute_atr(_trend_candles(30)) > 0.0


def test_adx_higher_on_trend_than_chop():
    trend = compute_adx(_trend_candles(60))
    chop = compute_adx(_flat_candles(60))
    assert trend > chop
