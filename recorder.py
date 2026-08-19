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
                market_id         TEXT,
                combo_key         TEXT NOT NULL,
                asset             TEXT NOT NULL,
                horizon           TEXT NOT NULL,
                slug              TEXT,
                question          TEXT,
                market_start      REAL,
                market_end        REAL,
                time_status       TEXT,
                start_ts          REAL,
                end_ts            REAL,
                resolution_source TEXT NOT NULL,
                resolution_type   TEXT NOT NULL,
                meta_ok           INTEGER NOT NULL DEFAULT 0,
                resolved          INTEGER NOT NULL DEFAULT 0,
                resolved_outcome  TEXT,
                official_result   TEXT,
                computed_result   TEXT,
                label_status      TEXT,
                source            TEXT NOT NULL DEFAULT 'live',
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
                checkpoint_sec    INTEGER,
                ts                REAL NOT NULL,
                market_start      REAL,
                market_end        REAL,
                tte_sec           REAL,
                seconds_remaining REAL,
                spot_price        REAL,
                reference_price   REAL,
                distance_usd      REAL,
                distance_bps      REAL,
                up_bid            REAL,
                up_ask            REAL,
                up_mid            REAL,
                down_bid          REAL,
                down_ask          REAL,
                down_mid          REAL,
                clob_spread       REAL,
                spot_age_ms       REAL,
                book_age_ms       REAL,
                transport_age_ms  REAL,
                source_age_ms     REAL,
                clob_age_ms       REAL,
                reference_age_ms  REAL,
                quality_status    TEXT,
                source            TEXT NOT NULL DEFAULT 'live',
                extra_json        TEXT,
                final_result      TEXT,
                UNIQUE(condition_id, checkpoint_sec)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_snap_cond ON snapshots(condition_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_snap_combo ON snapshots(combo_key)")
        self.conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """Eski DB'lere eksik kolonlari ekle (ALTER TABLE ADD COLUMN, idempotent)."""
        want = {
            "markets": [
                ("market_id", "TEXT"), ("market_start", "REAL"), ("market_end", "REAL"),
                ("time_status", "TEXT"), ("official_result", "TEXT"),
                ("computed_result", "TEXT"), ("label_status", "TEXT"),
                ("source", "TEXT DEFAULT 'live'"),
                ("resolution_symbol", "TEXT"), ("official_result_source", "TEXT"),
                ("official_resolved_at", "REAL"),
            ],
            "snapshots": [
                ("market_start", "REAL"), ("market_end", "REAL"), ("tte_sec", "REAL"),
                ("checkpoint_sec", "INTEGER"), ("up_bid", "REAL"), ("up_ask", "REAL"),
                ("down_bid", "REAL"), ("down_ask", "REAL"), ("transport_age_ms", "REAL"),
                ("source_age_ms", "REAL"), ("quality_status", "TEXT"),
                ("source", "TEXT DEFAULT 'live'"),
                ("resolution_symbol", "TEXT"),
                ("official_reference_open", "REAL"), ("official_reference_open_time", "REAL"),
                ("official_reference_source", "TEXT"),
                ("proxy_reference_open", "REAL"), ("proxy_reference_open_time", "REAL"),
                ("proxy_reference_source", "TEXT"),
                ("official_distance_bps", "REAL"), ("proxy_distance_bps", "REAL"),
                ("reference_current", "REAL"), ("reference_current_time", "REAL"),
            ],
        }
        cur = self.conn.cursor()
        for table, cols in want.items():
            existing = {r[1] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, decl in cols:
                if name not in existing:
                    try:
                        cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                    except Exception:  # noqa: BLE001
                        pass
        self.conn.commit()

    def record_market(self, ref: MarketRef, source: str = "live") -> None:
        """Market metadata upsert (canonical zaman + resolution; meta_ok isareti)."""
        if not ref.condition_id:
            return
        meta_ok = 1 if ref.has_resolution_meta else 0
        self.conn.execute(
            """
            INSERT INTO markets (condition_id, market_id, combo_key, asset, horizon, slug,
                                 question, market_start, market_end, time_status,
                                 start_ts, end_ts, resolution_source, resolution_type,
                                 resolution_symbol, meta_ok, source, discovered_ts)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(condition_id) DO UPDATE SET
                slug=excluded.slug, question=excluded.question,
                market_start=excluded.market_start, market_end=excluded.market_end,
                time_status=excluded.time_status,
                start_ts=excluded.start_ts, end_ts=excluded.end_ts,
                resolution_source=excluded.resolution_source,
                resolution_type=excluded.resolution_type,
                resolution_symbol=excluded.resolution_symbol,
                meta_ok=excluded.meta_ok
            """,
            (
                ref.condition_id,
                ref.market_id,
                ref.combo.key,
                ref.combo.asset.value,
                ref.combo.horizon.value,
                ref.slug,
                ref.question,
                ref.market_start_ts,
                ref.market_end_ts,
                ref.time_status.value,
                ref.start_ts,
                ref.end_ts,
                ref.resolution_source,
                ref.resolution_type.value,
                ref.resolution_symbol,
                meta_ok,
                source,
                ref.discovered_ts,
            ),
        )
        self.conn.commit()

    def backfill_market(self, ref: MarketRef) -> None:
        """P1 settlement/label testi: resolved market'i SADECE markets tablosuna
        source='backfill' ile yaz. **Snapshot/feature URETMEZ** (fabricate yok)."""
        self.record_market(ref, source="backfill")
        if ref.resolved:
            self.settle(ref)

    def record_snapshot(
        self, ref: MarketRef, snap: FeatureSnapshot, checkpoint: Optional[int]
    ) -> None:
        """Ham snapshot row. **INSERT OR IGNORE** — UNIQUE(condition_id, checkpoint_sec)
        ile ayni checkpoint iki kez yazilmaz (reconnect/restart dedup)."""
        extra_json = json.dumps(snap.extra) if snap.extra else None
        self.conn.execute(
            """
            INSERT OR IGNORE INTO snapshots
                (condition_id, combo_key, checkpoint_sec, ts, market_start, market_end,
                 tte_sec, seconds_remaining, spot_price, reference_price, distance_usd,
                 distance_bps, up_bid, up_ask, up_mid, down_bid, down_ask, down_mid,
                 clob_spread, spot_age_ms, book_age_ms, transport_age_ms, source_age_ms,
                 clob_age_ms, reference_age_ms, quality_status, source, resolution_symbol,
                 official_reference_open, official_reference_open_time, official_reference_source,
                 proxy_reference_open, proxy_reference_open_time, proxy_reference_source,
                 official_distance_bps, proxy_distance_bps, reference_current,
                 reference_current_time, extra_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                    ?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ref.condition_id, snap.combo.key, checkpoint, snap.ts,
                snap.market_start, snap.market_end, snap.tte_sec, snap.seconds_remaining,
                snap.spot_price, snap.reference_price, snap.distance_usd, snap.distance_bps,
                snap.up_bid, snap.up_ask, snap.up_mid, snap.down_bid, snap.down_ask, snap.down_mid,
                snap.clob_spread, snap.spot_age_ms, snap.book_age_ms, snap.transport_age_ms,
                snap.source_age_ms, snap.clob_age_ms, snap.reference_age_ms,
                snap.quality_status, "live", snap.resolution_symbol,
                snap.official_reference_open, snap.official_reference_open_time,
                snap.official_reference_source, snap.proxy_reference_open,
                snap.proxy_reference_open_time, snap.proxy_reference_source,
                snap.official_distance_bps, snap.proxy_distance_bps, snap.reference_current,
                snap.reference_current_time, extra_json,
            ),
        )
        self.conn.commit()

    def settle(self, ref: MarketRef) -> None:
        """**EXPLICIT official** sonucu markete + snapshot'lara yaz (label).

        official_result = explicit metadata (discovery). computed_result = yerel audit
        (ref.computed_result, engine hesaplar). label_status = MATCH/MISMATCH/UNKNOWN.
        final_result (training label) YALNIZ label_status != UNKNOWN ve official varsa yazilir.
        """
        if not ref.condition_id or not ref.resolved:
            return
        official = ref.official_result or ref.resolved_outcome
        if official is None:
            return  # explicit official yok -> labeled sayma
        off = official.value
        computed = ref.computed_result.value if ref.computed_result else None
        label_status = ref.label_status.value if ref.label_status else "UNKNOWN"
        self.conn.execute(
            """UPDATE markets SET resolved=1, resolved_outcome=?, official_result=?,
                   official_result_source=?, official_resolved_at=?,
                   computed_result=?, label_status=? WHERE condition_id=?""",
            (off, off, ref.official_result_source, ref.official_resolved_at,
             computed, label_status, ref.condition_id),
        )
        # training label yalniz MISMATCH DEGILSE yazilir (MISMATCH -> training-disi)
        if label_status != "MISMATCH":
            self.conn.execute(
                "UPDATE snapshots SET final_result=? WHERE condition_id=?",
                (off, ref.condition_id),
            )
        self.conn.commit()
        log.info(
            "recorder etiketledi: %s official=%s computed=%s label=%s",
            ref.combo.key, off, computed, label_status,
        )

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
        mismatch = cur.execute(
            "SELECT COUNT(*) FROM markets WHERE label_status='MISMATCH'"
        ).fetchone()[0]
        live_snaps = cur.execute(
            "SELECT COUNT(*) FROM snapshots WHERE source='live'"
        ).fetchone()[0]
        backfill_markets = cur.execute(
            "SELECT COUNT(*) FROM markets WHERE source='backfill'"
        ).fetchone()[0]
        return {
            "markets": markets,
            "resolved_markets": resolved,
            "meta_ok_markets": meta_ok,
            "snapshots": snaps,
            "live_snapshots": live_snaps,
            "labeled_snapshots": labeled,
            "label_mismatch": mismatch,
            "backfill_markets": backfill_markets,
            "resolved_per_combo": per_combo,
        }

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
