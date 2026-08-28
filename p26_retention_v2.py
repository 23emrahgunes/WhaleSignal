"""Bound P2.6 research storage without deleting the current executable book.

V2 keeps only 15 minutes of raw CLOB history because P3 strict replay consumes its
short-horizon book path within minutes.  Book retention uses local observation time
(`recv_ts_ms`) and never deletes the freshest observed row for a token.  This avoids
removing a valid resting quote merely because its exchange source timestamp is old.

Other research retention remains intentionally broader:
- oracle ticks: 72h unless referenced by retained canonical rows,
- canonical rows: 168h,
- health events: 48h.

P2.5 paper and P3 databases are never opened by this module.
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path


RETENTION_VERSION = "P26_RETENTION_V2"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _delete_batches(
    conn: sqlite3.Connection,
    *,
    table: str,
    where_sql: str,
    params: tuple[object, ...],
    batch_size: int,
    max_batches: int,
) -> int:
    if not _table_exists(conn, table):
        return 0
    total = 0
    for _ in range(max_batches):
        rows = conn.execute(
            f"SELECT id FROM {table} WHERE {where_sql} ORDER BY id LIMIT ?",
            (*params, batch_size),
        ).fetchall()
        if not rows:
            break
        ids = [int(row[0]) for row in rows]
        marks = ",".join("?" for _ in ids)
        conn.execute(f"DELETE FROM {table} WHERE id IN ({marks})", ids)
        conn.commit()
        total += len(ids)
        if len(ids) < batch_size:
            break
    return total


def _prune_books(
    conn: sqlite3.Connection,
    *,
    cutoff_ms: int,
    batch_size: int,
    max_batches: int,
) -> int:
    if not _table_exists(conn, "p26_clob_books"):
        return 0
    total = 0
    for _ in range(max_batches):
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
            (int(cutoff_ms), int(batch_size)),
        ).fetchall()
        if not rows:
            break
        ids = [int(row[0]) for row in rows]
        marks = ",".join("?" for _ in ids)
        conn.execute(f"DELETE FROM p26_clob_books WHERE id IN ({marks})", ids)
        conn.commit()
        total += len(ids)
        if len(ids) < batch_size:
            break
    return total


def prune_p26_v2(
    db_path: str,
    *,
    book_hours: float = 0.25,
    oracle_hours: float = 72.0,
    canonical_hours: float = 168.0,
    health_hours: float = 48.0,
    batch_size: int = 5_000,
    max_batches: int = 200,
    now_ms: int | None = None,
) -> dict[str, int | str]:
    path = Path(db_path)
    if not path.exists():
        return {"status": "DB_MISSING", "version": RETENTION_VERSION}

    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    cut_book = now - int(float(book_hours) * 3_600_000)
    cut_oracle = now - int(float(oracle_hours) * 3_600_000)
    cut_canonical = now - int(float(canonical_hours) * 3_600_000)
    cut_health = now - int(float(health_hours) * 3_600_000)

    conn = sqlite3.connect(str(path), timeout=10.0)
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        result: dict[str, int | str] = {
            "status": "OK",
            "version": RETENTION_VERSION,
        }

        # Canonical rows reference oracle ticks, so prune them first.
        result["canonical_deleted"] = _delete_batches(
            conn,
            table="p26_canonical_rows",
            where_sql="decision_ts_ms < ?",
            params=(cut_canonical,),
            batch_size=batch_size,
            max_batches=max_batches,
        )
        result["books_deleted"] = _prune_books(
            conn,
            cutoff_ms=cut_book,
            batch_size=batch_size,
            max_batches=max_batches,
        )
        result["health_deleted"] = _delete_batches(
            conn,
            table="p26_health_events",
            where_sql="ts_ms < ?",
            params=(cut_health,),
            batch_size=batch_size,
            max_batches=max_batches,
        )

        if _table_exists(conn, "p26_oracle_ticks"):
            total = 0
            for _ in range(max_batches):
                rows = conn.execute(
                    """
                    SELECT o.id
                    FROM p26_oracle_ticks AS o
                    WHERE o.source_ts_ms < ?
                      AND NOT EXISTS (
                        SELECT 1 FROM p26_canonical_rows AS c
                        WHERE c.chainlink_tick_id=o.id
                      )
                    ORDER BY o.id
                    LIMIT ?
                    """,
                    (cut_oracle, batch_size),
                ).fetchall()
                if not rows:
                    break
                ids = [int(row[0]) for row in rows]
                marks = ",".join("?" for _ in ids)
                conn.execute(
                    f"DELETE FROM p26_oracle_ticks WHERE id IN ({marks})",
                    ids,
                )
                conn.commit()
                total += len(ids)
                if len(ids) < batch_size:
                    break
            result["oracle_deleted"] = total
        else:
            result["oracle_deleted"] = 0

        # PASSIVE avoids blocking the live collector. Freed pages remain reusable.
        try:
            checkpoint = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            result["wal_checkpoint"] = str(tuple(checkpoint) if checkpoint else ())
        except sqlite3.Error as exc:
            result["wal_checkpoint"] = f"ERROR:{type(exc).__name__}"
        return result
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/p26_research.sqlite")
    parser.add_argument("--book-hours", type=float, default=0.25)
    parser.add_argument("--oracle-hours", type=float, default=72.0)
    parser.add_argument("--canonical-hours", type=float, default=168.0)
    parser.add_argument("--health-hours", type=float, default=48.0)
    parser.add_argument("--batch-size", type=int, default=5_000)
    parser.add_argument("--max-batches", type=int, default=200)
    args = parser.parse_args()
    result = prune_p26_v2(
        args.db,
        book_hours=args.book_hours,
        oracle_hours=args.oracle_hours,
        canonical_hours=args.canonical_hours,
        health_hours=args.health_hours,
        batch_size=max(100, args.batch_size),
        max_batches=max(1, args.max_batches),
    )
    print("P26_RETENTION", result)


if __name__ == "__main__":
    main()
