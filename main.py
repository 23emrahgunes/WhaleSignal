"""Orkestrator — tum asenkron gorevleri asyncio.TaskGroup ile birlikte yurutur.

Akis (P1, SHADOW):
  discovery (Gamma active-event + slug fast path) -> aktif marketler + resmi resolve
  binance_feed (trade + diff-depth local book) -> spot/OFI hammadde
  clob_feed (aktif UP/DOWN token) -> teyit kotalari
  reference (horizon adaptoru) -> PTB
  ShadowEngine (snapshot dongusu) -> quality kapisi + checkpoint kaydi + dashboard durumu
  recorder -> SQLite dataset + resmi etiket (on_resolved)

Canli emir / imza / execution YOK.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from typing import Optional

import aiohttp

from binance_feed import BinanceFeed
from clob_feed import ClobQuoteStore, ClobSupervisor
from config import Settings, get_settings
from discovery import MarketDiscovery
from hub import DataHub
from models import (
    Asset,
    AssetHorizon,
    Decision,
    Horizon,
    Prediction,
    Regime,
    AbstainReason,
    all_combos,
)
from quality import check_freshness
from recorder import Recorder
from reference import ReferenceRouter
from web_dashboard import run_web

log = logging.getLogger("direction_engine.main")


def build_combos(settings: Settings) -> list[AssetHorizon]:
    """Ayarlardan 12 (veya alt-kume) combo listesi uret."""
    assets = settings.assets()
    horizons = settings.horizons()
    out: list[AssetHorizon] = []
    for combo in all_combos():
        if combo.asset.value in assets and combo.horizon.value in horizons:
            out.append(combo)
    return out


class ShadowEngine:
    """Snapshot dongusu: quality + checkpoint kaydi + dashboard durumu.

    P1'de yon MODELI YOK -> karar daima ABSTAIN (STALE_DATA veya INSUFFICIENT_DATA).
    P2'de regime + direction_model buraya baglanir.
    """

    def __init__(self, cfg: Settings, hub: DataHub, recorder: Recorder) -> None:
        self.cfg = cfg
        self.hub = hub
        self.recorder = recorder
        self.checkpoints = cfg.snapshot_checkpoints()
        self._recorded_markets: set[str] = set()
        self._fired: dict[str, set[int]] = {}  # condition_id -> fired checkpoints
        self.latest: dict[str, dict] = {}  # combo_key -> dashboard karti
        self.events: deque[dict] = deque(maxlen=80)
        self.started_at = time.time()

    def _event(self, kind: str, detail: str) -> None:
        self.events.appendleft({"ts": time.time(), "kind": kind, "detail": detail})

    def _maybe_record_market(self, ref) -> None:  # noqa: ANN001
        if ref.condition_id and ref.condition_id not in self._recorded_markets:
            self.recorder.record_market(ref)
            self._recorded_markets.add(ref.condition_id)
            self._event("MARKET", f"{ref.combo.key} {ref.resolution_type.value}")

    def _checkpoint_crossed(self, ref, seconds_remaining: float) -> Optional[int]:  # noqa: ANN001
        fired = self._fired.setdefault(ref.condition_id, set())
        for cp in self.checkpoints:  # desc sirali
            if seconds_remaining <= cp and cp not in fired:
                fired.add(cp)
                return cp
        return None

    def tick(self) -> None:
        active = self.hub.discovery.snapshot_active()
        now = time.time()
        present_keys = set()
        for key, ref in active.items():
            present_keys.add(key)
            self._maybe_record_market(ref)
            snap = self.hub.build_snapshot(ref, now)
            q = check_freshness(snap, self.cfg)

            # P1: model yok -> ABSTAIN. Stale ise sebep STALE_DATA.
            if not q.ok:
                reason = q.reason
            else:
                reason = AbstainReason.INSUFFICIENT_DATA
            pred = Prediction(
                combo=ref.combo,
                ts=now,
                decision=Decision.ABSTAIN,
                abstain_reason=reason,
                regime=Regime.UNKNOWN,
                reasons=q.notes,
                market_implied_up=snap.up_mid,
            )

            # checkpoint'te kayit (yalniz veri tazeyken; bayat satir dataset'i kirletmesin)
            if q.ok:
                cp = self._checkpoint_crossed(ref, snap.seconds_remaining)
                if cp is not None:
                    self.recorder.record_snapshot(ref, snap, cp)
                    self._event("SNAPSHOT", f"{ref.combo.key} @ t-{cp}s")

            self.latest[key] = self._card(ref, snap, q, pred)

        # artik aktif olmayan combo kartlarini "YOK" isaretle
        for key in list(self.latest):
            if key not in present_keys and self.latest[key].get("active"):
                self.latest[key]["active"] = False

    def _card(self, ref, snap, q, pred) -> dict:  # noqa: ANN001
        return {
            "combo": ref.combo.key,
            "active": True,
            "slug": ref.slug,
            "resolution_type": ref.resolution_type.value,
            "resolution_meta_ok": ref.has_resolution_meta,
            "seconds_remaining": round(snap.seconds_remaining, 1),
            "spot_price": snap.spot_price,
            "reference_price": snap.reference_price,
            "distance_usd": round(snap.distance_usd, 4) if snap.distance_usd is not None else None,
            "distance_bps": round(snap.distance_bps, 2) if snap.distance_bps is not None else None,
            "up_mid": snap.up_mid,
            "down_mid": snap.down_mid,
            "clob_spread": snap.clob_spread,
            "freshness": {
                "ok": q.ok,
                "spot_age_ms": round(snap.spot_age_ms, 0) if snap.spot_age_ms is not None else None,
                "book_age_ms": round(snap.book_age_ms, 0) if snap.book_age_ms is not None else None,
                "clob_age_ms": round(snap.clob_age_ms, 0) if snap.clob_age_ms is not None else None,
                "notes": q.notes,
            },
            "decision": pred.decision.value,
            "abstain_reason": pred.abstain_reason.value,
            "regime": pred.regime.value,
            "why": pred.reasons,
        }

    def snapshot(self) -> dict:
        """web dashboard icin tam durum (JSON-guvenli)."""
        combos = build_combos(self.cfg)
        cards = []
        for combo in combos:
            card = self.latest.get(combo.key)
            if card is None:
                card = {
                    "combo": combo.key,
                    "active": False,
                    "decision": Decision.ABSTAIN.value,
                    "abstain_reason": AbstainReason.NO_MARKET.value,
                    "why": ["market kesfedilmedi (YOK)"],
                }
            cards.append(card)
        return {
            "now": time.time(),
            "uptime_sec": round(time.time() - self.started_at, 1),
            "mode": "SHADOW",
            "phase": "P1",
            "cards": cards,
            "recorder": self.recorder.stats(),
            "min_markets_for_stats": self.cfg.min_markets_for_stats,
            "events": list(self.events),
            "discovery_last_ts": self.hub.discovery.last_discovery_ts,
            "binance_connected": self.hub.binance.connected,
        }

    async def run(self, stop: asyncio.Event) -> None:
        interval = self.cfg.snapshot_loop_ms / 1000.0
        log.info("ShadowEngine dongusu basladi (SHADOW, P1)")
        while not stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                log.exception("tick hatasi: %s", exc)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)
        log.info("ShadowEngine durdu | %s", self.recorder.stats())


async def _reference_refresher(
    hub: DataHub, session: aiohttp.ClientSession, stop: asyncio.Event, interval: float = 2.0
) -> None:
    while not stop.is_set():
        try:
            await hub.refresh_references(session)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("reference refresher hatasi: %s", exc)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    import signal

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass  # Windows: KeyboardInterrupt'a guveniriz


async def run() -> None:
    cfg = get_settings()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    combos = build_combos(cfg)
    symbols = sorted({c.binance_symbol for c in combos})
    log.info("kapsam: %d combo, semboller=%s (SHADOW)", len(combos), symbols)

    stop = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop)

    recorder = Recorder(cfg.db_path)
    async with aiohttp.ClientSession() as session:
        discovery = MarketDiscovery(cfg, session, combos)
        binance = BinanceFeed(cfg, symbols, session)
        clob_store = ClobQuoteStore()
        reference = ReferenceRouter(cfg)
        hub = DataHub(cfg, discovery, binance, clob_store, reference)
        engine = ShadowEngine(cfg, hub, recorder)
        discovery.on_resolved(recorder.settle)
        clob = ClobSupervisor(cfg, clob_store, session, hub.active_token_ids)

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(discovery.run(stop), name="discovery")
                tg.create_task(binance.run(stop), name="binance")
                tg.create_task(clob.run(stop), name="clob")
                tg.create_task(_reference_refresher(hub, session, stop), name="reference")
                tg.create_task(engine.run(stop), name="engine")
                if cfg.web_enabled:
                    tg.create_task(run_web(engine, cfg, stop), name="web")
        except* Exception as eg:  # noqa: B001
            for exc in eg.exceptions:
                log.error("gorev hatasi: %s", exc)
        finally:
            recorder.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("kullanici durdurdu (Ctrl+C)")


if __name__ == "__main__":
    main()
