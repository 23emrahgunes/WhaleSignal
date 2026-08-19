"""Direction Engine vNext orchestrator — P2.5 SHADOW.

Pipeline:
  discovery -> official market identity/time/settlement
  Binance direct trade + diff-depth -> feature mark history, flow and local book
  Chainlink/Binance reference -> official PTB/current reference
  Polymarket CLOB -> UP/DOWN confirmation
  P2.1 features -> P2.2 predictability/regime -> P2.3 B1/B2 model
  -> P2.4 calibration/threshold -> P2.5 shadow forecast + SQLite audit

There is deliberately no private key, signer, order submission or live execution.
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
from calibration import CalibrationBook
from chainlink_feed import ChainlinkFeed
from clob_feed import ClobQuoteStore, ClobSupervisor
from config import Settings, get_settings
from direction_model import MODEL_VERSION, DirectionModel
from discovery import MarketDiscovery
from features import FeatureEngine
from forecasting import ForecastEnvelope, ShadowForecaster
from hub import DataHub
from models import (
    AbstainReason,
    AssetHorizon,
    Decision,
    Horizon,
    QStatus,
    all_combos,
)
from quality import assess
from recorder import Recorder
from reference import ReferenceRouter
from shadow_learning import LearningUpdate, apply_pending_updates
from web_dashboard import run_web

log = logging.getLogger("direction_engine.main")


def decide_1h_from_klines(rows, ws_ms: int, now_ms: float):  # noqa: ANN001
    """Return the finalized canonical 1h candle result, never a poll-spot guess."""
    if not isinstance(rows, list):
        return None, None
    for row in rows:
        try:
            open_time, open_px = int(row[0]), float(row[1])
            close_px, close_time = float(row[4]), int(row[6])
        except (IndexError, TypeError, ValueError):
            continue
        if open_time != ws_ms:
            continue
        if close_time > now_ms:
            return None, None
        return (
            Decision.UP if close_px >= open_px else Decision.DOWN,
            "BINANCE_FINALIZED_1H_CANDLE",
        )
    return None, None


def build_combos(settings: Settings) -> list[AssetHorizon]:
    assets = settings.assets()
    horizons = settings.horizons()
    return [
        combo for combo in all_combos()
        if combo.asset.value in assets and combo.horizon.value in horizons
    ]


def _data_ready(report) -> bool:  # noqa: ANN001
    """Data readiness is independent of model/calibration readiness."""
    return (
        report.snapshot_recordable
        and report.clob == QStatus.OK
        and report.reference == QStatus.OK
        and report.clock == QStatus.OK
    )


class ShadowEngine:
    def __init__(
        self,
        cfg: Settings,
        hub: DataHub,
        recorder: Recorder,
        model: DirectionModel,
        calibration: CalibrationBook,
    ) -> None:
        self.cfg = cfg
        self.hub = hub
        self.recorder = recorder
        self.model = model
        self.calibration = calibration
        self.forecaster = ShadowForecaster(
            model,
            calibration,
            inference_enabled=cfg.model_inference_active,
        )
        self._recorded_markets: set[str] = set()
        self._fired: dict[str, set[int]] = {}
        self._prev_tte: dict[str, float] = {}
        self._feature_engines: dict[str, FeatureEngine] = {}
        self._combo_market: dict[str, str] = {}
        self._token_market: dict[str, str] = {}
        self.latest: dict[str, dict] = {}
        self.events: deque[dict] = deque(maxlen=150)
        self.started_at = time.time()
        self._resolve_count = 0
        self._data_quality_errors = 0
        self._forecast_writes = 0
        self._snapshot_writes = 0
        self._model_learn_calls = 0
        self._model_save_calls = 0
        self._calibration_writes = 0
        self._clob = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_learning_update = LearningUpdate()

    def attach_clob(self, clob) -> None:  # noqa: ANN001
        self._clob = clob

    def attach_session(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    def _event(self, kind: str, detail: str) -> None:
        self.events.appendleft({"ts": time.time(), "kind": kind, "detail": detail})

    def _maybe_record_market(self, ref) -> None:  # noqa: ANN001
        market_id = ref.market_id
        if ref.up_token_id:
            self._token_market[ref.up_token_id] = market_id
        if ref.down_token_id:
            self._token_market[ref.down_token_id] = market_id
        if market_id and market_id not in self._recorded_markets:
            self.recorder.record_market(ref)
            self._recorded_markets.add(market_id)
            self._event(
                "MARKET",
                f"{ref.combo.key} {ref.resolution_type.value} time={ref.time_status.value}",
            )

    def token_market_index(self) -> dict[str, str]:
        return dict(self._token_market)

    def _feature_engine(self, ref) -> FeatureEngine:  # noqa: ANN001
        engine = self._feature_engines.get(ref.combo.key)
        if engine is None:
            engine = FeatureEngine(ref.combo)
            self._feature_engines[ref.combo.key] = engine
        if self._combo_market.get(ref.combo.key) != ref.market_id:
            engine.on_market_change()
            self._combo_market[ref.combo.key] = ref.market_id
        return engine

    def _checkpoint_crossed(self, ref, tte: float) -> Optional[int]:  # noqa: ANN001
        market_id = ref.market_id
        previous = self._prev_tte.get(market_id)
        self._prev_tte[market_id] = tte
        if previous is None:
            return None
        fired = self._fired.setdefault(market_id, set())
        for checkpoint in self.cfg.checkpoints_for(ref.combo.horizon.value):
            if previous > checkpoint >= tte and checkpoint not in fired:
                fired.add(checkpoint)
                return checkpoint
        return None

    def _feature_vector(self, ref, snap, now: float):  # noqa: ANN001
        feed = self.hub.binance.get_feed(ref.combo.binance_symbol)
        if feed is None or not feed.book.synced:
            return None
        engine = self._feature_engine(ref)
        prices = feed.feature_series() if hasattr(feed, "feature_series") else list(feed.prices)
        return engine.update(
            prices,
            list(feed.trades),
            feed.book,
            snap.reference_price,
            snap.up_mid,
            snap.down_mid,
            snap.tte_sec if snap.tte_sec is not None else snap.seconds_remaining,
            now,
            up_bid=snap.up_bid,
            up_ask=snap.up_ask,
            down_bid=snap.down_bid,
            down_ask=snap.down_ask,
        )

    def tick(self) -> None:
        active = self.hub.discovery.snapshot_active()
        now = time.time()
        clock_synced = self.hub.binance.clock_synced
        present_keys: set[str] = set()

        for key, ref in active.items():
            present_keys.add(key)
            self._maybe_record_market(ref)
            snap = self.hub.build_snapshot(ref, now)
            model_ready = self.cfg.model_inference_active and self.model.ready_for(ref.combo.key)
            quality = assess(ref, snap, self.cfg, now, clock_synced, model_ready)
            data_ready = _data_ready(quality)
            snap.quality_status = "OK" if data_ready else quality.abstain_reason.value
            snap.prediction_ready = False
            if quality.abstain_reason in {
                AbstainReason.UNSAFE,
                AbstainReason.UNSAFE_TIME_METADATA,
            }:
                self._data_quality_errors += 1

            fv = self._feature_vector(ref, snap, now) if quality.snapshot_recordable else None
            envelope = self.forecaster.evaluate(
                ref.combo,
                snap.ts,
                fv,
                data_ready=data_ready,
                data_abstain_reason=quality.abstain_reason,
                data_notes=quality.notes,
            )
            snap.prediction_ready = envelope.prediction.decision != Decision.ABSTAIN

            if quality.snapshot_recordable:
                tte = snap.tte_sec if snap.tte_sec is not None else snap.seconds_remaining
                checkpoint = self._checkpoint_crossed(ref, tte)
                if checkpoint is not None:
                    if fv is not None:
                        snap.extra = fv.to_dict()
                    if self.recorder.record_snapshot(ref, snap, checkpoint):
                        self._snapshot_writes += 1
                    forecast_record = envelope.to_record()
                    forecast_record["ts"] = snap.ts
                    if self.recorder.record_forecast(
                        ref,
                        checkpoint,
                        forecast_record,
                        feature_coverage=(fv.feature_coverage if fv is not None else None),
                        quality_status=snap.quality_status,
                    ):
                        self._forecast_writes += 1
                    self._event(
                        "FORECAST",
                        f"{ref.combo.key} @t-{checkpoint}s "
                        f"decision={envelope.prediction.decision.value} "
                        f"reason={envelope.prediction.abstain_reason.value}",
                    )

            self.latest[key] = self._card(ref, snap, quality, envelope, fv)

        for key in list(self.latest):
            if key not in present_keys and self.latest[key].get("active"):
                self.latest[key]["active"] = False

    async def on_market_resolved(self, ref) -> None:  # noqa: ANN001
        official = ref.official_result or ref.resolved_outcome
        if official is None:
            return
        ref.computed_result, ref.computed_result_source = await self._compute_result(ref)
        ref.computed_result_time = time.time()
        label_status = self.recorder.settle(ref)
        self._resolve_count += 1
        self._event(
            "RESOLVED",
            f"{ref.combo.key} official={official.value} label={label_status}",
        )
        update = apply_pending_updates(
            self.recorder,
            self.model,
            self.calibration,
            training_enabled=self.cfg.training_active,
            calibration_enabled=self.cfg.calibration_active,
            model_path=self.cfg.model_path,
            calibration_path=self.cfg.calibration_path,
        )
        self._last_learning_update = update
        self._model_learn_calls += update.model_markets
        self._calibration_writes += update.forecast_rows
        if update.model_markets:
            self._model_save_calls += 1

    async def _compute_result(self, ref):  # noqa: ANN001
        if ref.combo.horizon == Horizon.H1H:
            return await self._compute_1h_finalized_candle(ref)
        # 5m/15m official result remains authoritative.  No closing-source proxy is
        # invented merely to manufacture MATCH.
        return None, None

    async def _compute_1h_finalized_candle(self, ref):  # noqa: ANN001
        if self._session is None or ref.market_start_ts is None:
            return None, None
        start_ms = int(ref.market_start_ts * 1000)
        symbol = ref.resolution_symbol or ref.combo.binance_symbol
        url = f"{self.cfg.binance_rest_base}/api/v3/klines"
        params = {
            "symbol": symbol,
            "interval": "1h",
            "startTime": str(start_ms - 3_600_000),
            "limit": "3",
        }
        try:
            async with self._session.get(url, params=params, timeout=12) as response:
                if response.status != 200:
                    return None, None
                rows = await response.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("%s finalized candle unavailable: %s", ref.combo.key, exc)
            return None, None
        return decide_1h_from_klines(rows, start_ms, time.time() * 1000)

    def _card(self, ref, snap, quality, envelope: ForecastEnvelope, fv) -> dict:  # noqa: ANN001
        def rounded(value, digits=3):
            return round(value, digits) if value is not None else None

        prediction = envelope.prediction
        return {
            "combo": ref.combo.key,
            "active": True,
            "market_id": (ref.market_id or "")[-8:],
            "slug": ref.slug,
            "condition_id": (ref.condition_id or "")[-8:],
            "up_token": (ref.up_token_id or "")[-8:],
            "down_token": (ref.down_token_id or "")[-8:],
            "tte_sec": rounded(snap.tte_sec, 1),
            "time_status": ref.time_status.value,
            "resolution_type": ref.resolution_type.value,
            "resolution_symbol": ref.resolution_symbol,
            "resolution_meta_ok": ref.has_resolution_meta,
            "official_reference_open": rounded(snap.official_reference_open, 4),
            "official_reference_source": snap.official_reference_source,
            "proxy_reference_open": rounded(snap.proxy_reference_open, 4),
            "reference_current": rounded(snap.reference_current, 4),
            "reference_current_age_ms": rounded(snap.reference_current_age_ms, 0),
            "spot_price": rounded(snap.spot_price, 4),
            "distance_bps": rounded(snap.official_distance_bps, 2),
            "proxy_distance_bps": rounded(snap.proxy_distance_bps, 2),
            "up_bid": rounded(snap.up_bid),
            "up_ask": rounded(snap.up_ask),
            "up_mid": rounded(snap.up_mid),
            "down_bid": rounded(snap.down_bid),
            "down_ask": rounded(snap.down_ask),
            "down_mid": rounded(snap.down_mid),
            "clob_age_ms": rounded(snap.clob_age_ms, 0),
            "transport_age_ms": rounded(snap.transport_age_ms, 0),
            "source_age_ms": rounded(snap.source_age_ms, 0),
            "book_age_ms": rounded(snap.book_age_ms, 0),
            "quality": quality.dims(),
            "data_ready": envelope.data_ready,
            "feature_ready": envelope.feature_ready,
            "prediction_ready": prediction.decision != Decision.ABSTAIN,
            "quality_notes": quality.notes,
            "features": fv.dashboard() if fv is not None else None,
            "decision": prediction.decision.value,
            "abstain_reason": prediction.abstain_reason.value,
            "regime": prediction.regime.value,
            "predictability": rounded(prediction.predictability, 4),
            "regime_diagnostics": envelope.regime.to_dict(),
            "p_up": rounded(prediction.p_up, 6),
            "p_up_raw": rounded(envelope.raw_p_up, 6),
            "p_up_no_clob": rounded(envelope.p_up_no_clob, 6),
            "confidence": rounded(prediction.confidence, 4),
            "baselines": envelope.baselines.to_dict(),
            "model_source": envelope.model_source,
            "model_version": envelope.model_version,
            "calibration_ready": envelope.calibration_ready,
            "calibration_source": envelope.calibration_source,
            "calibration_markets": envelope.calibration_markets,
            "threshold_ready": envelope.threshold_ready,
            "threshold_source": envelope.threshold_source,
            "decision_margin": rounded(envelope.decision_margin, 4),
            "why": prediction.reasons,
            "pipeline_diagnostics": envelope.diagnostics,
        }

    def snapshot(self) -> dict:
        combos = build_combos(self.cfg)
        discovery_status = self.hub.discovery.snapshot_status()
        cards: list[dict] = []
        up_mids: list[float] = []
        clob_quote_healthy = ptb_healthy = feature_ready = decision_ready = 0
        for combo in combos:
            card = self.latest.get(combo.key)
            if card is None or not card.get("active"):
                card = {
                    "combo": combo.key,
                    "active": False,
                    "discovery_status": discovery_status.get(combo.key, "NOT_FOUND"),
                    "decision": Decision.ABSTAIN.value,
                    "abstain_reason": AbstainReason.NO_MARKET.value,
                    "why": [f"discovery={discovery_status.get(combo.key, 'NOT_FOUND')}"],
                }
            else:
                card["discovery_status"] = discovery_status.get(combo.key, "FOUND")
                if card.get("up_mid") is not None and card.get("down_mid") is not None:
                    up_mids.append(card["up_mid"])
                    clob_quote_healthy += 1
                if card.get("official_reference_open") is not None:
                    ptb_healthy += 1
                if card.get("feature_ready"):
                    feature_ready += 1
                if card.get("prediction_ready"):
                    decision_ready += 1
            cards.append(card)

        active_count = sum(1 for card in cards if card.get("active"))
        clob_transport_healthy = (
            active_count if self._clob is not None and self._clob.transport_healthy else 0
        )
        suspicious = False
        if len(up_mids) >= 3:
            from collections import Counter
            suspicious = Counter(round(mid, 3) for mid in up_mids).most_common(1)[0][1] >= 3

        recorder_stats = self.recorder.stats()
        analytics = self.recorder.forecast_analytics(self.cfg.min_markets_for_stats)
        return {
            "now": time.time(),
            "uptime_sec": round(time.time() - self.started_at, 1),
            "mode": "SHADOW",
            "phase": self.cfg.phase,
            "cards": cards,
            "recorder": recorder_stats,
            "model": self.model.stats(),
            "calibration": self.calibration.summary(),
            "forecast_analytics": analytics,
            "min_markets_for_stats": self.cfg.min_markets_for_stats,
            "events": list(self.events),
            "discovery_status": discovery_status,
            "discovery_last_ts": self.hub.discovery.last_discovery_ts,
            "binance_connected": self.hub.binance.connected,
            "clock_synced": self.hub.binance.clock_synced,
            "clock_offset_ms": self.hub.binance.clock_offset_ms,
            "chainlink": (
                self.hub.reference.chainlink.status()
                if getattr(self.hub.reference, "chainlink", None) else {}
            ),
            "safety": {
                "phase": self.cfg.phase,
                "mode": "SHADOW",
                "model_training_enabled": self.cfg.training_active,
                "calibration_enabled": self.cfg.calibration_active,
                "model_learn_calls": self._model_learn_calls,
                "model_save_calls": self._model_save_calls,
                "calibration_writes": self._calibration_writes,
                "forecast_writes": self._forecast_writes,
                "live_orders": 0,
                "signing_enabled": False,
                "execution_enabled": False,
            },
            "footer": {
                "markets_active": active_count,
                "markets_discovered_total": recorder_stats["markets"],
                "snapshots_total": recorder_stats["snapshots"],
                "snapshots_labeled": recorder_stats["labeled_snapshots"],
                "forecasts_total": recorder_stats["forecasts"],
                "forecasts_labeled": recorder_stats["labeled_forecasts"],
                "forecasts_decided": recorder_stats["decided_forecasts"],
                "resolved_total": recorder_stats["resolved_markets"],
                "official_only": recorder_stats["official_only"],
                "label_mismatch": recorder_stats["label_mismatch"],
                "model_updates": recorder_stats["model_updates"],
                "calibration_updates": recorder_stats["calibration_updates"],
                "clob_transport_healthy": clob_transport_healthy,
                "clob_quote_healthy": clob_quote_healthy,
                "ptb_states_healthy": ptb_healthy,
                "feature_states_ready": feature_ready,
                "decision_states_ready": decision_ready,
                "discovery_errors": self.hub.discovery.discovery_errors,
                "data_quality_errors": self._data_quality_errors,
                "suspicious_identical_quotes": suspicious,
                **self.hub.clob_store.counters,
            },
        }

    async def run(self, stop: asyncio.Event) -> None:
        interval = self.cfg.snapshot_loop_ms / 1000.0
        log.info("ShadowEngine started (phase=%s, no execution)", self.cfg.phase)
        while not stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                log.exception("engine tick failed: %s", exc)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)
        log.info("ShadowEngine stopped | %s", self.recorder.stats())


async def _reference_refresher(
    hub: DataHub,
    session: aiohttp.ClientSession,
    stop: asyncio.Event,
    interval: float = 2.0,
) -> None:
    last_clock = 0.0
    while not stop.is_set():
        try:
            await hub.refresh_references(session)
            now = time.time()
            if now - last_clock >= 60.0:
                await hub.binance.refresh_clock()
                last_clock = now
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("reference refresher failed: %s", exc)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    import signal
    for signal_value in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_value, stop.set)
        except (NotImplementedError, RuntimeError):
            pass


async def _supervise(name: str, coro_factory, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await coro_factory(stop)
            return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("task '%s' crashed; isolated restart in 3s", name)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=3.0)


async def run() -> None:
    cfg = get_settings()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    cfg.enforce_phase_lock()
    # DirectionModel.load uses the process phase as an additional P1/P2.1 guard.
    os.environ["PHASE"] = cfg.phase

    combos = build_combos(cfg)
    symbols = sorted({combo.binance_symbol for combo in combos})
    log.info(
        "scope=%d combos symbols=%s mode=SHADOW phase=%s training=%s calibration=%s",
        len(combos), symbols, cfg.phase, cfg.training_active, cfg.calibration_active,
    )

    stop = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop)
    recorder = Recorder(cfg.db_path)
    model = DirectionModel.load(cfg.model_path) or DirectionModel(
        cfg.per_combo_model_min_markets,
        cfg.horizon_model_min_markets,
    )
    calibration = CalibrationBook.load(cfg.calibration_path) or CalibrationBook(
        min_n=cfg.min_markets_for_stats,
        min_fit_markets=cfg.calibration_min_markets,
        min_class_markets=cfg.calibration_min_class_markets,
        min_threshold_n=cfg.threshold_min_decisions,
        target_accuracy=cfg.threshold_target_accuracy,
    )
    bootstrap = apply_pending_updates(
        recorder,
        model,
        calibration,
        training_enabled=cfg.training_active,
        calibration_enabled=cfg.calibration_active,
        model_path=cfg.model_path,
        calibration_path=cfg.calibration_path,
    )
    log.info("startup shadow-learning bootstrap: %s", bootstrap.to_dict())

    async with aiohttp.ClientSession() as session:
        discovery = MarketDiscovery(cfg, session, combos)
        binance = BinanceFeed(cfg, symbols, session)
        clob_store = ClobQuoteStore()
        chainlink = ChainlinkFeed(cfg, session) if cfg.chainlink_enabled else None
        reference = ReferenceRouter(cfg, chainlink=chainlink)
        hub = DataHub(cfg, discovery, binance, clob_store, reference)
        engine = ShadowEngine(cfg, hub, recorder, model, calibration)
        engine.attach_session(session)
        discovery.on_resolved(engine.on_market_resolved)
        clob = ClobSupervisor(cfg, clob_store, session, hub.active_token_ids)
        engine.attach_clob(clob)

        if cfg.backfill_resolved_markets > 0:
            try:
                count = await discovery.backfill_resolved(
                    cfg.backfill_resolved_markets, recorder.backfill_market
                )
                log.info("backfill loaded %d resolved market metadata rows", count)
            except Exception as exc:  # noqa: BLE001
                log.warning("backfill failed: %s", exc)

        tasks = [
            asyncio.create_task(_supervise("discovery", discovery.run, stop)),
            asyncio.create_task(_supervise("binance", binance.run, stop)),
            asyncio.create_task(_supervise("clob", clob.run, stop)),
            asyncio.create_task(_supervise(
                "reference", lambda s: _reference_refresher(hub, session, s), stop
            )),
            asyncio.create_task(_supervise("engine", engine.run, stop)),
        ]
        if chainlink is not None:
            tasks.append(asyncio.create_task(_supervise("chainlink", chainlink.run, stop)))
        if cfg.web_enabled:
            tasks.append(asyncio.create_task(
                _supervise("web", lambda s: run_web(engine, cfg, s), stop)
            ))
        try:
            await asyncio.gather(*tasks)
        finally:
            recorder.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("stopped by user")
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        log.exception("fatal run failure")
        raise


if __name__ == "__main__":
    main()
