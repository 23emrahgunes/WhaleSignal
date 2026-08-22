"""Persistent full-depth Polymarket book snapshots for P2.6 research replay.

`source_ts_ms` is the exchange/book-change timestamp. `recv_ts_ms` is the most
recent local observation time for that exact state. If a reconnect returns an
identical unchanged snapshot, the existing row is not duplicated; its recv_ts_ms
is advanced so downstream consumers can prove the state was observed in the
current live socket session without pretending the exchange source timestamp was
new.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from typing import Iterable

from p26_execution import BookLevel, OrderBookSnapshot
from p26_schema import connect_p26, ensure_p26_schema


BOOK_SCHEMA_VERSION = "P26_CLOB_BOOK_V1"


def _levels_json(levels: Iterable[BookLevel]) -> str:
    return json.dumps(
        [[float(level.price), float(level.size)] for level in levels],
        separators=(",", ":"),
    )


def ensure_book_schema(conn: sqlite3.Connection) -> None:
    ensure_p26_schema(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS p26_clob_books (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            condition_id    TEXT NOT NULL,
            combo_key       TEXT NOT NULL,
            side            TEXT NOT NULL CHECK(side IN ('UP','DOWN')),
            token_id        TEXT NOT NULL,
            recv_ts_ms      INTEGER NOT NULL,
            source_ts_ms    INTEGER NOT NULL,
            sequence        INTEGER,
            bids_json       TEXT NOT NULL,
            asks_json       TEXT NOT NULL,
            payload_sha256  TEXT NOT NULL,
            schema_version  TEXT NOT NULL,
            inserted_at_ms  INTEGER NOT NULL,
            UNIQUE(token_id,source_ts_ms,payload_sha256)
        );
        CREATE INDEX IF NOT EXISTS idx_p26_clob_condition_side_time
        ON p26_clob_books(condition_id,side,source_ts_ms);
        """
    )
    conn.commit()


class BookSnapshotStore:
    def __init__(self, db_path: str) -> None:
        self.conn = connect_p26(db_path)
        ensure_book_schema(self.conn)

    def insert(
        self,
        *,
        condition_id: str,
        combo_key: str,
        side: str,
        snapshot: OrderBookSnapshot,
        recv_ts_ms: int | None = None,
    ) -> bool:
        side = side.strip().upper()
        if side not in {"UP", "DOWN"}:
            raise ValueError("side must be UP or DOWN")
        bids = _levels_json(snapshot.bids)
        asks = _levels_json(snapshot.asks)
        payload = json.dumps(
            {
                "token": snapshot.token_id,
                "ts": snapshot.ts_ms,
                "sequence": snapshot.sequence,
                "bids": json.loads(bids),
                "asks": json.loads(asks),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        observed_ms = int(snapshot.ts_ms if recv_ts_ms is None else recv_ts_ms)
        before = self.conn.total_changes
        self.conn.execute(
            """
            INSERT INTO p26_clob_books(
                condition_id,combo_key,side,token_id,recv_ts_ms,source_ts_ms,
                sequence,bids_json,asks_json,payload_sha256,schema_version,inserted_at_ms
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(token_id,source_ts_ms,payload_sha256) DO UPDATE SET
                recv_ts_ms=MAX(p26_clob_books.recv_ts_ms,excluded.recv_ts_ms),
                condition_id=excluded.condition_id,
                combo_key=excluded.combo_key,
                side=excluded.side
            """,
            (
                condition_id,
                combo_key,
                side,
                snapshot.token_id,
                observed_ms,
                int(snapshot.ts_ms),
                snapshot.sequence,
                bids,
                asks,
                digest,
                BOOK_SCHEMA_VERSION,
                int(time.time() * 1000),
            ),
        )
        self.conn.commit()
        return self.conn.total_changes > before

    @staticmethod
    def _decode(row: sqlite3.Row) -> OrderBookSnapshot:
        return OrderBookSnapshot.from_levels(
            token_id=str(row["token_id"]),
            ts_ms=int(row["source_ts_ms"]),
            bids=[tuple(value) for value in json.loads(str(row["bids_json"]))],
            asks=[tuple(value) for value in json.loads(str(row["asks_json"]))],
            sequence=(int(row["sequence"]) if row["sequence"] is not None else None),
        )

    def history(
        self,
        condition_id: str,
        side: str,
        *,
        start_ts_ms: int,
        end_ts_ms: int,
    ) -> list[OrderBookSnapshot]:
        rows = self.conn.execute(
            """
            SELECT * FROM p26_clob_books
            WHERE condition_id=? AND side=? AND source_ts_ms BETWEEN ? AND ?
            ORDER BY source_ts_ms,id
            """,
            (condition_id, side.upper(), int(start_ts_ms), int(end_ts_ms)),
        ).fetchall()
        return [self._decode(row) for row in rows]

    def latest(
        self,
        condition_id: str,
        side: str,
        *,
        at_or_before_ms: int,
    ) -> OrderBookSnapshot | None:
        row = self.conn.execute(
            """
            SELECT * FROM p26_clob_books
            WHERE condition_id=? AND side=? AND source_ts_ms<=?
            ORDER BY source_ts_ms DESC,id DESC LIMIT 1
            """,
            (condition_id, side.upper(), int(at_or_before_ms)),
        ).fetchone()
        return self._decode(row) if row is not None else None

    def prune(self, *, before_ts_ms: int, batch_size: int = 10_000) -> int:
        rows = self.conn.execute(
            "SELECT id FROM p26_clob_books WHERE source_ts_ms<? ORDER BY id LIMIT ?",
            (int(before_ts_ms), int(batch_size)),
        ).fetchall()
        if not rows:
            return 0
        ids = [int(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in ids)
        self.conn.execute(
            f"DELETE FROM p26_clob_books WHERE id IN ({placeholders})",
            ids,
        )
        self.conn.commit()
        return len(ids)

    def close(self) -> None:
        self.conn.close()
