#!/usr/bin/env python3
"""External watchdog for the P2.6 public book collector.

Runs outside the collector process so it can recover a stuck event loop. The watchdog
only observes the public-research heartbeat stored in p26_meta. If the heartbeat is
stale or the socket has remained disconnected beyond a short grace, it restarts the
book collector systemd service.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


DEFAULT_MAX_HEARTBEAT_AGE_MS = 15_000
DEFAULT_DISCONNECTED_GRACE_MS = 5_000
HEALTH_KEY = "book_collector_health_json"


def evaluate_health(
    health: dict[str, Any],
    *,
    now_ms: int,
    max_heartbeat_age_ms: int = DEFAULT_MAX_HEARTBEAT_AGE_MS,
    disconnected_grace_ms: int = DEFAULT_DISCONNECTED_GRACE_MS,
) -> tuple[bool, str, int]:
    heartbeat = int(health.get("heartbeat_ts_ms") or 0)
    age = int(now_ms) - heartbeat if heartbeat > 0 else 10**12
    if heartbeat <= 0:
        return False, "NO_HEARTBEAT", age
    if age > int(max_heartbeat_age_ms):
        return False, "STALE_HEARTBEAT", age
    if not bool(health.get("connected")) and age > int(disconnected_grace_ms):
        return False, "DISCONNECTED", age
    return True, "OK", age


def read_health(db_path: str) -> dict[str, Any]:
    path = Path(db_path).resolve()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
    try:
        conn.execute("PRAGMA busy_timeout=1000")
        row = conn.execute(
            "SELECT value FROM p26_meta WHERE key=?", (HEALTH_KEY,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {}
    value = json.loads(str(row[0]))
    return value if isinstance(value, dict) else {}


def restart_service(service: str) -> None:
    subprocess.run(["/bin/systemctl", "restart", service], check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/p26_research.sqlite")
    parser.add_argument(
        "--service", default="direction-engine-p26-book.service"
    )
    parser.add_argument(
        "--max-heartbeat-age-ms", type=int, default=DEFAULT_MAX_HEARTBEAT_AGE_MS
    )
    parser.add_argument(
        "--disconnected-grace-ms", type=int, default=DEFAULT_DISCONNECTED_GRACE_MS
    )
    parser.add_argument("--no-restart", action="store_true")
    args = parser.parse_args()

    now_ms = int(time.time() * 1000)
    try:
        health = read_health(args.db)
        healthy, reason, age = evaluate_health(
            health,
            now_ms=now_ms,
            max_heartbeat_age_ms=args.max_heartbeat_age_ms,
            disconnected_grace_ms=args.disconnected_grace_ms,
        )
    except Exception as exc:  # noqa: BLE001
        healthy, reason, age = False, f"HEALTH_READ_ERROR:{type(exc).__name__}", -1
        health = {}

    print(
        f"healthy={str(healthy).lower()} reason={reason} heartbeat_age_ms={age} "
        f"connected={health.get('connected')} tokens={health.get('subscribed_tokens')}"
    )
    if healthy:
        return 0

    if args.no_restart:
        return 2

    print(f"watchdog_restart_service={args.service}")
    restart_service(args.service)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
