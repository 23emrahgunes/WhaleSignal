"""Paper-trading / simulasyon katmani.

Canli veya kaydedilmis WS akisini tuketip 0.40 cift-bacak emirlerinin dolup
dolmadigini (fill-rate), kar/zarar (PnL) ve Sharpe oranini hesaplar. Gercek emir
gondermez; `ClobExecutor(SIM)` ile birlikte kullanilir.

Basit dolum modeli: 0.40 BUY resting emri, karsi tarafin en iyi ask'i <= 0.40
oldugunda dolar (defter fiyattan gecti). Iki bacak da dolarsa 'box' kilitlenir
(garanti kar = size*(1 - cift_maliyet)); tek bacak kalirsa mevcut bid'e
isaretlenip (mark-to-bid) zarar yazilir.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from typing import Iterator, Optional

from models import Outcome, OrderBook


@dataclass
class BoxLeg:
    outcome: Outcome
    price: float
    size: float
    filled: bool = False
    fill_ts: Optional[float] = None
    fill_price: Optional[float] = None


@dataclass
class BoxTrade:
    opened_ts: float
    up: BoxLeg
    down: BoxLeg
    closed: bool = False
    close_reason: str = ""
    pnl: float = 0.0

    @property
    def both_filled(self) -> bool:
        return self.up.filled and self.down.filled


@dataclass
class SimStats:
    boxes: int = 0
    completed: int = 0  # iki bacak dolan
    stranded: int = 0  # tek bacak kalan
    leg_fills: int = 0
    leg_attempts: int = 0
    pnls: list[float] = field(default_factory=list)

    @property
    def fill_rate(self) -> float:
        return self.leg_fills / self.leg_attempts if self.leg_attempts else 0.0

    @property
    def total_pnl(self) -> float:
        return float(sum(self.pnls))

    @property
    def completion_rate(self) -> float:
        return self.completed / self.boxes if self.boxes else 0.0

    def sharpe(self) -> float:
        """Islem-basi getirilerin Sharpe'i (risksiz oran 0 varsayimi)."""
        n = len(self.pnls)
        if n < 2:
            return 0.0
        mean = sum(self.pnls) / n
        var = sum((x - mean) ** 2 for x in self.pnls) / (n - 1)
        std = math.sqrt(var)
        if std <= 0:
            return 0.0
        return (mean / std) * math.sqrt(n)

    def as_dict(self) -> dict[str, float]:
        return {
            "boxes": self.boxes,
            "completed": self.completed,
            "stranded": self.stranded,
            "completionRate": round(self.completion_rate, 4),
            "fillRate": round(self.fill_rate, 4),
            "totalPnl": round(self.total_pnl, 4),
            "sharpe": round(self.sharpe(), 4),
        }


class Simulator:
    """Tek-box yasam dongusu + toplu istatistik."""

    def __init__(self, entry_price: float = 0.40, size: float = 5.0) -> None:
        self.entry_price = entry_price
        self.size = size
        self.stats = SimStats()
        self.active: Optional[BoxTrade] = None

    @property
    def has_open_box(self) -> bool:
        return self.active is not None and not self.active.closed

    def open_box(self, now: Optional[float] = None) -> BoxTrade:
        now = time.time() if now is None else now
        box = BoxTrade(
            opened_ts=now,
            up=BoxLeg(Outcome.UP, self.entry_price, self.size),
            down=BoxLeg(Outcome.DOWN, self.entry_price, self.size),
        )
        self.active = box
        self.stats.boxes += 1
        self.stats.leg_attempts += 2
        return box

    def on_tick(
        self, book_up: Optional[OrderBook], book_down: Optional[OrderBook], now: Optional[float] = None
    ) -> list[Outcome]:
        """Aktif box'in bacaklarini dolum icin kontrol et. Yeni dolanlari doner."""
        now = time.time() if now is None else now
        if not self.has_open_box:
            return []
        assert self.active is not None
        newly: list[Outcome] = []
        for leg, book in ((self.active.up, book_up), (self.active.down, book_down)):
            if leg.filled or book is None:
                continue
            if _fills(book, leg.price):
                leg.filled = True
                leg.fill_ts = now
                leg.fill_price = leg.price
                self.stats.leg_fills += 1
                newly.append(leg.outcome)
        return newly

    def close_box(
        self,
        reason: str,
        book_up: Optional[OrderBook] = None,
        book_down: Optional[OrderBook] = None,
        now: Optional[float] = None,
    ) -> Optional[BoxTrade]:
        """Aktif box'i kapat ve PnL yaz."""
        if not self.has_open_box:
            return None
        assert self.active is not None
        box = self.active
        box.closed = True
        box.close_reason = reason

        if box.both_filled:
            pair_cost = box.up.price + box.down.price
            box.pnl = self.size * (1.0 - pair_cost)  # garanti kilitli kar
            self.stats.completed += 1
        elif box.up.filled or box.down.filled:
            # tek bacak: mevcut bid'e mark-to-bid (konservatif zarar)
            leg = box.up if box.up.filled else box.down
            book = book_up if box.up.filled else book_down
            bid = book.best_bid if (book and book.best_bid is not None) else 0.0
            box.pnl = self.size * (bid - leg.price)  # genelde negatif
            self.stats.stranded += 1
        else:
            box.pnl = 0.0  # hic dolmadi (iptal), maliyet yok

        self.stats.pnls.append(box.pnl)
        self.active = None
        return box


def _fills(book: OrderBook, price: float) -> bool:
    """0.40 BUY resting emri: en iyi ask <= emir fiyati ise dolar."""
    return book.best_ask is not None and book.best_ask <= price + 1e-9


# ----------------------------------------------------------------------------
# Kayittan tekrar oynatma (ndjson replay)
# ----------------------------------------------------------------------------


def replay_books(path: str) -> Iterator[tuple[OrderBook, OrderBook, float]]:
    """ndjson kaydini (up/down defter anlik goruntuleri) tekrar oynatir.

    Satir format: {"ts":..,"up":{"bids":[[p,s]..],"asks":[..]},"down":{..}}
    """
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            up = _book_from_rec("up", rec)
            down = _book_from_rec("down", rec)
            yield up, down, float(rec.get("ts", time.time()))


def _book_from_rec(key: str, rec: dict) -> OrderBook:
    from models import BookLevel

    side = rec.get(key, {})
    bids = [BookLevel(float(p), float(s)) for p, s in side.get("bids", [])]
    asks = [BookLevel(float(p), float(s)) for p, s in side.get("asks", [])]
    return OrderBook(token_id=key, bids=bids, asks=asks, ts=float(rec.get("ts", 0.0)))
