"""P2.5 SHADOW service entrypoint.

Deploy this entrypoint after P2.1. It reuses the proven discovery/feed/reference
plumbing and swaps in the P2.2-P2.5 engine/recorder. No order execution exists.
"""
from __future__ import annotations

import asyncio
import logging

import aiohttp

from binance_feed import BinanceFeed
from chainlink_feed import ChainlinkFeed
from clob_feed import ClobQuoteStore, ClobSupervisor
from hub import DataHub
from main import (
    _install_signal_handlers,
    _reference_refresher,
    _supervise,
    build_combos,
)
from p25_calibration import CalibrationBook
from p25_config import Settings
from p25_discovery import P25MarketDiscovery
from p25_model import DirectionModel
from p25_recorder import P25Recorder
from p25_safety_engine import P25Engine
from p25_web import run_web
from reference import ReferenceRouter

log = logging.getLogger("direction_engine.p25_main")


async def run() -> None:
    cfg = Settings()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    cfg.enforce_phase_lock()
    combos = build_combos(cfg)
    symbols = sorted({combo.binance_symbol for combo in combos})
    log.info(
        "scope=%d combos symbols=%s SHADOW phase=%s "
        "training=%s calibration=%s forecasts=%s",
        len(combos),
        symbols,
        cfg.phase,
        cfg.training_active,
        cfg.calibration_active,
        cfg.forecast_recording_active,
    )

    stop = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop)

    recorder = P25Recorder(cfg.db_path)
    if cfg.model_inference_active:
        model = DirectionModel.load(cfg.model_path) or DirectionModel(
            cfg.per_combo_model_min_markets,
            cfg.model_min_markets_predict,
        )
    else:
        model = DirectionModel(
            cfg.per_combo_model_min_markets,
            cfg.model_min_markets_predict,
        )
    calibration = (
        CalibrationBook.load(
            cfg.calibration_path,
            min_n=cfg.min_markets_for_stats,
        )
        if cfg.calibration_active
        else CalibrationBook(min_n=cfg.min_markets_for_stats)
    )

    async with aiohttp.ClientSession() as session:
        discovery = P25MarketDiscovery(cfg, session, combos)
        binance = BinanceFeed(cfg, symbols, session)
        clob_store = ClobQuoteStore()
        chainlink = (
            ChainlinkFeed(cfg, session) if cfg.chainlink_enabled else None
        )
        reference = ReferenceRouter(cfg, chainlink=chainlink)
        hub = DataHub(cfg, discovery, binance, clob_store, reference)
        engine = P25Engine(cfg, hub, recorder, model, calibration)
        engine.attach_session(session)
        discovery.on_resolved(engine.on_market_resolved)

        clob = ClobSupervisor(
            cfg,
            clob_store,
            session,
            hub.active_token_ids,
        )
        engine.attach_clob(clob)

        if cfg.backfill_resolved_markets > 0:
            try:
                count = await discovery.backfill_resolved(
                    cfg.backfill_resolved_markets,
                    recorder.backfill_market,
                )
                log.info(
                    "backfill=%d resolved markets metadata-only",
                    count,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("backfill failed: %s", exc)

        tasks = [
            asyncio.create_task(
                _supervise("discovery", discovery.run, stop),
                name="discovery",
            ),
            asyncio.create_task(
                _supervise("binance", binance.run, stop),
                name="binance",
            ),
            asyncio.create_task(
                _supervise("clob", clob.run, stop),
                name="clob",
            ),
            asyncio.create_task(
                _supervise(
                    "reference",
                    lambda event: _reference_refresher(
                        hub, session, event
                    ),
                    stop,
                ),
                name="reference",
            ),
            asyncio.create_task(
                _supervise("engine", engine.run, stop),
                name="engine",
            ),
        ]
        if chainlink is not None:
            tasks.append(
                asyncio.create_task(
                    _supervise("chainlink", chainlink.run, stop),
                    name="chainlink",
                )
            )
        if cfg.web_enabled:
            tasks.append(
                asyncio.create_task(
                    _supervise(
                        "web",
                        lambda event: run_web(engine, cfg, event),
                        stop,
                    ),
                    name="web",
                )
            )

        try:
            await asyncio.gather(*tasks)
        finally:
            if cfg.calibration_active:
                calibration.save(cfg.calibration_path)
            if cfg.training_active:
                model.save(cfg.model_path)
            recorder.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("stopped by user")
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        log.exception("P2.5 service fatal error")
        raise


if __name__ == "__main__":
    main()
