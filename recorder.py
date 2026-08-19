"""P2.5 SQLite recorder for markets, features, shadow forecasts and labels.

Official Polymarket resolution is the authoritative label.  A local computed result
is an audit: MATCH is ideal, OFFICIAL_ONLY is usable when the audit source is not
available, and MISMATCH is excluded from training/calibration.  Forecast and model
updates are deduplicated so restarts cannot learn the same market twice.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from typing import Optional

from models import FeatureSnapshot, LabelStatus, MarketRef

log = logging.getLogger("direction_engine.recorder")

ELIGIBLE_LABEL_STATUSES = ("MATCH", "OFFICIAL_ONLY")


class Recorder:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
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
                resolution_symbol TEXT,
                meta_ok           INTEGER NOT NULL DEFAULT 0,
                resolved          INTEGER NOT NULL DEFAULT 0,
                resolved_outcome  TEXT,
                official_result   TEXT,
                official_result_source TEXT,
                official_resolved_at REAL,
                computed_result   TEXT,
                computed_result_source TEXT,
                computed_result_time REAL,
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
                resolution_symbol TEXT,
                official_reference_open REAL,
                official_reference_open_time REAL,
                official_reference_source TEXT,
                proxy_reference_open REAL,
                proxy_reference_open_time REAL,
                proxy_reference_source TEXT,
                official_distance_bps REAL,
                proxy_distance_bps REAL,
                reference_current REAL,
                reference_current_time REAL,
                extra_json        TEXT,
                final_result      TEXT,
                UNIQUE(condition_id, checkpoint_sec)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS forecasts (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                condition_id        TEXT NOT NULL,
                combo_key           TEXT NOT NULL,
                checkpoint_sec      INTEGER NOT NULL,
                ts                  REAL NOT NULL,
                forecast_version    TEXT NOT NULL,
                model_version       TEXT NOT NULL,
                model_source        TEXT,
                model_schema_hash   TEXT,
                p_up_raw            REAL,
                p_up_calibrated     REAL,
                p_up_no_clob        REAL,
                baseline_coinflip   REAL,
                baseline_ptb        REAL,
                market_implied_up   REAL,
                calibration_ready   INTEGER NOT NULL DEFAULT 0,
                calibration_source  TEXT,
                calibration_markets INTEGER NOT NULL DEFAULT 0,
                threshold_ready     INTEGER NOT NULL DEFAULT 0,
                threshold_source    TEXT,
                decision_margin     REAL,
                decision            TEXT NOT NULL,
                abstain_reason       TEXT,
                confidence          REAL,
                predictability      REAL,
                regime              TEXT,
                direction_score     REAL,
                agreement           REAL,
                conflict            REAL,
                data_ready          INTEGER NOT NULL DEFAULT 0,
                feature_ready       INTEGER NOT NULL DEFAULT 0,
                feature_coverage    REAL,
                quality_status      TEXT,
                why_json            TEXT,
                diagnostics_json    TEXT,
                final_result        TEXT,
                correct             INTEGER,
                UNIQUE(condition_id, checkpoint_sec, model_version, forecast_version)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS model_updates (
                condition_id TEXT NOT NULL,
                model_version TEXT NOT NULL,
                combo_key TEXT NOT NULL,
                label TEXT NOT NULL,
                feature_rows INTEGER NOT NULL,
                updated_ts REAL NOT NULL,
                PRIMARY KEY(condition_id, model_version)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS calibration_updates (
                condition_id TEXT NOT NULL,
                calibration_version TEXT NOT NULL,
                model_version TEXT NOT NULL,
                combo_key TEXT NOT NULL,
                forecast_rows INTEGER NOT NULL,
                updated_ts REAL NOT NULL,
                PRIMARY KEY(condition_id, calibration_version, model_version)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_snap_cond ON snapshots(condition_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_snap_combo ON snapshots(combo_key)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_fc_cond ON forecasts(condition_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_fc_combo ON forecasts(combo_key, checkpoint_sec)")
        self.conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """Idempotently add columns missing from older P1 databases."""
        wanted = {
            "markets": [
                ("market_id", "TEXT"), ("market_start", "REAL"), ("market_end", "REAL"),
                ("time_status", "TEXT"), ("official_result", "TEXT"),
                ("computed_result", "TEXT"), ("label_status", "TEXT"),
                ("source", "TEXT DEFAULT 'live'"), ("resolution_symbol", "TEXT"),
                ("official_result_source", "TEXT"), ("official_resolved_at", "REAL"),
                ("computed_result_source", "TEXT"), ("computed_result_time", "REAL"),
            ],
            "snapshots": [
                ("market_start", "REAL"), ("market_end", "REAL"), ("tte_sec", "REAL"),
                ("checkpoint_sec", "INTEGER"), ("up_bid", "REAL"), ("up_ask", "REAL"),
                ("down_bid", "REAL"), ("down_ask", "REAL"), ("transport_age_ms", "REAL"),
                ("source_age_ms", "REAL"), ("quality_status", "TEXT"),
                ("source", "TEXT DEFAULT 'live'"), ("resolution_symbol", "TEXT"),
                ("official_reference_open", "REAL"), ("official_reference_open_time", "REAL"),
                ("official_reference_source", "TEXT"), ("proxy_reference_open", "REAL"),
                ("proxy_reference_open_time", "REAL"), ("proxy_reference_source", "TEXT"),
                ("official_distance_bps", "REAL"), ("proxy_distance_bps", "REAL"),
                ("reference_current", "REAL"), ("reference_current_time", "REAL"),
            ],
        }
        cur = self.conn.cursor()
        for table, columns in wanted.items():
            existing = {row[1] for row in cur.execute(f"PRAGMA table_info({table})")}
            for name, declaration in columns:
                if name not in existing:
                    try:
                        cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
                    except sqlite3.DatabaseError:
                        pass
        self.conn.commit()

    def record_market(self, ref: MarketRef, source: str = "live") -> None:
        if not ref.condition_id:
            return
        meta_ok = 1 if ref.has_resolution_meta else 0
        self.conn.execute(
            """
            INSERT INTO markets (
                condition_id, market_id, combo_key, asset, horizon, slug, question,
                market_start, market_end, time_status, start_ts, end_ts,
                resolution_source, resolution_type, resolution_symbol,
                meta_ok, source, discovered_ts
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(condition_id) DO UPDATE SET
                slug=excluded.slug,
                question=excluded.question,
                market_start=excluded.market_start,
                market_end=excluded.market_end,
                time_status=excluded.time_status,
                start_ts=excluded.start_ts,
                end_ts=excluded.end_ts,
                resolution_source=excluded.resolution_source,
                resolution_type=excluded.resolution_type,
                resolution_symbol=excluded.resolution_symbol,
                meta_ok=excluded.meta_ok
            """,
            (
                ref.condition_id, ref.market_id, ref.combo.key,
                ref.combo.asset.value, ref.combo.horizon.value, ref.slug, ref.question,
                ref.market_start_ts, ref.market_end_ts, ref.time_status.value,
                ref.start_ts, ref.end_ts, ref.resolution_source,
                ref.resolution_type.value, ref.resolution_symbol, meta_ok, source,
                ref.discovered_ts,
            ),
        )
        self.conn.commit()

    def backfill_market(self, ref: MarketRef) -> None:
        self.record_market(ref, source="backfill")
        if ref.resolved:
            self.settle(ref)

    def record_snapshot(
        self, ref: MarketRef, snap: FeatureSnapshot, checkpoint: Optional[int]
    ) -> bool:
        extra_json = json.dumps(snap.extra, separators=(",", ":")) if snap.extra else None
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO snapshots (
                condition_id, combo_key, checkpoint_sec, ts, market_start, market_end,
                tte_sec, seconds_remaining, spot_price, reference_price, distance_usd,
                distance_bps, up_bid, up_ask, up_mid, down_bid, down_ask, down_mid,
                clob_spread, spot_age_ms, book_age_ms, transport_age_ms, source_age_ms,
                clob_age_ms, reference_age_ms, quality_status, source, resolution_symbol,
                official_reference_open, official_reference_open_time,
                official_reference_source, proxy_reference_open,
                proxy_reference_open_time, proxy_reference_source,
                official_distance_bps, proxy_distance_bps, reference_current,
                reference_current_time, extra_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ref.condition_id, snap.combo.key, checkpoint, snap.ts,
                snap.market_start, snap.market_end, snap.tte_sec, snap.seconds_remaining,
                snap.spot_price, snap.reference_price, snap.distance_usd, snap.distance_bps,
                snap.up_bid, snap.up_ask, snap.up_mid, snap.down_bid, snap.down_ask,
                snap.down_mid, snap.clob_spread, snap.spot_age_ms, snap.book_age_ms,
                snap.transport_age_ms, snap.source_age_ms, snap.clob_age_ms,
                snap.reference_age_ms, snap.quality_status, "live", snap.resolution_symbol,
                snap.official_reference_open, snap.official_reference_open_time,
                snap.official_reference_source, snap.proxy_reference_open,
                snap.proxy_reference_open_time, snap.proxy_reference_source,
                snap.official_distance_bps, snap.proxy_distance_bps,
                snap.reference_current, snap.reference_current_time, extra_json,
            ),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def record_forecast(
        self,
        ref: MarketRef,
        checkpoint: int,
        record: dict,
        *,
        feature_coverage: Optional[float],
        quality_status: str,
    ) -> bool:
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO forecasts (
                condition_id, combo_key, checkpoint_sec, ts, forecast_version,
                model_version, model_source, model_schema_hash, p_up_raw,
                p_up_calibrated, p_up_no_clob, baseline_coinflip, baseline_ptb,
                market_implied_up, calibration_ready, calibration_source,
                calibration_markets, threshold_ready, threshold_source,
                decision_margin, decision, abstain_reason, confidence,
                predictability, regime, direction_score, agreement, conflict,
                data_ready, feature_ready, feature_coverage, quality_status,
                why_json, diagnostics_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ref.condition_id, ref.combo.key, checkpoint,
                float(record.get("ts") or time.time()),
                record.get("forecast_version") or "unknown",
                record.get("model_version") or "unknown",
                record.get("model_source"), record.get("model_schema_hash"),
                record.get("p_up_raw"), record.get("p_up_calibrated"),
                record.get("p_up_no_clob"), record.get("baseline_coinflip"),
                record.get("baseline_ptb"), record.get("market_implied_up"),
                int(bool(record.get("calibration_ready"))),
                record.get("calibration_source"),
                int(record.get("calibration_markets") or 0),
                int(bool(record.get("threshold_ready"))),
                record.get("threshold_source"), record.get("decision_margin"),
                record.get("decision") or "ABSTAIN", record.get("abstain_reason"),
                record.get("confidence"), record.get("predictability"),
                record.get("regime"), record.get("direction_score"),
                record.get("agreement"), record.get("conflict"),
                int(bool(record.get("data_ready"))),
                int(bool(record.get("feature_ready"))), feature_coverage,
                quality_status,
                json.dumps(record.get("why") or [], separators=(",", ":")),
                json.dumps({
                    "regime": record.get("regime_diagnostics") or {},
                    "pipeline": record.get("diagnostics") or [],
                }, separators=(",", ":")),
            ),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def settle(self, ref: MarketRef) -> Optional[str]:
        """Persist official label and computed audit; return label status."""
        if not ref.condition_id or not ref.resolved:
            return None
        official = ref.official_result or ref.resolved_outcome
        if official is None:
            return None
        official_value = official.value
        computed = ref.computed_result.value if ref.computed_result else None
        if computed is None:
            label_status = "OFFICIAL_ONLY"
        elif computed == official_value:
            label_status = "MATCH"
        else:
            label_status = "MISMATCH"

        self.conn.execute(
            """
            UPDATE markets SET
                resolved=1,
                resolved_outcome=?,
                official_result=?,
                official_result_source=?,
                official_resolved_at=?,
                computed_result=?,
                computed_result_source=?,
                computed_result_time=?,
                label_status=?
            WHERE condition_id=?
            """,
            (
                official_value, official_value, ref.official_result_source,
                ref.official_resolved_at or time.time(), computed,
                ref.computed_result_source, ref.computed_result_time,
                label_status, ref.condition_id,
            ),
        )
        if label_status in ELIGIBLE_LABEL_STATUSES:
            self.conn.execute(
                "UPDATE snapshots SET final_result=? WHERE condition_id=?",
                (official_value, ref.condition_id),
            )
            self.conn.execute(
                """
                UPDATE forecasts SET
                    final_result=?,
                    correct=CASE
                        WHEN decision='UP' THEN CASE WHEN ?='UP' THEN 1 ELSE 0 END
                        WHEN decision='DOWN' THEN CASE WHEN ?='DOWN' THEN 1 ELSE 0 END
                        ELSE NULL
                    END
                WHERE condition_id=?
                """,
                (official_value, official_value, official_value, ref.condition_id),
            )
        else:
            self.conn.execute(
                "UPDATE snapshots SET final_result=NULL WHERE condition_id=?",
                (ref.condition_id,),
            )
            self.conn.execute(
                "UPDATE forecasts SET final_result=NULL, correct=NULL WHERE condition_id=?",
                (ref.condition_id,),
            )
        self.conn.commit()
        try:
            ref.label_status = LabelStatus(label_status)
        except ValueError:
            # models.LabelStatus from P1 has no OFFICIAL_ONLY member; DB remains exact.
            ref.label_status = LabelStatus.UNKNOWN
        log.info(
            "settled %s official=%s(%s) computed=%s(%s) label=%s",
            ref.combo.key, official_value, ref.official_result_source, computed,
            ref.computed_result_source, label_status,
        )
        return label_status

    def feature_rows(self, condition_id: str) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT combo_key, checkpoint_sec, ts, tte_sec, seconds_remaining,
                   extra_json, final_result
            FROM snapshots
            WHERE condition_id=? AND extra_json IS NOT NULL
            ORDER BY checkpoint_sec DESC
            """,
            (condition_id,),
        ).fetchall()
        output: list[dict] = []
        for row in rows:
            try:
                payload = json.loads(row["extra_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            output.append({
                "combo_key": row["combo_key"],
                "checkpoint_sec": row["checkpoint_sec"],
                "ts": row["ts"],
                "tte_sec": row["tte_sec"] or row["seconds_remaining"],
                "features": payload,
                "final_result": row["final_result"],
            })
        return output

    def forecast_rows(self, condition_id: str, model_version: Optional[str] = None) -> list[dict]:
        sql = "SELECT * FROM forecasts WHERE condition_id=?"
        params: list[object] = [condition_id]
        if model_version is not None:
            sql += " AND model_version=?"
            params.append(model_version)
        sql += " ORDER BY checkpoint_sec DESC"
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def pending_model_updates(self, model_version: str) -> list[dict]:
        placeholders = ",".join("?" for _ in ELIGIBLE_LABEL_STATUSES)
        rows = self.conn.execute(
            f"""
            SELECT m.condition_id, m.combo_key, m.official_result, m.label_status
            FROM markets m
            LEFT JOIN model_updates u
              ON u.condition_id=m.condition_id AND u.model_version=?
            WHERE m.resolved=1
              AND m.official_result IN ('UP','DOWN')
              AND m.label_status IN ({placeholders})
              AND u.condition_id IS NULL
            ORDER BY m.market_end, m.condition_id
            """,
            (model_version, *ELIGIBLE_LABEL_STATUSES),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_model_updated(
        self, condition_id: str, model_version: str, combo_key: str,
        label: str, feature_rows: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO model_updates
                (condition_id, model_version, combo_key, label, feature_rows, updated_ts)
            VALUES (?,?,?,?,?,?)
            """,
            (condition_id, model_version, combo_key, label, feature_rows, time.time()),
        )
        self.conn.commit()

    def pending_calibration_updates(
        self, calibration_version: str, model_version: str
    ) -> list[dict]:
        placeholders = ",".join("?" for _ in ELIGIBLE_LABEL_STATUSES)
        rows = self.conn.execute(
            f"""
            SELECT m.condition_id, m.combo_key, m.official_result, m.label_status
            FROM markets m
            LEFT JOIN calibration_updates u
              ON u.condition_id=m.condition_id
             AND u.calibration_version=?
             AND u.model_version=?
            WHERE m.resolved=1
              AND m.official_result IN ('UP','DOWN')
              AND m.label_status IN ({placeholders})
              AND u.condition_id IS NULL
            ORDER BY m.market_end, m.condition_id
            """,
            (calibration_version, model_version, *ELIGIBLE_LABEL_STATUSES),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_calibration_updated(
        self, condition_id: str, calibration_version: str, model_version: str,
        combo_key: str, forecast_rows: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO calibration_updates
                (condition_id, calibration_version, model_version, combo_key,
                 forecast_rows, updated_ts)
            VALUES (?,?,?,?,?,?)
            """,
            (
                condition_id, calibration_version, model_version, combo_key,
                forecast_rows, time.time(),
            ),
        )
        self.conn.commit()

    @staticmethod
    def _probability_metrics(rows: list[sqlite3.Row], field: str) -> Optional[dict]:
        pairs: list[tuple[float, int]] = []
        for row in rows:
            value = row[field]
            outcome = row["final_result"]
            if value is None or outcome not in ("UP", "DOWN"):
                continue
            p = max(1e-6, min(1.0 - 1e-6, float(value)))
            y = 1 if outcome == "UP" else 0
            pairs.append((p, y))
        if not pairs:
            return None
        brier = sum((p - y) ** 2 for p, y in pairs) / len(pairs)
        log_loss = sum(
            -(y * __import__("math").log(p) + (1-y) * __import__("math").log(1-p))
            for p, y in pairs
        ) / len(pairs)
        return {"n": len(pairs), "brier": round(brier, 6), "log_loss": round(log_loss, 6)}

    def forecast_analytics(self, min_n: int = 30) -> dict:
        rows = self.conn.execute(
            "SELECT * FROM forecasts WHERE final_result IN ('UP','DOWN')"
        ).fetchall()
        decided = [row for row in rows if row["decision"] in ("UP", "DOWN")]
        correct = sum(int(row["correct"] or 0) for row in decided)
        overall = {
            "n_forecasts": len(rows),
            "n_decided": len(decided),
            "coverage": round(len(decided) / len(rows), 6) if rows else 0.0,
            "insufficient": len(rows) < min_n,
            "accuracy": (
                round(correct / len(decided), 6) if len(decided) >= min_n else None
            ),
            "model_raw": self._probability_metrics(rows, "p_up_raw"),
            "model_calibrated": self._probability_metrics(rows, "p_up_calibrated"),
            "model_no_clob": self._probability_metrics(rows, "p_up_no_clob"),
            "coinflip": self._probability_metrics(rows, "baseline_coinflip"),
            "ptb_diffusion": self._probability_metrics(rows, "baseline_ptb"),
            "market_implied": self._probability_metrics(rows, "market_implied_up"),
        }
        per_combo: dict[str, dict] = {}
        combos = sorted({str(row["combo_key"]) for row in rows})
        for combo in combos:
            subset = [row for row in rows if row["combo_key"] == combo]
            decisions = [row for row in subset if row["decision"] in ("UP", "DOWN")]
            wins = sum(int(row["correct"] or 0) for row in decisions)
            per_combo[combo] = {
                "n": len(subset),
                "decided": len(decisions),
                "coverage": round(len(decisions) / len(subset), 6) if subset else 0.0,
                "accuracy": (
                    round(wins / len(decisions), 6)
                    if len(decisions) >= min_n else None
                ),
                "insufficient": len(subset) < min_n,
                "brier": self._probability_metrics(subset, "p_up_calibrated"),
            }
        return {"overall": overall, "per_combo": per_combo, "min_n": min_n}

    def stats(self) -> dict:
        cur = self.conn.cursor()
        scalar = lambda sql: cur.execute(sql).fetchone()[0]  # noqa: E731
        per_combo = dict(cur.execute(
            "SELECT combo_key, COUNT(*) FROM markets WHERE resolved=1 GROUP BY combo_key"
        ).fetchall())
        return {
            "markets": scalar("SELECT COUNT(*) FROM markets"),
            "resolved_markets": scalar("SELECT COUNT(*) FROM markets WHERE resolved=1"),
            "meta_ok_markets": scalar("SELECT COUNT(*) FROM markets WHERE meta_ok=1"),
            "snapshots": scalar("SELECT COUNT(*) FROM snapshots"),
            "live_snapshots": scalar("SELECT COUNT(*) FROM snapshots WHERE source='live'"),
            "labeled_snapshots": scalar("SELECT COUNT(*) FROM snapshots WHERE final_result IS NOT NULL"),
            "label_mismatch": scalar("SELECT COUNT(*) FROM markets WHERE label_status='MISMATCH'"),
            "official_only": scalar("SELECT COUNT(*) FROM markets WHERE label_status='OFFICIAL_ONLY'"),
            "backfill_markets": scalar("SELECT COUNT(*) FROM markets WHERE source='backfill'"),
            "forecasts": scalar("SELECT COUNT(*) FROM forecasts"),
            "labeled_forecasts": scalar("SELECT COUNT(*) FROM forecasts WHERE final_result IS NOT NULL"),
            "decided_forecasts": scalar("SELECT COUNT(*) FROM forecasts WHERE decision IN ('UP','DOWN')"),
            "model_updates": scalar("SELECT COUNT(*) FROM model_updates"),
            "calibration_updates": scalar("SELECT COUNT(*) FROM calibration_updates"),
            "resolved_per_combo": per_combo,
        }

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
