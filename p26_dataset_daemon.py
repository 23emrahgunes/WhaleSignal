"""Periodic canonical dataset synchronizer for the isolated P2.6 database."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal

from p26_config import get_p26_settings
from p26_dataset import CanonicalDatasetBuilder

log = logging.getLogger("direction_engine.p26.dataset")


async def run(interval_sec: float) -> None:
    settings = get_p26_settings()
    builder = CanonicalDatasetBuilder(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    try:
        while not stop.is_set():
            try:
                result = await asyncio.to_thread(builder.sync)
                log.info("canonical sync %s", json.dumps(result.__dict__, sort_keys=True))
            except Exception:  # noqa: BLE001
                log.exception("canonical sync failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_sec)
            except asyncio.TimeoutError:
                pass
    finally:
        builder.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval-sec", type=float, default=10.0)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    asyncio.run(run(max(1.0, args.interval_sec)))


if __name__ == "__main__":
    main()
