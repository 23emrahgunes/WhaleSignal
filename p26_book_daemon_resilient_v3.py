"""P2.6 resilient CLOB book collector V3 with bounded raw-history writes.

V2 made reconnect/session seeding resilient, but the risk-critical empty-ask fix
bypassed the normal history throttle for *every* event while asks stayed empty.
On active crypto markets that can create thousands of SQLite rows per second.

V3 preserves the 100ms-class raw history required by P3 strict replay while making
empty-book persistence transition-aware:

- non-empty -> empty is persisted immediately,
- repeated events while the ask side remains empty are not persisted,
- empty -> non-empty is persisted immediately,
- ordinary non-empty changes keep the configured minimum persistence interval,
- REST session seeds persist empty asks too, so reconnects cannot resurrect ghost
  liquidity.

Raw book history is pruned in the background to 15 minutes while always retaining
the freshest observed row for each token.  This module is SHADOW/PAPER data
collection only; it contains no credentials, signing or order submission.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sqlite3
import time
from typing import Any, Optional

import aiohttp

from p26_book_daemon import LocalBook
from p26_book_daemon_resilient_v2 import (
    ResilientBookCollectorV2,
    _source_timestamp_ms,
)
from p26_config import P26Settings, get_p26_settings


log = logging.getLogger("direction_engine.p26.book.resilient_v3")
BOOK_HISTORY_RETENTION_MS = 15 * 60 * 1000
PRUNE_INTERVAL_SEC = 300.0
PRUNE_BATCH_SIZE = 5_000
PRUNE_MAX_BATCHES = 100
PRUNE_BUSY_TIMEOUT_MS = 1_000


class ResilientBookCollectorV3(ResilientBookCollectorV2):
    """V2 transport semantics plus transition-aware empty-book persistence."""

    def __init__(self, settings: P26Settings) -> None:
        super().__init__(settings)
        self.last_empty_ask_state: dict[str, bool] = {}

    def _persist(
        self,
        token: str,
        ts_ms: int,
        sequence: Optional[int],
        recv_ms: int,
    ) -> None:
        meta = self.token_meta.get(token)
        book = self.local_books.get(token)
        if meta is None or book is None:
            return

        empty_now = not book.asks
        previous_empty = self.last_empty_ask_state.get(token)
        first_observation = previous_empty is None
        state_transition = (
            previous_empty is not None and previous_empty != empty_now
        )
        self.last_empty_ask_state[token] = empty_now

        # Once the executable ask side is known empty, subsequent bid/other events
        # do not add BUY-side truth.  Persisting every one of them was the write storm.
        # The transition back to non-empty is always persisted immediately below.
        if empty_now and previous_empty is True:
            return

        last = self.last_persist_ms.get(token, 0)
        if (
            not first_observation
            and not state_transition
            and recv_ms - last < self.settings.book_persist_min_interval_ms
        ):
            return

        condition_id, combo_key, side = meta
        snapshot = book.snapshot(token_id=token, ts_ms=ts_ms, sequence=sequence)
        created = self.books.insert(
            condition_id=condition_id,
            combo_key=combo_key,
            side=side,
            snapshot=snapshot,
            recv_ts_ms=recv_ms,
        )
        if created:
            self.persisted += 1
        self.last_persist_ms[token] = recv_ms

    def _apply_session_seed(
        self,
        payload: object,
        *,
        recv_ms: int,
        session_started_ms: int,
    ) -> tuple[int, int]:
        """Seed every usable REST book, including an explicitly empty ask side."""
        if not isinstance(payload, list):
            return 0, len(self.token_meta)

        seeded_tokens: set[str] = set()
        observed_ms = max(int(recv_ms), int(session_started_ms))
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            token = str(raw.get("asset_id") or "")
            meta = self.token_meta.get(token)
            if meta is None:
                continue

            book = self.local_books.setdefault(token, LocalBook())
            book.apply_snapshot(raw.get("bids") or [], raw.get("asks") or [])
            source_ts_ms = _source_timestamp_ms(raw.get("timestamp"), int(recv_ms))
            snapshot = book.snapshot(
                token_id=token,
                ts_ms=source_ts_ms,
                sequence=None,
            )
            condition_id, combo_key, side = meta
            created = self.books.insert(
                condition_id=condition_id,
                combo_key=combo_key,
                side=side,
                snapshot=snapshot,
                recv_ts_ms=observed_ms,
            )
            if created:
                self.persisted += 1
            self.last_persist_ms[token] = observed_ms
            self.last_empty_ask_state[token] = not book.asks
            seeded_tokens.add(token)

        return len(seeded_tokens), max(0, len(self.token_meta) - len(seeded_tokens))


def prune_book_history(
    db_path: str,
    *,
    now_ms: int | None = None,
    retention_ms: int = BOOK_HISTORY_RETENTION_MS,
    batch_size: int = PRUNE_BATCH_SIZE,
    max_batches: int = PRUNE_MAX_BATCHES,
) -> int:
    """Delete old raw history but always retain the freshest row per token.

    `recv_ts_ms` is used for storage retention rather than exchange source time:
    reconnect/session observation can prove a resting quote is current even when its
    source timestamp is old.  A row is deleted only when a fresher observed row for
    the same token exists.
    """
    cutoff = (int(time.time() * 1000) if now_ms is None else int(now_ms)) - int(
        retention_ms
    )
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.execute(f"PRAGMA busy_timeout={PRUNE_BUSY_TIMEOUT_MS}")
        total = 0
        for _ in range(max(1, int(max_batches))):
            rows = conn.execute(
                """
                SELECT b.id
                FROM p26_clob_books AS b
                WHERE b.recv_ts_ms < ?
                  AND EXISTS (
                    SELECT 1
                    FROM p26_clob_books AS newer
                    WHERE newer.token_id=b.token_id
                      AND (
                        newer.recv_ts_ms > b.recv_ts_ms
                        OR (newer.recv_ts_ms=b.recv_ts_ms AND newer.id>b.id)
                      )
                  )
                ORDER BY b.id
                LIMIT ?
                """,
                (cutoff, max(100, int(batch_size))),
            ).fetchall()
            if not rows:
                break
            ids = [int(row[0]) for row in rows]
            marks = ",".join("?" for _ in ids)
            conn.execute(f"DELETE FROM p26_clob_books WHERE id IN ({marks})", ids)
            conn.commit()
            total += len(ids)
            if len(ids) < max(100, int(batch_size)):
                break
        # PASSIVE never waits for readers/writers; retention must not disturb feed.
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        return total
    finally:
        conn.close()


async def _prune_loop(db_path: str, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=PRUNE_INTERVAL_SEC)
            return
        except asyncio.TimeoutError:
            pass
        try:
            deleted = await asyncio.to_thread(prune_book_history, db_path)
            if deleted:
                log.info("P2.6 V3 bounded-history prune deleted=%d", deleted)
        except sqlite3.OperationalError as exc:
            log.warning("P2.6 V3 bounded-history prune skipped error=%r", exc)
        except Exception:  # noqa: BLE001
            log.exception("P2.6 V3 bounded-history prune failed")


async def _run() -> None:
    settings = get_p26_settings()
    collector = ResilientBookCollectorV3(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    prune_task = asyncio.create_task(_prune_loop(settings.p26_db_path, stop))
    try:
        await collector.run(stop)
    finally:
        stop.set()
        prune_task.cancel()
        try:
            await prune_task
        except asyncio.CancelledError:
            pass
        collector.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    asyncio.run(_run())


if __name__ == "__main__":
    main()
