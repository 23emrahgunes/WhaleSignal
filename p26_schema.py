"""SQLite schema for the isolated P2.6 research database.

The P2.5 database is never migrated by this module.  P2.6 uses its own WAL
research database and only opens P2.5 in read-only mode when extracting canonical
rows.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Iterable


P26_SCHEMA_VERSION = 1


DDL = """
CREATE TABLE IF NOT EXISTS p26_meta (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at_ms   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS p26_oracle_ticks (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    asset               TEXT NOT NULL,
    source              TEXT NOT NULL,
    value_text          TEXT NOT NULL,
    value_real          REAL NOT NULL CHECK(value_real > 0),
    source_ts_ms        INTEGER NOT NULL,
    recv_ts_ms          INTEGER NOT NULL,
    payload_sha256      TEXT NOT NULL,
    schema_version      TEXT NOT NULL,
    inserted_at_ms      INTEGER NOT NULL,
    UNIQUE(asset, source, source_ts_ms, value_text, payload_sha256)
);
CREATE INDEX IF NOT EXISTS idx_p26_oracle_asset_source_ts
ON p26_oracle_ticks(asset, source_ts_ms);
CREATE INDEX IF NOT EXISTS idx_p26_oracle_recv_ts
ON p26_oracle_ticks(recv_ts_ms);

CREATE TABLE IF NOT EXISTS p26_canonical_rows (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id                TEXT NOT NULL,
    market_id                   TEXT,
    slug                        TEXT,
    combo_key                   TEXT NOT NULL,
    asset                       TEXT NOT NULL,
    horizon                     TEXT NOT NULL,
    market_start_ts_ms          INTEGER NOT NULL,
    market_end_ts_ms            INTEGER NOT NULL,
    checkpoint_sec              INTEGER NOT NULL,
    nominal_target_ts_ms        INTEGER NOT NULL,
    decision_ts_ms              INTEGER NOT NULL,
    capture_lag_ms              INTEGER NOT NULL,
    source_snapshot_id          INTEGER NOT NULL,
    feature_vector_json         TEXT NOT NULL,
    feature_names_json          TEXT NOT NULL,
    feature_vector_sha256       TEXT NOT NULL,
    feature_schema_version      TEXT NOT NULL,
    feature_schema_hash         TEXT NOT NULL,
    extraction_policy_version   TEXT NOT NULL,
    binance_trade_ts_ms         INTEGER,
    binance_book_ts_ms          INTEGER,
    clob_quote_ts_ms            INTEGER,
    chainlink_tick_id           INTEGER,
    chainlink_source_ts_ms      INTEGER,
    max_source_event_ts_ms      INTEGER,
    up_bid                      REAL,
    up_ask                      REAL,
    up_mid                      REAL,
    down_bid                    REAL,
    down_ask                    REAL,
    down_mid                    REAL,
    clob_spread                 REAL,
    clob_age_ms                 REAL,
    source_age_ms               REAL,
    book_age_ms                 REAL,
    quality_status              TEXT NOT NULL,
    lineage_status              TEXT NOT NULL,
    training_eligible           INTEGER NOT NULL DEFAULT 0,
    lineage_json                TEXT NOT NULL,
    code_commit                 TEXT NOT NULL,
    created_at_ms               INTEGER NOT NULL,
    FOREIGN KEY(chainlink_tick_id) REFERENCES p26_oracle_ticks(id) ON DELETE RESTRICT,
    UNIQUE(condition_id, checkpoint_sec, feature_schema_hash, extraction_policy_version)
);
CREATE INDEX IF NOT EXISTS idx_p26_canonical_combo_time
ON p26_canonical_rows(combo_key, decision_ts_ms);
CREATE INDEX IF NOT EXISTS idx_p26_canonical_training
ON p26_canonical_rows(training_eligible, decision_ts_ms);

CREATE TABLE IF NOT EXISTS p26_labels (
    condition_id                TEXT PRIMARY KEY,
    official_label              INTEGER CHECK(official_label IN (0,1)),
    official_result_source      TEXT,
    official_resolved_at_ms     INTEGER,
    computed_result             INTEGER CHECK(computed_result IN (0,1)),
    computed_status             TEXT NOT NULL DEFAULT 'UNKNOWN'
        CHECK(computed_status IN ('MATCH','MISMATCH','UNKNOWN')),
    updated_at_ms               INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_p26_labels_official
ON p26_labels(official_label, official_resolved_at_ms);

CREATE TABLE IF NOT EXISTS p26_health_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    component       TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    severity        TEXT NOT NULL,
    message         TEXT NOT NULL,
    details_json    TEXT,
    ts_ms           INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_p26_health_component_time
ON p26_health_events(component, ts_ms);
"""


def connect_p26(path: str, *, read_only: bool = False) -> sqlite3.Connection:
    resolved = Path(path).resolve()
    if read_only:
        conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True, timeout=30.0)
    else:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(resolved, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def ensure_p26_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    now_ms = int(time.time() * 1000)
    conn.execute(
        """
        INSERT INTO p26_meta(key,value,updated_at_ms)
        VALUES('schema_version',?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at_ms=excluded.updated_at_ms
        """,
        (str(P26_SCHEMA_VERSION), now_ms),
    )
    conn.commit()


def integrity_check(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    return str(row[0]) if row else "NO_RESULT"


def table_counts(conn: sqlite3.Connection, tables: Iterable[str] | None = None) -> dict[str, int]:
    if tables is None:
        tables = [
            row[0]
            for row in conn.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type='table' AND name LIKE 'p26_%'
                ORDER BY name
                """
            ).fetchall()
        ]
    result: dict[str, int] = {}
    for table in tables:
        quoted = '"' + str(table).replace('"', '""') + '"'
        result[str(table)] = int(conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
    return result
