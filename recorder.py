"""Direction Recorder — SQLite dataset (feature snapshot'lari + resmi etiket).

Iki tablo:
  - `markets`   : her kesfedilen market (resolution_source/type ZORUNLU) + resmi
                  resolved sonuc (label). meta_ok=0 -> resolution metadata eksik
                  (egitim-disi).
  - `snapshots` : checkpoint'lerde yazilan feature satirlari; kapanista `final_result`
                  RESMI resolved sonucla backfill edilir (yerel fiyat kiyasi DEGIL).

sqlite3 senkron; tum yazimlar tek asyncio event-loop icinde yapilir (kucuk hacim).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from typing import Optional

from models import FeatureSnapshot, MarketRef

log = logging.getLogger("direction_engine.recorder")


class Recorder:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.ensure_schema()

    def ensure_schema(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS markets (
                condition_id      TEXT PRIMARY KEY,
                combo_key         TEXT NOT NULL,
                asset             TEXT NOT NULL,
                horizon           TEXT NOT NULL,
                slug              TEXT,
                question          TEXT,
                start_ts          REAL,
                end_ts            REAL,
                resolution_source TEXT NOT NULL,
                resolution_type   TEXT NOT NULL,
                meta_ok           INTEGER NOT NULL DEFAULT 0,
                resolved          INTEGER NOT NULL DEFAULT 0,
                resolved_outcome  TEXT,
                discovered_ts     REAL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                condition_id      TEXT NOT NULL,
                combo_key         TEXT NOT NULL,
                checkpoint        INTEGER,
                ts                REAL NOT NULL,
                seconds_remaining REAL,
                spot_price        REAL,
                reference_price   REAL,
                distance_usd      REAL,
                distance_bps      REAL,
                up_mid            REAL,
                down_mid          REAL,
                clob_spread       REAL,
                spot_age_ms       REAL,
                book_age_ms       REAL,
                clob_age_ms       REAL,
                reference_age_ms  REAL,
                extra_json        TEXT,
                final_result      TEXT
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_snap_cond ON snapshots(condition_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_snap_combo ON snapshots(combo_key)")
        self.conn.commit()

    def record_market(self, ref: MarketRef) -> None:
        """Market metadata upsert (resolution_source/type zorunlu; meta_ok isareti)."""
        if not ref.condition_id:
            return
        meta_ok = 1 if ref.has_resolution_meta else 0
        self.conn.execute(
            """
            INSERT INTO markets (condition_id, combo_key, asset, horizon, slug, question,
                                 start_ts, end_ts, resolution_source, resolution_type,
                                 meta_ok, discovered_ts)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(condition_id) DO UPDATE SET
                slug=excluded.slug, question=excluded.question,
                start_ts=excluded.start_ts, end_ts=excluded.end_ts,
                resolution_source=excluded.resolution_source,
                resolution_type=excluded.resolution_type,
                meta_ok=excluded.meta_ok
            """,
            (
                ref.condition_id,
                ref.combo.key,
                ref.combo.asset.value,
                ref.combo.horizon.value,
                ref.slug,
                ref.question,
                ref.start_ts,
                ref.end_ts,
                ref.resolution_source,
                ref.resolution_type.value,
                meta_ok,
                ref.discovered_ts,
            ),
        )
        self.conn.commit()

    def record_snapshot(
        self, ref: MarketRef, snap: FeatureSnapshot, checkpoint: Optional[int]
    ) -> None:
        extra_json = json.dumps(snap.extra) if snap.extra else None
        self.conn.execute(
            """
            INSERT INTO snapshots (condition_id, combo_key, checkpoint, ts, seconds_remaining,
                                   spot_price, reference_price, distance_usd, distance_bps,
                                   up_mid, down_mid, clob_spread, spot_age_ms, book_age_ms,
                                   clob_age_ms, reference_age_ms, extra_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ref.condition_id,
                snap.combo.key,
                checkpoint,
                snap.ts,
                snap.seconds_remaining,
                snap.spot_price,
                snap.reference_price,
                snap.distance_usd,
                snap.distance_bps,
                snap.up_mid,
                snap.down_mid,
                snap.clob_spread,
                snap.spot_age_ms,
                snap.book_age_ms,
                snap.clob_age_ms,
                snap.reference_age_ms,
                extra_json,
            ),
        )
        self.conn.commit()

    def settle(self, ref: MarketRef) -> None:
        """RESMI resolved sonucu markete + tum snapshot'larina backfill et (label)."""
        if not ref.condition_id or not ref.resolved or ref.resolved_outcome is None:
            return
        outcome = ref.resolved_outcome.value
        self.conn.execute(
            "UPDATE markets SET resolved=1, resolved_outcome=? WHERE condition_id=?",
            (outcome, ref.condition_id),
        )
        self.conn.execute(
            "UPDATE snapshots SET final_result=? WHERE condition_id=?",
            (outcome, ref.condition_id),
        )
        self.conn.commit()
        log.info("recorder etiketledi: %s -> %s", ref.combo.key, outcome)

    def stats(self) -> dict:
        cur = self.conn.cursor()
        markets = cur.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
        resolved = cur.execute("SELECT COUNT(*) FROM markets WHERE resolved=1").fetchone()[0]
        meta_ok = cur.execute("SELECT COUNT(*) FROM markets WHERE meta_ok=1").fetchone()[0]
        snaps = cur.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        labeled = cur.execute(
            "SELECT COUNT(*) FROM snapshots WHERE final_result IS NOT NULL"
        ).fetchone()[0]
        per_combo = dict(
            cur.execute(
                "SELECT combo_key, COUNT(*) FROM markets WHERE resolved=1 GROUP BY combo_key"
            ).fetchall()
        )
        return {
            "markets": markets,
            "resolved_markets": resolved,
            "meta_ok_markets": meta_ok,
            "snapshots": snaps,
            "labeled_snapshots": labeled,
            "resolved_per_combo": per_combo,
        }

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
