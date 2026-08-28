"""Bound the P2.6 research database so live research cannot fill the VPS disk.

This is research housekeeping only.  It never touches the P2.5 paper database or
P3 arbitrage database.  Deletes are committed in small batches to avoid long writer
locks while the P2.6 collectors are active.
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path


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


def prune_p26(
    db_path: str,
    *,
    book_hours: float = 24.0,
    oracle_hours: float = 72.0,
    canonical_hours: float = 168.0,
    health_hours: float = 48.0,
    batch_size: int = 5_000,
    max_batches: int = 200,
    now_ms: int | None = None,
) -> dict[str, int | str]:
    path = Path(db_path)
    if not path.exists():
        return {"status": "DB_MISSING"}

    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    cut_book = now - int(book_hours * 3_600_000)
    cut_oracle = now - int(oracle_hours * 3_600_000)
    cut_canonical = now - int(canonical_hours * 3_600_000)
    cut_health = now - int(health_hours * 3_600_000)

    conn = sqlite3.connect(str(path), timeout=60.0)
    try:
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("PRAGMA foreign_keys=ON")
        result: dict[str, int | str] = {"status": "OK"}

        # Canonical rows reference oracle ticks, so prune canonical history first.
        result["canonical_deleted"] = _delete_batches(
            conn,
            table="p26_canonical_rows",
            where_sql="decision_ts_ms < ?",
            params=(cut_canonical,),
            batch_size=batch_size,
            max_batches=max_batches,
        )
        result["books_deleted"] = _delete_batches(
            conn,
            table="p26_clob_books",
            where_sql="source_ts_ms < ?",
            params=(cut_book,),
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

        # Oracle rows still referenced by retained canonical rows must never be removed.
        if _table_exists(conn, "p26_oracle_ticks"):
            total = 0
            for _ in range(max_batches):
                rows = conn.execute(
                    """
                    SELECT o.id
                    FROM p26_oracle_ticks o
                    WHERE o.source_ts_ms < ?
                      AND NOT EXISTS (
                          SELECT 1 FROM p26_canonical_rows c
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

        # Reclaim the WAL immediately. Main DB free pages remain reusable by SQLite,
        # preventing unbounded file growth without an expensive online VACUUM.
        try:
            checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            result["wal_checkpoint"] = str(tuple(checkpoint) if checkpoint else ())
        except sqlite3.Error as exc:
            result["wal_checkpoint"] = f"ERROR:{type(exc).__name__}"
        return result
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/p26_research.sqlite")
    parser.add_argument("--book-hours", type=float, default=24.0)
    parser.add_argument("--oracle-hours", type=float, default=72.0)
    parser.add_argument("--canonical-hours", type=float, default=168.0)
    parser.add_argument("--health-hours", type=float, default=48.0)
    parser.add_argument("--batch-size", type=int, default=5_000)
    parser.add_argument("--max-batches", type=int, default=200)
    args = parser.parse_args()
    result = prune_p26(
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
