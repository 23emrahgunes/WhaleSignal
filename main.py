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
import os
import time
from collections import deque
from typing import Optional

import aiohttp

from binance_feed import BinanceFeed
from calibration import CalibrationBook, CalSample
from clob_feed import ClobQuoteStore, ClobSupervisor
from config import Settings, get_settings
from direction_model import DirectionModel
from discovery import MarketDiscovery
from features import FeatureEngine
from hub import DataHub
from models import (
    AssetHorizon,
    Decision,
    Prediction,
    Regime,
    AbstainReason,
    all_combos,
)
from quality import check_freshness
from recorder import Recorder
from reference import ReferenceRouter
from regime import classify_regime
from web_dashboard import run_web

# Karar marji: |p_up-0.5| bu esigin altindaysa emin degil -> ABSTAIN
DECISION_MARGIN = 0.05
MODEL_PATH = "models/direction_model.pkl"

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


class _MarketAcc:
    """Bir marketin omru boyunca biriken feature'lar + son karar (resolve'da ogrenme)."""

    __slots__ = ("combo_key", "fvs", "last_pred", "created")

    def __init__(self, combo_key: str) -> None:
        self.combo_key = combo_key
        self.fvs: list = []  # checkpoint'lerdeki FeatureVector'lar (egitim seti)
        self.last_pred: Optional[dict] = None  # son ABSTAIN-olmayan tahmin (kalibrasyon)
        self.created = time.time()


