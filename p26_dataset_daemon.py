"""Periodic canonical dataset synchronizer for the isolated P2.6 database.

The builder owns persistent SQLite connections. Synchronization therefore runs on
the same event-loop thread that created those connections. The process is an
isolated sidecar with no latency-sensitive websocket work, so a short synchronous
SQLite scan is preferable to violating sqlite3 thread affinity via
``asyncio.to_thread``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import time

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

    consecutive_errors = 0
    try:
        while not stop.is_set():
            started = time.monotonic()
            try:
                result = builder.sync()
                consecutive_errors = 0
                elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
                payload = dict(result.__dict__)
                payload["elapsed_ms"] = elapsed_ms
                log.info(
                    "canonical sync %s",
                    json.dumps(payload, sort_keys=True),
                )
            except Exception:  # noqa: BLE001
                consecutive_errors += 1
                log.exception(
                    "canonical sync failed consecutive_errors=%d",
                    consecutive_errors,
                )
                # Fail the service after repeated errors so systemd restarts it
                # and deploy/status checks cannot mistake an endless error loop
                # for a healthy dataset sidecar.
                if consecutive_errors >= 3:
                    raise

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
