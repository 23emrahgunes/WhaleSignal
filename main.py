"""Orkestrator: tum asenkron gorevleri asyncio.TaskGroup ile birlikte yurutur.

Akis: DataHub kaynaklari (Gamma/Deribit/Binance/CLOB) paralel beslenir; strateji
dongusu her tick birlesik durumu okuyup giris/koruma kararini uretir ve
`ClobExecutor` (SIM/DRY/LIVE) + `Simulator` uzerinden isler. Temiz kapanis:
SIGINT/SIGTERM -> acik emirler iptal edilir.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from typing import Optional

import aiohttp

from analytics_engine import build_analytics
from clob_executor import ClobExecutor
from config import Settings, get_settings
from data_ingestion import (
    BinanceKlineStream,
    DataHub,
    DeribitVolatilityPoller,
    GammaMetadataPoller,
    PolymarketOrderbookStream,
)
from execution_strategy import (
    AdverseSelectionGuard,
    EntryDecision,
    GuardAction,
    should_enter,
)
from models import ExecMode, MarketState, Outcome, Side
from simulator_backtester import Simulator
from web_dashboard import run_web

log = logging.getLogger("dual_arbitraj.main")


class StrategyRunner:
    """Giris/koruma kararlarini surer; executor + simulator'u koordine eder."""

    def __init__(self, cfg: Settings, hub: DataHub, executor: ClobExecutor) -> None:
        self.cfg = cfg
        self.hub = hub
        self.executor = executor
        self.sim = Simulator(entry_price=cfg.entry_price, size=cfg.order_size)
        self.guard = AdverseSelectionGuard(cfg.single_leg_timeout_sec)
        self._order_ids: dict[Outcome, Optional[str]] = {Outcome.UP: None, Outcome.DOWN: None}
        self._last_skip_log = 0.0
        self.events: deque[dict] = deque(maxlen=60)  # dashboard olay gecmisi
        self.started_at = time.time()
        self._market_key: Optional[str] = None  # aktif market kimligi (donus tespiti)
        self._entered_market: Optional[str] = None  # bu markete girildi mi (tek giris/market)

    def _entry_decision(self, state: MarketState) -> EntryDecision:
        """should_enter + tek-giris/market guard. Ayni markete ikinci kutu ACMAZ."""
        dec = should_enter(state, self.cfg)
        if state.meta is not None and self._entered_market == state.meta.up_token_id:
            reasons = list(dec.reasons)
            reasons.append("ZATEN_GIRILDI")
            return EntryDecision(allowed=False, reasons=reasons)
        return dec

    def _event(self, kind: str, detail: str, pnl: float = 0.0) -> None:
        self.events.appendleft(
            {"ts": time.time(), "kind": kind, "detail": detail, "pnl": round(pnl, 3)}
        )

    def _build_state(self) -> MarketState:
        meta, book_up, book_down, candles, iv = self.hub.snapshot()
        analytics = build_analytics(
            book_up,
            candles,
            iv,
            saturation_eps=self.cfg.saturation_eps,
        )
        return MarketState(
            meta=meta,
            book_up=book_up,
            book_down=book_down,
            analytics=analytics,
            now=time.time(),
        )

    def _open_box(self, state: MarketState) -> None:
        assert state.meta is not None
        self.guard.reset()
        self.sim.open_box(state.now)
        for outcome, token in (
            (Outcome.UP, state.meta.up_token_id),
            (Outcome.DOWN, state.meta.down_token_id),
        ):
            ack = self.executor.place(token, Side.BUY, self.cfg.order_size, self.cfg.entry_price)
            self._order_ids[outcome] = ack.get("orderId")
        log.info(
            "BOX ACILDI mode=%s up=%s down=%s @%.2f x%.0f",
            self.executor.mode.value,
            state.meta.up_token_id[:8],
            state.meta.down_token_id[:8],
            self.cfg.entry_price,
            self.cfg.order_size,
        )
        self._entered_market = state.meta.up_token_id  # tek giris/market: bir daha acma
        self._event("BOX_ACILDI", f"{self.cfg.order_size:.0f} x UP+DOWN @ {self.cfg.entry_price}")

    def _cancel_leg(self, outcome: Outcome) -> None:
        self.executor.cancel(self._order_ids.get(outcome))
        self._order_ids[outcome] = None

    def _close(self, reason: str, state: MarketState) -> None:
        box = self.sim.close_box(reason, state.book_up, state.book_down, state.now)
        for oc in (Outcome.UP, Outcome.DOWN):
            self._order_ids[oc] = None
        self.guard.reset()
        if box is not None:
            log.info("BOX KAPANDI %s pnl=%.3f | %s", reason, box.pnl, self.sim.stats.as_dict())
            self._event("BOX_KAPANDI", reason, box.pnl)

    def tick(self, state: MarketState) -> None:
        # 0) Market degisti mi (5dk donus) -> acik box'i kapat, yeni markete gec
        if state.meta is not None:
            key = state.meta.up_token_id
            if self._market_key is not None and key != self._market_key and self.sim.has_open_box:
                self._close("MARKET_DEGISTI", state)
            self._market_key = key

        # 1) Vade bitti mi -> acik box'i duzlestir
        if state.meta is not None and state.meta.remaining_sec(state.now) <= 0:
            if self.sim.has_open_box:
                self._close("VADE_SONU", state)
            return

        # 2) Aktif box: dolumlari isle + koruma
        if self.sim.has_open_box:
            for filled in self.sim.on_tick(state.book_up, state.book_down, state.now):
                self.guard.record_fill(filled, state.now)
                self._order_ids[filled] = None  # dolan bacagin emri kapandi
            if self.sim.active is not None and self.sim.active.both_filled:
                self._close("KILITLENDI", state)
                return
            res = self.guard.check(state.now)
            if res.action == GuardAction.CANCEL_OPEN and res.missing_side is not None:
                self._cancel_leg(res.missing_side)
                log.warning(
                    "ADVERSE-SELECTION: karsi bacak (%s) %.1fs icinde dolmadi -> acik emir iptal",
                    res.missing_side.value,
                    res.elapsed,
                )
                self._close("ADVERSE_SELECTION", state)
            return

        # 3) Box yok: giris karari (ayni markete ikinci kez girmez)
        decision = self._entry_decision(state)
        if decision.allowed:
            self._open_box(state)
        elif state.now - self._last_skip_log > 15:
            self._last_skip_log = state.now
            log.info("giris yok: %s", ", ".join(decision.reasons) or "-")

    def snapshot(self) -> dict:
        """Web dashboard icin tam anlik durum (JSON-guvenli)."""
        st = self._build_state()
        a = st.analytics
        meta = st.meta
        dec = self._entry_decision(st)
        box = self.sim.active
        market = None
        if meta is not None:
            market = {
                "question": meta.question,
                "condition_id": meta.condition_id,
                "up_token": meta.up_token_id,
                "down_token": meta.down_token_id,
                "remaining_sec": round(meta.remaining_sec(st.now), 1),
                "duration_sec": round(meta.duration_sec, 1),
            }
        box_info = None
        if box is not None:
            box_info = {
                "up_filled": box.up.filled,
                "down_filled": box.down.filled,
                "opened_ts": box.opened_ts,
                "guard": self.guard.status.value,
            }
        return {
            "mode": self.executor.mode.value,
            "now": st.now,
            "uptime_sec": round(st.now - self.started_at, 1),
            "market": market,
            "analytics": {
                "ready": a.ready,
                "obi": round(a.obi, 4),
                "atr_pct": round(a.atr_pct, 5),
                "adx": round(a.adx, 2),
                "price_velocity": round(a.price_velocity, 4),
                "saturation": a.saturation,
                "bb_squeeze": a.bb_squeeze,
                "implied_vol": round(a.implied_vol, 2),
                "up_mid": st.book_up.midpoint if st.book_up else None,
                "down_mid": st.book_down.midpoint if st.book_down else None,
            },
            "thresholds": {
                "obi_max": self.cfg.obi_max,
                "atr_max_pct": self.cfg.atr_max_pct,
                "adx_max": self.cfg.adx_max,
                "time_decay_pct": self.cfg.time_decay_pct,
            },
            "entry": {"allowed": dec.allowed, "reasons": dec.reasons},
            "box": box_info,
            "stats": self.sim.stats.as_dict(),
            "events": list(self.events),
            "connection": {
                "book_up": st.book_up is not None,
                "book_down": st.book_down is not None,
                "candles": len(self.hub.candles),
                "updated_at": self.hub.updated_at,
                "stale_sec": round(st.now - self.hub.updated_at, 1) if self.hub.updated_at else None,
            },
        }

    async def run(self, stop: asyncio.Event, interval: float = 1.0) -> None:
        log.info("strateji dongusu basladi (mode=%s)", self.executor.mode.value)
        while not stop.is_set():
            try:
                self.tick(self._build_state())
            except Exception as exc:  # noqa: BLE001
                log.exception("strateji tick hatasi: %s", exc)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)
        # kapanista acik emirleri iptal et
        for oc in (Outcome.UP, Outcome.DOWN):
            self._cancel_leg(oc)
        log.info("strateji durdu | final: %s", self.sim.stats.as_dict())


