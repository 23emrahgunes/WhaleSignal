"""Offline SIM demo — dis ag GEREKTIRMEZ.

Gercek modulleri (should_enter + ClobExecutor(SIM) + Simulator +
AdverseSelectionGuard) sentetik veriyle surer. Iki senaryo:
  A) Sakin/simetrik market -> box acilir, iki bacak dolar -> KILITLENDI (garanti kar)
  B) Tek bacak dolar, karsi bacak 15 sn'de dolmaz -> ADVERSE_SELECTION iptal

Calistir:  .venv/Scripts/python.exe sim_demo.py
"""
from __future__ import annotations

import logging
import time

from analytics_engine import build_analytics
from clob_executor import ClobExecutor
from config import Settings
from data_ingestion import DataHub
from main import StrategyRunner
from models import BookLevel, Candle, ExecMode, MarketMeta, MarketState, OrderBook


def _calm_candles(n: int = 20, mid: float = 63000.0) -> list[Candle]:
    """Choppy/yatay mumlar -> dusuk ATR%, dusuk ADX (konsolidasyon)."""
    out = []
    for i in range(n):
        c = mid + (5.0 if i % 2 else -5.0)  # +-$5 salinim
        out.append(Candle(open_time=i, open=mid, high=c + 3, low=c - 3, close=c, volume=1.0))
    return out


def _book(token: str, ask: float, bid: float = 0.40, size: float = 100.0) -> OrderBook:
    return OrderBook(
        token_id=token,
        bids=[BookLevel(bid, size)],
        asks=[BookLevel(ask, size)],
    )


def _state(cfg: Settings, hub: DataHub, now: float) -> MarketState:
    _, book_up, book_down, candles, iv = hub.snapshot()
    analytics = build_analytics(book_up, candles, iv, saturation_eps=cfg.saturation_eps)
    return MarketState(meta=hub.meta, book_up=book_up, book_down=book_down, analytics=analytics, now=now)


def _make(cfg: Settings) -> tuple[DataHub, StrategyRunner]:
    hub = DataHub()
    t0 = time.time()
    hub.meta = MarketMeta("demo", "BTC up/down", "UP", "DOWN", start_ts=t0, end_ts=t0 + 300)
    for c in _calm_candles():
        hub.candles.append(c)
    hub.implied_vol = 55.0
    runner = StrategyRunner(cfg, hub, ClobExecutor(cfg))
    return hub, runner


def scenario_a(cfg: Settings) -> None:
    print("\n=== SENARYO A: sakin market -> box acilir, iki bacak dolar (KILIT) ===")
    hub, runner = _make(cfg)
    t = time.time()
    # asks 0.41 (henuz dolmaz) — simetrik tahta, OBI ~0
    hub.book_up = _book("UP", ask=0.41)
    hub.book_down = _book("DOWN", ask=0.41)
    runner.tick(_state(cfg, hub, t))  # -> giris karari (box acilmali)
    print(f"  box acik mi: {runner.sim.has_open_box}")
    # asks 0.40'a duser -> iki bacak da dolar
    hub.book_up = _book("UP", ask=0.40)
    hub.book_down = _book("DOWN", ask=0.40)
    runner.tick(_state(cfg, hub, t + 1))  # -> dolum + kilit
    print(f"  final istatistik: {runner.sim.stats.as_dict()}")


def scenario_b(cfg: Settings) -> None:
    print("\n=== SENARYO B: tek bacak kalir -> 15 sn sonra ADVERSE_SELECTION iptal ===")
    hub, runner = _make(cfg)
    t = time.time()
    hub.book_up = _book("UP", ask=0.41)
    hub.book_down = _book("DOWN", ask=0.41)
    runner.tick(_state(cfg, hub, t))  # box acilir
    # yalniz DOWN dolar (asks 0.40), UP asks 0.41'de kalir
    hub.book_down = _book("DOWN", ask=0.40)
    runner.tick(_state(cfg, hub, t + 1))
    print(f"  t+1: box durumu -> guard={runner.guard.status.value}")
    # 14 sn sonra: henuz iptal yok
    runner.tick(_state(cfg, hub, t + 14))
    print(f"  t+14: box hala acik mi: {runner.sim.has_open_box}")
    # 16 sn sonra: karsi bacak yok -> iptal
    runner.tick(_state(cfg, hub, t + 16))
    print(f"  t+16: box acik mi: {runner.sim.has_open_box}  (iptal edildi)")
    print(f"  final istatistik: {runner.sim.stats.as_dict()}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    cfg = Settings(_env_file=None)
    cfg.exec_mode = ExecMode.SIM
    cfg.entry_price = 0.40
    cfg.order_size = 5.0
    cfg.single_leg_timeout_sec = 15.0
    print(f"EXEC_MODE={cfg.exec_mode.value}  (gercek emir YOK)")
    scenario_a(cfg)
    scenario_b(cfg)


if __name__ == "__main__":
    main()
