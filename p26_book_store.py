"""Persistent full-depth Polymarket book snapshots for P2.6 research replay."""
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
        before = self.conn.total_changes
        self.conn.execute(
            """
            INSERT OR IGNORE INTO p26_clob_books(
                condition_id,combo_key,side,token_id,recv_ts_ms,source_ts_ms,
                sequence,bids_json,asks_json,payload_sha256,schema_version,inserted_at_ms
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                condition_id,
                combo_key,
                side,
                snapshot.token_id,
                int(snapshot.ts_ms if recv_ts_ms is None else recv_ts_ms),
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

    def close(self) -> None:
        self.conn.close()