async def _book_supervisor(
    cfg: Settings, hub: DataHub, session: aiohttp.ClientSession, stop: asyncio.Event
) -> None:
    """Meta token id'leri degistikce (5dk market donusu) CLOB book akisini yeniden
    baslatir. Her yeni markette yeni asset_ids'e abone olunur."""
    current_ids: Optional[list[str]] = None
    child_stop: Optional[asyncio.Event] = None
    child_task: Optional[asyncio.Task] = None
    while not stop.is_set():
        meta = hub.meta
        ids = [meta.up_token_id, meta.down_token_id] if meta else None
        if ids and ids != current_ids:
            if child_stop is not None:
                child_stop.set()
            if child_task is not None:
                with contextlib.suppress(Exception):
                    await child_task
            current_ids = ids
            child_stop = asyncio.Event()
            stream = PolymarketOrderbookStream(cfg, hub, ids, session)
            child_task = asyncio.create_task(stream.run(child_stop))
            log.info("CLOB order book akisi (yeni market): %s", [a[:10] for a in ids])
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=2.0)
    if child_stop is not None:
        child_stop.set()
    if child_task is not None:
        with contextlib.suppress(Exception):
            await child_task


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    import signal

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            # Windows: add_signal_handler desteklenmez; KeyboardInterrupt'a guveniriz.
            pass


