"""SQLite schema for the isolated P3 arbitrage research database."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path


P3_SCHEMA_VERSION = 2

DDL = """
CREATE TABLE IF NOT EXISTS p3_meta (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at_ms   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS p3_opportunities (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_key         TEXT NOT NULL UNIQUE,
    strategy                TEXT NOT NULL,
    condition_id            TEXT NOT NULL,
    combo_key               TEXT NOT NULL,
    detected_ts_ms          INTEGER NOT NULL,
    up_book_id              INTEGER NOT NULL,
    down_book_id            INTEGER NOT NULL,
    up_book_ts_ms           INTEGER NOT NULL,
    down_book_ts_ms         INTEGER NOT NULL,
    source_skew_ms          INTEGER NOT NULL,
    max_book_age_ms         INTEGER NOT NULL,
    quantity_shares         REAL NOT NULL CHECK(quantity_shares > 0),
    up_vwap                 REAL,
    down_vwap               REAL,
    up_fee_usdc             REAL NOT NULL DEFAULT 0,
    down_fee_usdc           REAL NOT NULL DEFAULT 0,
    gross_edge_per_share    REAL NOT NULL,
    gross_profit_usdc       REAL NOT NULL,
    execution_buffer_usdc   REAL NOT NULL DEFAULT 0,
    net_profit_usdc         REAL NOT NULL,
    capital_usdc            REAL NOT NULL,
    net_roi                 REAL NOT NULL,
    up_limit_price          REAL,
    down_limit_price        REAL,
    fee_lineage_ok          INTEGER NOT NULL,
    quality_status          TEXT NOT NULL,
    payload_json            TEXT NOT NULL,
    created_at_ms           INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_p3_opp_condition_time
ON p3_opportunities(condition_id,detected_ts_ms);
CREATE INDEX IF NOT EXISTS idx_p3_opp_strategy_profit
ON p3_opportunities(strategy,net_profit_usdc DESC);

CREATE TABLE IF NOT EXISTS p3_windows (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy                TEXT NOT NULL,
    condition_id            TEXT NOT NULL,
    combo_key               TEXT NOT NULL,
    opened_ts_ms            INTEGER NOT NULL,
    last_seen_ts_ms         INTEGER NOT NULL,
    closed_ts_ms            INTEGER,
    observations            INTEGER NOT NULL DEFAULT 1,
    peak_net_profit_usdc    REAL NOT NULL,
    peak_net_roi            REAL NOT NULL,
    peak_quantity_shares    REAL NOT NULL,
    peak_opportunity_id     INTEGER,
    status                  TEXT NOT NULL CHECK(status IN ('OPEN','CLOSED')),
    close_reason            TEXT,
    FOREIGN KEY(peak_opportunity_id) REFERENCES p3_opportunities(id)
);
CREATE INDEX IF NOT EXISTS idx_p3_windows_status
ON p3_windows(status,strategy,condition_id,last_seen_ts_ms);

-- Every positive scanner touch is persisted, including unchanged/deduplicated
-- book states. This is the strict continuity clock for P3.6.1 confirmation.
CREATE TABLE IF NOT EXISTS p3_window_observations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id           INTEGER NOT NULL,
    opportunity_id      INTEGER NOT NULL,
    observed_ts_ms      INTEGER NOT NULL,
    created_at_ms       INTEGER NOT NULL,
    UNIQUE(window_id,observed_ts_ms),
    FOREIGN KEY(window_id) REFERENCES p3_windows(id) ON DELETE CASCADE,
    FOREIGN KEY(opportunity_id) REFERENCES p3_opportunities(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_p3_window_obs_window_time
ON p3_window_observations(window_id,observed_ts_ms,id);

CREATE TABLE IF NOT EXISTS p3_replays (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id          INTEGER NOT NULL,
    delay_ms                INTEGER NOT NULL,
    target_ts_ms            INTEGER NOT NULL,
    observed_ts_ms          INTEGER,
    strategy                TEXT NOT NULL,
    quantity_shares         REAL NOT NULL,
    up_fill                 INTEGER NOT NULL DEFAULT 0,
    down_fill               INTEGER NOT NULL DEFAULT 0,
    both_fill               INTEGER NOT NULL DEFAULT 0,
    outcome                 TEXT NOT NULL,
    up_exec_price           REAL,
    down_exec_price         REAL,
    gross_profit_usdc       REAL,
    unwind_side             TEXT,
    unwind_price            REAL,
    unwind_fee_usdc         REAL,
    unwind_loss_usdc        REAL,
    cycle_net_pnl_usdc      REAL,
    details_json            TEXT NOT NULL,
    created_at_ms           INTEGER NOT NULL,
    UNIQUE(opportunity_id,delay_ms),
    FOREIGN KEY(opportunity_id) REFERENCES p3_opportunities(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_p3_replays_delay_outcome
ON p3_replays(delay_ms,outcome);

-- Confirmation-time replay is keyed to the actual scanner observation timestamp,
-- not the deduplicated opportunity's original detection timestamp.
CREATE TABLE IF NOT EXISTS p3_entry_replays (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id               INTEGER NOT NULL,
    confirm_ms              INTEGER NOT NULL,
    observation_id          INTEGER NOT NULL,
    opportunity_id          INTEGER NOT NULL,
    entry_ts_ms             INTEGER NOT NULL,
    delay_ms                INTEGER NOT NULL,
    target_ts_ms            INTEGER NOT NULL,
    observed_ts_ms          INTEGER,
    strategy                TEXT NOT NULL,
    quantity_shares         REAL NOT NULL,
    up_fill                 INTEGER NOT NULL DEFAULT 0,
    down_fill               INTEGER NOT NULL DEFAULT 0,
    both_fill               INTEGER NOT NULL DEFAULT 0,
    outcome                 TEXT NOT NULL,
    up_exec_price           REAL,
    down_exec_price         REAL,
    gross_profit_usdc       REAL,
    unwind_side             TEXT,
    unwind_price            REAL,
    unwind_fee_usdc         REAL,
    unwind_loss_usdc        REAL,
    cycle_net_pnl_usdc      REAL,
    details_json            TEXT NOT NULL,
    created_at_ms           INTEGER NOT NULL,
    UNIQUE(window_id,confirm_ms,delay_ms),
    FOREIGN KEY(window_id) REFERENCES p3_windows(id) ON DELETE CASCADE,
    FOREIGN KEY(observation_id) REFERENCES p3_window_observations(id) ON DELETE CASCADE,
    FOREIGN KEY(opportunity_id) REFERENCES p3_opportunities(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_p3_entry_replays_policy
ON p3_entry_replays(confirm_ms,delay_ms,outcome);

CREATE TABLE IF NOT EXISTS p3_health_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    component       TEXT NOT NULL,
    severity        TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    message         TEXT NOT NULL,
    details_json    TEXT,
    ts_ms           INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_p3_health_time ON p3_health_events(ts_ms);
"""


def connect_p3(path: str, *, read_only: bool = False) -> sqlite3.Connection:
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


def open_p26_read_only(path: str) -> sqlite3.Connection:
    resolved = Path(path).resolve()
    conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def ensure_p3_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    now_ms = int(time.time() * 1000)
    conn.execute(
        """
        INSERT INTO p3_meta(key,value,updated_at_ms)
        VALUES('schema_version',?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at_ms=excluded.updated_at_ms
        """,
        (str(P3_SCHEMA_VERSION), now_ms),
    )
    conn.commit()


def integrity_check(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    return str(row[0]) if row else "NO_RESULT"
