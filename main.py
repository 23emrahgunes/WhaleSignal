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
    LabelStatus,
    Prediction,
    Regime,
    AbstainReason,
    all_combos,
)
from quality import assess, check_freshness
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
        # canonical state: hepsi market_id (condition_id) anahtarli
        self._recorded_markets: set[str] = set()
        self._fired: dict[str, set[int]] = {}  # market_id -> yazilmis checkpoint'ler
        self._prev_tte: dict[str, float] = {}  # market_id -> onceki TTE (edge-crossing)
        self._feature_engines: dict[str, FeatureEngine] = {}  # combo.key (asset feed shared)
        self._combo_market: dict[str, str] = {}  # combo_key -> aktif market_id
        self._acc: dict[str, _MarketAcc] = {}  # market_id -> accumulator
        self._token_market: dict[str, str] = {}  # token_id -> market_id (reverse index)
        self.latest: dict[str, dict] = {}
        self.events: deque[dict] = deque(maxlen=100)
        self.started_at = time.time()
        self._resolve_count = 0
        self._data_quality_errors = 0

    def _event(self, kind: str, detail: str) -> None:
        self.events.appendleft({"ts": time.time(), "kind": kind, "detail": detail})

    def _maybe_record_market(self, ref) -> None:  # noqa: ANN001
        mid = ref.market_id
        # token -> market_id reverse index (CLOB dogru instance'a yonlensin)
        if ref.up_token_id:
            self._token_market[ref.up_token_id] = mid
        if ref.down_token_id:
            self._token_market[ref.down_token_id] = mid
        if mid and mid not in self._recorded_markets:
            self.recorder.record_market(ref)
            self._recorded_markets.add(mid)
            self._acc[mid] = _MarketAcc(ref.combo.key)
            self._event(
                "MARKET",
                f"{ref.combo.key} {ref.resolution_type.value} time={ref.time_status.value}",
            )

    def token_market_index(self) -> dict[str, str]:
        return dict(self._token_market)

    def _feature_engine(self, ref) -> FeatureEngine:  # noqa: ANN001
        fe = self._feature_engines.get(ref.combo.key)
        if fe is None:
            fe = FeatureEngine(ref.combo)
            self._feature_engines[ref.combo.key] = fe
        # market degistiyse market-bazli durumu sifirla (PTB slope/CLOB trajectory)
        if self._combo_market.get(ref.combo.key) != ref.market_id:
            fe.on_market_change()
            self._combo_market[ref.combo.key] = ref.market_id
        return fe

    def _checkpoint_crossed(self, ref, tte: float) -> Optional[int]:  # noqa: ANN001
        """EDGE-CROSSING: cp yalniz `prev_tte > cp >= tte` gecisinde tetiklenir.

        Mid-window join'de (prev_tte yok) yuksek cp'ler BACKFILL EDILMEZ; yalniz o
        andan sonraki gecisler yazilir."""
        mid = ref.market_id
        prev = self._prev_tte.get(mid)
        self._prev_tte[mid] = tte
        if prev is None:
            return None  # ilk gozlem: referans al, backfill etme
        fired = self._fired.setdefault(mid, set())
        for cp in self.cfg.checkpoints_for(ref.combo.horizon.value):
            if prev > cp >= tte and cp not in fired:
                fired.add(cp)
                return cp
        return None

    def _decide(self, ref, snap, q, fv):  # noqa: ANN001
        """7-boyut quality -> (prediction_ready ise) regime -> model. Prediction dondurur.

        P1'de model egitilmedigi icin prediction_ready False -> ABSTAIN(MODEL_NOT_TRAINED).
        Diger eksiklerde de q.abstain_reason (UNSAFE_TIME/CLOB_MISSING/PTB_MISSING/...).
        """
        combo = ref.combo
        market_up = snap.up_mid  # gercek up_mid (yoksa None; 0.505 YOK)
        # HEURISTIC predictability (P1'de model degil) — fv varsa hesapla
        reg = classify_regime(fv) if fv is not None else None
        predictability = reg.predictability if reg is not None else 0.0

        if not q.prediction_ready:
            reasons = list(q.notes)
            if reg is not None:
                reasons.append(f"rejim(HEURISTIC)={reg.regime.value} p={predictability:.2f}")
            return Prediction(
                combo=combo, ts=snap.ts, decision=Decision.ABSTAIN,
                abstain_reason=q.abstain_reason, predictability=predictability,
                regime=(reg.regime if reg else Regime.UNKNOWN), reasons=reasons,
                market_implied_up=market_up,
            )
        # buraya gelindiyse: time/market/tokens/clob/reference/clock/model OK
        if reg is not None and reg.abstain:
            return Prediction(
                combo=combo, ts=snap.ts, decision=Decision.ABSTAIN,
                abstain_reason=reg.abstain_reason, regime=reg.regime,
                predictability=predictability, reasons=reg.reasons,
                market_implied_up=market_up,
            )
        mo = self.model.predict(combo.key, fv)
        if not mo.ready or mo.p_up is None:
            return Prediction(
                combo=combo, ts=snap.ts, decision=Decision.ABSTAIN,
                abstain_reason=AbstainReason.MODEL_NOT_TRAINED,
                regime=(reg.regime if reg else Regime.UNKNOWN),
                predictability=predictability, market_implied_up=market_up,
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
            confidence=mo.confidence, predictability=predictability,
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
        clock_synced = self.hub.binance.clock_synced
        present_keys = set()
        for key, ref in active.items():
            present_keys.add(key)
            self._maybe_record_market(ref)
            snap = self.hub.build_snapshot(ref, now)
            model_ready = self.model.ready_for(ref.combo.key)
            q = assess(ref, snap, self.cfg, now, clock_synced, model_ready)
            snap.quality_status = "OK" if q.prediction_ready else q.abstain_reason.value
            snap.prediction_ready = q.prediction_ready
            if q.abstain_reason in (
                AbstainReason.UNSAFE, AbstainReason.UNSAFE_TIME_METADATA
            ):
                self._data_quality_errors += 1

            # feature'lar yalniz snapshot_recordable (time+market+tokens+feed OK) iken
            fv = None
            if q.snapshot_recordable:
                fe = self._feature_engine(ref)
                feed = self.hub.binance.get_feed(ref.combo.binance_symbol)
                book = feed.book if feed else None
                if book is not None:
                    fv = fe.update(
                        list(feed.prices), list(feed.trades), book, snap.reference_price,
                        snap.up_mid, snap.down_mid, snap.tte_sec or snap.seconds_remaining, now,
                    )

            pred = self._decide(ref, snap, q, fv)

            # checkpoint edge-crossing: ham row (feature extra) + egitim accumulator.
            # UNSAFE_TIME ise snapshot_recordable False -> hic yazilmaz.
            if q.snapshot_recordable:
                cp = self._checkpoint_crossed(ref, snap.tte_sec or snap.seconds_remaining)
                if cp is not None:
                    if fv is not None:
                        snap.extra = fv.to_dict()
                    self.recorder.record_snapshot(ref, snap, cp)
                    acc = self._acc.get(ref.market_id)
                    if acc is not None and fv is not None:
                        acc.fvs.append(fv)
                    self._event("SNAPSHOT", f"{ref.combo.key} @ t-{cp}s q={snap.quality_status}")

            if pred.decision != Decision.ABSTAIN:
                acc = self._acc.get(ref.market_id)
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
        """discovery callback: EXPLICIT official resolve -> computed audit + etiket +
        (etiket MISMATCH degilse) model ogrenimi + kalibrasyon."""
        official = ref.official_result or ref.resolved_outcome
        if official is None:
            return
        # computed_result (yerel audit): son snapshot spot vs reference_open
        ref.computed_result = self._compute_result(ref)
        self.recorder.settle(ref)
        label_up = 1 if official == Decision.UP else 0
        acc = self._acc.get(ref.market_id)
        # MISMATCH -> training-disi (model ogrenmez)
        trainable = ref.label_status != LabelStatus.MISMATCH
        if trainable and acc is not None and acc.fvs:
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
        self._event(
            "RESOLVED",
            f"{ref.combo.key} official={official.value} label={ref.label_status.value}",
        )
        # modeli kalici yap
        try:
            os.makedirs(os.path.dirname(MODEL_PATH) or ".", exist_ok=True)
            self.model.save(MODEL_PATH)
        except Exception as exc:  # noqa: BLE001
            log.warning("model kaydedilemedi: %s", exc)
        # accumulator temizle
        self._acc.pop(ref.market_id, None)

    def _compute_result(self, ref) -> Optional[Decision]:  # noqa: ANN001
        """Yerel audit: kapanis spot vs reference_open (official DEGIL, yalniz kiyas)."""
        if ref.reference_open is None:
            return None
        feed = self.hub.binance.get_feed(ref.combo.binance_symbol)
        spot = None
        if feed is not None:
            spot, _ = feed.spot_price()
        if spot is None:
            return None
        return Decision.UP if spot >= ref.reference_open else Decision.DOWN

    def _prune_acc(self) -> None:
        if len(self._acc) <= 600:
            return
        for cid in sorted(self._acc, key=lambda c: self._acc[c].created)[:100]:
            self._acc.pop(cid, None)

    def _card(self, ref, snap, q, pred, fv) -> dict:  # noqa: ANN001
        def r(x, n=3):
            return round(x, n) if x is not None else None
        return {
            "combo": ref.combo.key,
            "active": True,
            # --- debug kimligi (BTC5m != BTC15m gozle dogrula) ---
            "market_id": (ref.market_id or "")[-8:],
            "slug": ref.slug,
            "condition_id": (ref.condition_id or "")[-8:],
            "up_token": (ref.up_token_id or "")[-8:],
            "down_token": (ref.down_token_id or "")[-8:],
            # --- zaman ---
            "tte_sec": r(snap.tte_sec, 1),
            "time_status": ref.time_status.value,
            # --- reference / PTB ---
            "resolution_type": ref.resolution_type.value,
            "resolution_meta_ok": ref.has_resolution_meta,
            "reference_open": r(ref.reference_open, 2),
            "spot_price": r(snap.spot_price, 2),
            "distance_bps": r(snap.distance_bps, 2),
            # --- CLOB (up/down bid/ask/mid; 0.505 YOK) ---
            "up_bid": r(snap.up_bid), "up_ask": r(snap.up_ask), "up_mid": r(snap.up_mid),
            "down_bid": r(snap.down_bid), "down_ask": r(snap.down_ask), "down_mid": r(snap.down_mid),
            "clob_age_ms": r(snap.clob_age_ms, 0),
            # --- ayrisik yaslar ---
            "transport_age_ms": r(snap.transport_age_ms, 0),
            "source_age_ms": r(snap.source_age_ms, 0),
            "book_age_ms": r(snap.book_age_ms, 0),
            # --- 7 boyut quality + prediction_ready ---
            "quality": q.dims(),
            "prediction_ready": q.prediction_ready,
            "quality_notes": q.notes,
            # --- karar (P1: heuristic/model_not_trained) ---
            "decision": pred.decision.value,
            "abstain_reason": pred.abstain_reason.value,
            "regime": pred.regime.value,
            "predictability_heuristic": r(pred.predictability, 3),
            "p_up": r(pred.p_up, 4),
            "confidence": r(pred.confidence, 3),
            "why": pred.reasons,
        }

    def snapshot(self) -> dict:
        combos = build_combos(self.cfg)
        status = self.hub.discovery.snapshot_status()
        cards = []
        up_mids: list[float] = []
        clob_healthy = ptb_healthy = 0
        for combo in combos:
            card = self.latest.get(combo.key)
            if card is None or not card.get("active"):
                card = {
                    "combo": combo.key, "active": False,
                    "discovery_status": status.get(combo.key, "NOT_FOUND"),
                    "decision": Decision.ABSTAIN.value,
                    "abstain_reason": AbstainReason.NO_MARKET.value,
                    "why": [f"discovery={status.get(combo.key, 'NOT_FOUND')}"],
                }
            else:
                card["discovery_status"] = status.get(combo.key, "FOUND")
                if card.get("up_mid") is not None:
                    up_mids.append(card["up_mid"])
                    clob_healthy += 1
                if card.get("reference_open") is not None:
                    ptb_healthy += 1
            cards.append(card)

        # SUSPICIOUS_IDENTICAL_QUOTES: >=3 aktif markette ayni up_mid
        suspicious = False
        if len(up_mids) >= 3:
            from collections import Counter
            most = Counter(round(m, 3) for m in up_mids).most_common(1)[0]
            suspicious = most[1] >= 3

        rec = self.recorder.stats()
        return {
            "now": time.time(),
            "uptime_sec": round(time.time() - self.started_at, 1),
            "mode": "SHADOW",
            "phase": "P1-hardened",
            "cards": cards,
            "recorder": rec,
            "model": self.model.stats(),
            "calibration": self.calib.summary(),
            "min_markets_for_stats": self.cfg.min_markets_for_stats,
            "events": list(self.events),
            "discovery_status": status,
            "discovery_last_ts": self.hub.discovery.last_discovery_ts,
            "binance_connected": self.hub.binance.connected,
            "clock_synced": self.hub.binance.clock_synced,
            "clock_offset_ms": self.hub.binance.clock_offset_ms,
            # --- footer metrikleri ---
            "footer": {
                "markets_active": sum(1 for c in cards if c.get("active")),
                "markets_discovered_total": rec["markets"],
                "snapshots_total": rec["snapshots"],
                "snapshots_labeled": rec["labeled_snapshots"],
                "resolved_total": rec["resolved_markets"],
                "label_mismatch": rec["label_mismatch"],
                "clob_states_healthy": clob_healthy,
                "ptb_states_healthy": ptb_healthy,
                "discovery_errors": self.hub.discovery.discovery_errors,
                "data_quality_errors": self._data_quality_errors,
                "suspicious_identical_quotes": suspicious,
            },
        }

    async def run(self, stop: asyncio.Event) -> None:
        interval = self.cfg.snapshot_loop_ms / 1000.0
        log.info("ShadowEngine dongusu basladi (SHADOW, P1-hardened)")
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
    last_clock = 0.0
    while not stop.is_set():
        try:
            await hub.refresh_references(session)
            now = time.time()
            if now - last_clock >= 60.0:  # clock offset periyodik
                await hub.binance.refresh_clock()
                last_clock = now
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

        # P1 backfill: resolved market + resolution + label pipeline testi (snapshot URETMEZ)
        if cfg.backfill_resolved_markets > 0:
            try:
                n = await discovery.backfill_resolved(
                    cfg.backfill_resolved_markets, recorder.backfill_market
                )
                log.info("backfill: %d resolved market yuklendi (source=backfill)", n)
            except Exception as exc:  # noqa: BLE001
                log.warning("backfill hatasi: %s", exc)

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