async def run() -> None:
    cfg = get_settings()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    ok, why = cfg.live_ready()
    if not ok:
        raise SystemExit(f"LIVE modu icin konfig eksik: {why}")
    if cfg.exec_mode == ExecMode.LIVE:
        log.warning("!!! EXEC_MODE=LIVE — GERCEK EMIR GONDERILECEK !!!")

    stop = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop)

    async with aiohttp.ClientSession() as session:
        hub = DataHub()
        executor = ClobExecutor(cfg)
        runner = StrategyRunner(cfg, hub, executor)
        gamma = GammaMetadataPoller(cfg, hub, session)
        deribit = DeribitVolatilityPoller(cfg, hub, session)
        binance = BinanceKlineStream(cfg, hub, session)
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(gamma.run(stop), name="gamma")
                tg.create_task(deribit.run(stop), name="deribit")
                tg.create_task(binance.run(stop), name="binance")
                tg.create_task(_book_supervisor(cfg, hub, session, stop), name="book")
                tg.create_task(runner.run(stop), name="strategy")
                if cfg.web_enabled:
                    tg.create_task(run_web(runner, cfg, stop), name="web")
        except* Exception as eg:  # noqa: B001
            for exc in eg.exceptions:
                log.error("gorev hatasi: %s", exc)


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("kullanici durdurdu (Ctrl+C)")


if __name__ == "__main__":
    main()