class ShadowEngine:
    """Snapshot dongusu: feature -> regime -> model -> karar; checkpoint kaydi;
    resolve'da model ogrenimi + kalibrasyon. SHADOW: canli emir YOK.

    P2: yon MODELI online logistic (RESMI resolved market'lerden ogrenir). Yeterli
    etiketli market yoksa karar ABSTAIN(INSUFFICIENT_DATA).
    """

    def __init__(
        self,
        cfg: Settings,
        hub: DataHub,
        recorder: Recorder,
        model: DirectionModel,
        calib: CalibrationBook,
    ) -> None:
        self.cfg = cfg
        self.hub = hub
        self.recorder = recorder
        self.model = model
        self.calib = calib
        self.checkpoints = cfg.snapshot_checkpoints()
        self._recorded_markets: set[str] = set()
        self._fired: dict[str, set[int]] = {}
        self._feature_engines: dict[str, FeatureEngine] = {}
        self._combo_market: dict[str, str] = {}  # combo_key -> aktif condition_id
        self._acc: dict[str, _MarketAcc] = {}  # condition_id -> accumulator
        self.latest: dict[str, dict] = {}
        self.events: deque[dict] = deque(maxlen=100)
        self.started_at = time.time()
        self._resolve_count = 0

    def _event(self, kind: str, detail: str) -> None:
        self.events.appendleft({"ts": time.time(), "kind": kind, "detail": detail})

    def _maybe_record_market(self, ref) -> None:  # noqa: ANN001
        if ref.condition_id and ref.condition_id not in self._recorded_markets:
            self.recorder.record_market(ref)
            self._recorded_markets.add(ref.condition_id)
            self._acc[ref.condition_id] = _MarketAcc(ref.combo.key)
            self._event("MARKET", f"{ref.combo.key} {ref.resolution_type.value}")

    def _feature_engine(self, ref) -> FeatureEngine:  # noqa: ANN001
        fe = self._feature_engines.get(ref.combo.key)
        if fe is None:
            fe = FeatureEngine(ref.combo)
            self._feature_engines[ref.combo.key] = fe
        # market degistiyse market-bazli durumu sifirla (PTB slope/CLOB trajectory)
        if self._combo_market.get(ref.combo.key) != ref.condition_id:
            fe.on_market_change()
            self._combo_market[ref.combo.key] = ref.condition_id
        return fe

    def _checkpoint_crossed(self, ref, seconds_remaining: float) -> Optional[int]:  # noqa: ANN001
        fired = self._fired.setdefault(ref.condition_id, set())
        for cp in self.checkpoints:
            if seconds_remaining <= cp and cp not in fired:
                fired.add(cp)
                return cp
        return None

    def _decide(self, ref, snap, q, fv):  # noqa: ANN001
        """quality -> regime -> model. Prediction dondurur."""
        combo = ref.combo
        market_up = snap.up_mid
        if not q.ok:
            return Prediction(
                combo=combo, ts=snap.ts, decision=Decision.ABSTAIN,
                abstain_reason=q.reason, regime=Regime.UNKNOWN,
                reasons=q.notes, market_implied_up=market_up,
            )
        reg = classify_regime(fv)
        if reg.abstain:
            return Prediction(
                combo=combo, ts=snap.ts, decision=Decision.ABSTAIN,
                abstain_reason=reg.abstain_reason, regime=reg.regime,
                predictability=reg.predictability, reasons=reg.reasons,
                market_implied_up=market_up,
            )
        mo = self.model.predict(combo.key, fv)
        if not mo.ready or mo.p_up is None:
            return Prediction(
                combo=combo, ts=snap.ts, decision=Decision.ABSTAIN,
                abstain_reason=AbstainReason.INSUFFICIENT_DATA, regime=reg.regime,
                predictability=reg.predictability,
                reasons=["model ogrenme asamasinda"] + reg.reasons,
                market_implied_up=market_up,
            )
        p_up = mo.p_up
        if p_up > 0.5 + DECISION_MARGIN:
            decision, reason = Decision.UP, AbstainReason.NONE
        elif p_up < 0.5 - DECISION_MARGIN:
            decision, reason = Decision.DOWN, AbstainReason.NONE
        else:
            decision, reason = Decision.ABSTAIN, AbstainReason.LOW_PREDICTABILITY
        why = self._why(fv, reg, mo) if decision != Decision.ABSTAIN else (
            ["marj yetersiz (p_up~0.5)"] + reg.reasons
        )
        return Prediction(
            combo=combo, ts=snap.ts, p_up=p_up, p_down=1.0 - p_up,
            confidence=mo.confidence, predictability=reg.predictability,
            regime=reg.regime, decision=decision, abstain_reason=reason,
            reasons=why, market_implied_up=market_up,
        )

    def _why(self, fv, reg, mo) -> list[str]:  # noqa: ANN001
        out = [f"p_up={mo.p_up:.2f} ({mo.source})", f"rejim={reg.regime.value}"]
        if mo.p_up_no_clob is not None:
            out.append(f"CLOB'suz p_up={mo.p_up_no_clob:.2f}")
        if fv.has_reference:
            out.append(f"PTB {fv.distance_bps:+.1f}bps (z={fv.ptb_z:+.2f})")
        out.append(f"momentum {fv.ret_slow*100:+.3f}% persist={fv.sign_persistence:.2f}")
        out.append(f"flow {fv.flow_mid:+.2f}")
        return out

    def tick(self) -> None:
        active = self.hub.discovery.snapshot_active()
        now = time.time()
        present_keys = set()
        for key, ref in active.items():
            present_keys.add(key)
            self._maybe_record_market(ref)
            snap = self.hub.build_snapshot(ref, now)
            q = check_freshness(snap, self.cfg)

            fv = None
            if q.ok:
                fe = self._feature_engine(ref)
                feed = self.hub.binance.get_feed(ref.combo.binance_symbol)
                prices = list(feed.prices) if feed else []
                trades = list(feed.trades) if feed else []
                book = feed.book if feed else None
                if book is not None:
                    fv = fe.update(
                        prices, trades, book, snap.reference_price,
                        snap.up_mid, snap.down_mid, snap.seconds_remaining, now,
                    )

            if fv is not None:
                pred = self._decide(ref, snap, q, fv)
            else:
                pred = Prediction(
                    combo=ref.combo, ts=now, decision=Decision.ABSTAIN,
                    abstain_reason=(q.reason if not q.ok else AbstainReason.INSUFFICIENT_DATA),
                    regime=Regime.UNKNOWN, reasons=q.notes, market_implied_up=snap.up_mid,
                )

            # checkpoint: dataset kaydi (feature extra) + egitim accumulator
            if q.ok and fv is not None:
                cp = self._checkpoint_crossed(ref, snap.seconds_remaining)
                if cp is not None:
                    snap.extra = fv.to_dict()
                    self.recorder.record_snapshot(ref, snap, cp)
                    acc = self._acc.get(ref.condition_id)
                    if acc is not None:
                        acc.fvs.append(fv)
                    self._event("SNAPSHOT", f"{ref.combo.key} @ t-{cp}s")

            # son ABSTAIN-olmayan tahmini kalibrasyon icin sakla
            if pred.decision != Decision.ABSTAIN:
                acc = self._acc.get(ref.condition_id)
                if acc is not None:
                    acc.last_pred = {
                        "p_up": pred.p_up,
                        "decision_up": pred.decision == Decision.UP,
                        "confidence": pred.confidence,
                        "mkt": pred.market_implied_up,
                    }

            self.latest[key] = self._card(ref, snap, q, pred, fv)

        for key in list(self.latest):
            if key not in present_keys and self.latest[key].get("active"):
                self.latest[key]["active"] = False
        self._prune_acc()

    def on_market_resolved(self, ref) -> None:  # noqa: ANN001
        """discovery callback: RESMI resolve -> etiket + model ogrenimi + kalibrasyon."""
        self.recorder.settle(ref)
        if ref.resolved_outcome is None:
            return
        label_up = 1 if ref.resolved_outcome == Decision.UP else 0
        acc = self._acc.get(ref.condition_id)
        if acc is not None and acc.fvs:
            self.model.learn_with_label(ref.combo.key, acc.fvs, label_up)
        # kalibrasyon ornegi
        lp = acc.last_pred if acc else None
        if lp is not None:
            self.calib.record(ref.combo.key, CalSample(
                decided=True, outcome_up=(label_up == 1), p_up=lp["p_up"],
                decision_up=lp["decision_up"], confidence=lp["confidence"],
                market_implied_up=lp["mkt"],
            ))
        else:
            self.calib.record(ref.combo.key, CalSample(
                decided=False, outcome_up=(label_up == 1)))
        self._resolve_count += 1
        self._event("RESOLVED", f"{ref.combo.key} -> {ref.resolved_outcome.value}")
        # modeli kalici yap
        try:
            os.makedirs(os.path.dirname(MODEL_PATH) or ".", exist_ok=True)
            self.model.save(MODEL_PATH)
        except Exception as exc:  # noqa: BLE001
            log.warning("model kaydedilemedi: %s", exc)
        # accumulator temizle
        self._acc.pop(ref.condition_id, None)

    def _prune_acc(self) -> None:
        if len(self._acc) <= 600:
            return
        # en eski girisleri at (resolve olmayan artiklar)
        for cid in sorted(self._acc, key=lambda c: self._acc[c].created)[:100]:
            self._acc.pop(cid, None)

    def _card(self, ref, snap, q, pred, fv) -> dict:  # noqa: ANN001
        card = {
            "combo": ref.combo.key,
            "active": True,
            "slug": ref.slug,
            "resolution_type": ref.resolution_type.value,
            "resolution_meta_ok": ref.has_resolution_meta,
            "seconds_remaining": round(snap.seconds_remaining, 1),
            "spot_price": snap.spot_price,
            "reference_price": snap.reference_price,
            "distance_bps": round(snap.distance_bps, 2) if snap.distance_bps is not None else None,
            "up_mid": snap.up_mid,
            "down_mid": snap.down_mid,
            "freshness": {
                "ok": q.ok,
                "spot_age_ms": round(snap.spot_age_ms, 0) if snap.spot_age_ms is not None else None,
                "book_age_ms": round(snap.book_age_ms, 0) if snap.book_age_ms is not None else None,
                "notes": q.notes,
            },
            "decision": pred.decision.value,
            "abstain_reason": pred.abstain_reason.value,
            "regime": pred.regime.value,
            "predictability": round(pred.predictability, 3),
            "p_up": round(pred.p_up, 4),
            "confidence": round(pred.confidence, 3),
            "price_edge": round(pred.price_edge, 4) if pred.price_edge is not None else None,
            "why": pred.reasons,
        }
        return card

    def snapshot(self) -> dict:
        combos = build_combos(self.cfg)
        cards = []
        for combo in combos:
            card = self.latest.get(combo.key)
            if card is None:
                card = {
                    "combo": combo.key, "active": False,
                    "decision": Decision.ABSTAIN.value,
                    "abstain_reason": AbstainReason.NO_MARKET.value,
                    "why": ["market kesfedilmedi (YOK)"],
                }
            cards.append(card)
        return {
            "now": time.time(),
            "uptime_sec": round(time.time() - self.started_at, 1),
            "mode": "SHADOW",
            "phase": "P2",
            "cards": cards,
            "recorder": self.recorder.stats(),
            "model": self.model.stats(),
            "calibration": self.calib.summary(),
            "min_markets_for_stats": self.cfg.min_markets_for_stats,
            "events": list(self.events),
            "discovery_last_ts": self.hub.discovery.last_discovery_ts,
            "binance_connected": self.hub.binance.connected,
        }

    async def run(self, stop: asyncio.Event) -> None:
        interval = self.cfg.snapshot_loop_ms / 1000.0
        log.info("ShadowEngine dongusu basladi (SHADOW, P2)")
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
    model = DirectionModel.load(MODEL_PATH) or DirectionModel(cfg.per_combo_model_min_markets)
    calib = CalibrationBook(min_n=cfg.min_markets_for_stats)
    async with aiohttp.ClientSession() as session:
        discovery = MarketDiscovery(cfg, session, combos)
        binance = BinanceFeed(cfg, symbols, session)
        clob_store = ClobQuoteStore()
        reference = ReferenceRouter(cfg)
        hub = DataHub(cfg, discovery, binance, clob_store, reference)
        engine = ShadowEngine(cfg, hub, recorder, model, calib)
        discovery.on_resolved(engine.on_market_resolved)  # settle + ogren + kalibrasyon
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
