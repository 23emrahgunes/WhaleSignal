"""Thread-safe stale-while-revalidate cache for heavy P2.5 dashboard snapshots.

P2.5's full snapshot includes SQLite analytics/count scans.  Those scans are useful
for the dashboard but must never make every HTTP poll recompute the same payload.
This helper keeps one completed snapshot, serves stale data immediately after TTL,
and refreshes it in exactly one background thread.

SHADOW/PAPER observability only; no execution, signing or credentials.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any, Optional

log = logging.getLogger("direction_engine.p25_snapshot_cache")


class SnapshotCache:
    def __init__(self, source: Callable[[], dict], ttl_sec: float = 5.0) -> None:
        self.source = source
        self.ttl_sec = max(0.5, float(ttl_sec))
        self._lock = threading.Lock()
        self._value: Optional[dict] = None
        self._completed_at = 0.0
        self._refreshing = False
        self._last_error: Optional[str] = None

    def _refresh(self) -> None:
        try:
            value = self.source()
            completed = time.monotonic()
            with self._lock:
                self._value = value
                # Freshness starts when the expensive snapshot FINISHES, not when
                # it starts.  This prevents a 30s calculation from being born stale.
                self._completed_at = completed
                self._last_error = None
        except Exception as exc:  # noqa: BLE001
            log.exception("P2.5 snapshot cache refresh failed: %s", exc)
            with self._lock:
                self._last_error = str(exc)
        finally:
            with self._lock:
                self._refreshing = False

    def prewarm(self) -> None:
        """Start the first heavy snapshot in a daemon thread without blocking asyncio."""
        with self._lock:
            if self._value is not None or self._refreshing:
                return
            self._refreshing = True
        threading.Thread(
            target=self._refresh,
            name="p25-snapshot-cache",
            daemon=True,
        ).start()

    def get(self) -> dict:
        """Return fresh/stale cache immediately; only first-ever miss may block.

        Once a value exists, an expired value is returned immediately and exactly one
        background refresh is launched.  That makes dashboard polling independent of
        SQLite scan duration.
        """
        now = time.monotonic()
        with self._lock:
            value = self._value
            age = now - self._completed_at if value is not None else None
            refreshing = self._refreshing
            if value is not None and age is not None and age < self.ttl_sec:
                return value
            if value is not None:
                if not refreshing:
                    self._refreshing = True
                    threading.Thread(
                        target=self._refresh,
                        name="p25-snapshot-cache",
                        daemon=True,
                    ).start()
                return value

        # A prewarm normally means this path is rare.  On the first request before
        # prewarm finishes, compute synchronously in the worker thread used by web.
        value = self.source()
        completed = time.monotonic()
        with self._lock:
            self._value = value
            self._completed_at = completed
            self._last_error = None
            self._refreshing = False
        return value

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ready": self._value is not None,
                "refreshing": self._refreshing,
                "age_sec": (
                    max(0.0, time.monotonic() - self._completed_at)
                    if self._value is not None
                    else None
                ),
                "last_error": self._last_error,
            }
