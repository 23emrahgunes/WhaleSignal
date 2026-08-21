"""Canonical one-row-per-market dataset extractor for P2.6.

The extractor opens the P2.5 database read-only and writes only to the isolated
P2.6 research database.  It selects the actual snapshot emitted immediately after
one canonical checkpoint (5m T-60, 15m T-240, 1h T-600), records the capture lag
and source lineage, and stores official labels separately.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from p26_config import P26Settings
from p26_oracle_store import OracleTickStore
from p26_features import (
    EXTERNAL_FEATURE_NAMES,
    assert_external_only,
    schema_hash as external_schema_hash,
)
from p26_schema import connect_p26, ensure_p26_schema


FEATURE_SCHEMA_VERSION = "P26_EXTERNAL_FEATURES_V1"
EXTRACTION_POLICY_VERSION = "P26_CANONICAL_V1"

EXTERNAL_FEATURE_WHITELIST = EXTERNAL_FEATURE_NAMES
assert_external_feature_isolation = assert_external_only


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def feature_schema_hash(
    names: Iterable[str] = EXTERNAL_FEATURE_WHITELIST,
    version: str = FEATURE_SCHEMA_VERSION,
) -> str:
    return external_schema_hash(names, version)


def current_code_commit() -> str:
    explicit = os.getenv("P26_CODE_COMMIT")
    if explicit:
        return explicit
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return "UNKNOWN"


def open_p25_read_only(path: str) -> sqlite3.Connection:
    resolved = Path(path).resolve()
    conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _ms_from_seconds(value: object) -> Optional[int]:
    if value is None:
        return None
    return int(round(float(value) * 1000.0))


def _source_ts_from_age(decision_ts_ms: int, age_ms: object) -> Optional[int]:
    if age_ms is None:
        return None
    age = float(age_ms)
    # Preserve negative ages as an explicit future timestamp so the lineage
    # invariant can reject the row instead of silently treating it as missing.
    return int(round(decision_ts_ms - age))


def _label(value: object) -> Optional[int]:
    if value is None:
        return None
    normalized = str(value).strip().upper()
    if normalized == "UP":
        return 1
    if normalized == "DOWN":
        return 0
    return None


def _computed_status(value: object) -> str:
    normalized = str(value or "UNKNOWN").strip().upper()
    return normalized if normalized in {"MATCH", "MISMATCH", "UNKNOWN"} else "UNKNOWN"


@dataclass(frozen=True)
class CanonicalExtractionResult:
    scanned: int = 0
    inserted: int = 0
    duplicate: int = 0
    rejected_lag: int = 0
    rejected_checkpoint: int = 0
    invalid_features: int = 0
    complete_lineage: int = 0
    partial_lineage: int = 0
    labels_upserted: int = 0
    label_markets_scanned: int = 0
    snapshot_cursor_from: int = 0
    snapshot_cursor_to: int = 0

    def plus(self, **changes: int) -> "CanonicalExtractionResult":
        values = self.__dict__.copy()
        for key, value in changes.items():
            values[key] = values.get(key, 0) + value
        return CanonicalExtractionResult(**values)


class CanonicalDatasetBuilder:
    def __init__(
        self,
        settings: P26Settings,
        *,
        code_commit: Optional[str] = None,
    ) -> None:
        self.settings = settings
        self.code_commit = code_commit or current_code_commit()
        self.p26 = connect_p26(settings.p26_db_path)
        ensure_p26_schema(self.p26)
        self.oracle = OracleTickStore(settings.p26_db_path)
        self._schema_hash = feature_schema_hash(
            EXTERNAL_FEATURE_WHITELIST,
            settings.feature_schema_version,
        )
        self._cursor_key = (
            "dataset_snapshot_cursor:"
            f"{settings.extraction_policy_version}:{self._schema_hash}"
        )
        self._label_sync_key = "dataset_labels_last_scan_ms"

    def _meta_int(self, key: str, default: int = 0) -> int:
        row = self.p26.execute(
            "SELECT value FROM p26_meta WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            return int(default)
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return int(default)

    def _set_meta_int(self, key: str, value: int) -> None:
        self.p26.execute(
            """
            INSERT INTO p26_meta(key,value,updated_at_ms)
            VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value, updated_at_ms=excluded.updated_at_ms
            """,
            (key, str(int(value)), int(time.time() * 1000)),
        )

    def _initial_snapshot_cursor(self) -> int:
        row = self.p26.execute(
            "SELECT MAX(source_snapshot_id) FROM p26_canonical_rows"
        ).fetchone()
        return int(row[0] or 0)

    def _required_tables(self, conn: sqlite3.Connection) -> None:
        existing = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        required = {"markets", "snapshots"}
        missing = required - existing
        if missing:
            raise RuntimeError(f"P2.5 database missing tables: {sorted(missing)}")

    def _checkpoint_for_horizon(self, horizon: str) -> int:
        return self.settings.canonical_checkpoint(horizon)

    def _snapshot_highwater(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT MAX(id) FROM snapshots").fetchone()
        return int(row[0] or 0)

    def _scan_rows(
        self,
        conn: sqlite3.Connection,
        *,
        after_snapshot_id: int,
        through_snapshot_id: int,
    ) -> list[sqlite3.Row]:
        return conn.execute(
            """
            SELECT
                s.id AS snapshot_id,
                s.condition_id,
                s.combo_key,
                s.checkpoint_sec,
                s.ts,
                s.market_start,
                s.market_end,
                s.tte_sec,
                s.extra_json,
                s.quality_status,
                s.source_age_ms,
                s.book_age_ms,
                s.clob_age_ms,
                s.up_bid,s.up_ask,s.up_mid,s.down_bid,s.down_ask,s.down_mid,
                s.clob_spread,
                m.market_id,m.slug,m.asset,m.horizon,
                m.official_result,m.official_result_source,m.official_resolved_at,
                m.computed_result,m.label_status
            FROM snapshots s
            JOIN markets m ON m.condition_id=s.condition_id
            WHERE s.id > ?
              AND s.id <= ?
              AND s.extra_json IS NOT NULL
              AND (
                    (m.horizon='5m'  AND s.checkpoint_sec=?)
                 OR (m.horizon='15m' AND s.checkpoint_sec=?)
                 OR (m.horizon='1h'  AND s.checkpoint_sec=?)
              )
            ORDER BY s.ts ASC,s.id ASC
            LIMIT ?
            """
            ,
            (
                int(after_snapshot_id), int(through_snapshot_id),
                self.settings.canonical_checkpoints_5m,
                self.settings.canonical_checkpoints_15m,
                self.settings.canonical_checkpoints_1h,
                self.settings.dataset_max_snapshot_batch,
            ),
        ).fetchall()

    def _scan_labels(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return conn.execute(
            """
            SELECT condition_id,official_result,official_result_source,
                   official_resolved_at,computed_result,label_status
            FROM markets
            WHERE official_result IS NOT NULL
               OR computed_result IS NOT NULL
               OR UPPER(COALESCE(label_status,'UNKNOWN')) <> 'UNKNOWN'
            ORDER BY condition_id
            """
        ).fetchall()

    def _lineage(
        self,
        row: sqlite3.Row,
        decision_ts_ms: int,
    ) -> tuple[dict[str, Any], str, bool, Optional[int]]:
        trade_ts = _source_ts_from_age(decision_ts_ms, row["source_age_ms"])
        book_ts = _source_ts_from_age(decision_ts_ms, row["book_age_ms"])
        clob_ts = _source_ts_from_age(decision_ts_ms, row["clob_age_ms"])
        tick = self.oracle.at_or_before(
            str(row["asset"]),
            decision_ts_ms,
            max_age_ms=max(60_000, self.settings.canonical_max_lag_ms * 10),
        )
        chainlink_ts = tick.source_ts_ms if tick else None
        source_values = [ts for ts in (trade_ts, book_ts, clob_ts, chainlink_ts) if ts is not None]
        max_source = max(source_values) if source_values else None
        no_future = all(ts <= decision_ts_ms for ts in source_values)
        complete = all(ts is not None for ts in (trade_ts, book_ts, clob_ts, chainlink_ts))
        if not no_future:
            status = "FUTURE_DATA_REJECTED"
        elif complete:
            status = "COMPLETE_DERIVED_AGE"
        else:
            status = "PARTIAL_LEGACY"
        lineage = {
            "decision_ts_ms": decision_ts_ms,
            "binance_trade_ts_ms": trade_ts,
            "binance_book_ts_ms": book_ts,
            "clob_quote_ts_ms": clob_ts,
            "chainlink_tick_id": tick.id if tick else None,
            "chainlink_source_ts_ms": chainlink_ts,
            "max_source_event_ts_ms": max_source,
            "source_ages_ms": {
                "binance_source": row["source_age_ms"],
                "binance_book": row["book_age_ms"],
                "clob_quote": row["clob_age_ms"],
            },
            "timestamp_method": "decision_ts_minus_recorded_age",
            "no_future": no_future,
        }
        return lineage, status, complete and no_future, tick.id if tick else None

    def _feature_payload(self, row: sqlite3.Row) -> tuple[dict[str, float], dict[str, Any]]:
        raw = json.loads(str(row["extra_json"]))
        features = {
            name: float(raw.get(name, 0.0) or 0.0)
            for name in EXTERNAL_FEATURE_WHITELIST
        }
        metadata = {
            "feature_ready": bool(raw.get("feature_ready")),
            "feature_coverage": float(raw.get("feature_coverage", 0.0) or 0.0),
            "missing_features": list(raw.get("missing_features") or []),
        }
        return features, metadata

    def _insert_row(self, row: sqlite3.Row) -> tuple[str, str]:
        horizon = str(row["horizon"])
        expected_checkpoint = self._checkpoint_for_horizon(horizon)
        checkpoint = int(row["checkpoint_sec"])
        if checkpoint != expected_checkpoint:
            return "REJECTED_CHECKPOINT", ""
        market_end_ms = _ms_from_seconds(row["market_end"])
        market_start_ms = _ms_from_seconds(row["market_start"])
        if market_end_ms is None or market_start_ms is None:
            return "INVALID_TIME", ""
        nominal_target = market_end_ms - checkpoint * 1000
        decision_ts = _ms_from_seconds(row["ts"])
        if decision_ts is None:
            return "INVALID_TIME", ""
        lag = decision_ts - nominal_target
        if lag < 0 or lag > self.settings.canonical_max_lag_ms:
            return "REJECTED_LAG", ""

        try:
            features, feature_meta = self._feature_payload(row)
        except (ValueError, TypeError, json.JSONDecodeError):
            return "INVALID_FEATURES", ""
        vector_json = canonical_json(features)
        vector_hash = sha256_text(vector_json)
        names_json = canonical_json(list(EXTERNAL_FEATURE_WHITELIST))
        lineage, lineage_status, complete, tick_id = self._lineage(row, decision_ts)
        max_source = lineage["max_source_event_ts_ms"]
        no_future = bool(lineage["no_future"])
        training_eligible = int(
            bool(feature_meta["feature_ready"])
            and complete
            and no_future
            and not feature_meta["missing_features"]
        )
        quality_status = str(row["quality_status"] or "UNKNOWN")
        lineage.update(
            {
                "nominal_target_ts_ms": nominal_target,
                "capture_lag_ms": lag,
                "source_snapshot_id": int(row["snapshot_id"]),
                "feature_schema_hash": self._schema_hash,
                "feature_schema_version": self.settings.feature_schema_version,
                "extraction_policy_version": self.settings.extraction_policy_version,
                "code_commit": self.code_commit,
                "feature_meta": feature_meta,
            }
        )

        before = self.p26.total_changes
        self.p26.execute(
            """
            INSERT OR IGNORE INTO p26_canonical_rows(
                condition_id,market_id,slug,combo_key,asset,horizon,
                market_start_ts_ms,market_end_ts_ms,checkpoint_sec,
                nominal_target_ts_ms,decision_ts_ms,capture_lag_ms,
                source_snapshot_id,feature_vector_json,feature_names_json,
                feature_vector_sha256,feature_schema_version,feature_schema_hash,
                extraction_policy_version,binance_trade_ts_ms,binance_book_ts_ms,
                clob_quote_ts_ms,chainlink_tick_id,chainlink_source_ts_ms,
                max_source_event_ts_ms,up_bid,up_ask,up_mid,down_bid,down_ask,
                down_mid,clob_spread,clob_age_ms,source_age_ms,book_age_ms,
                quality_status,lineage_status,training_eligible,lineage_json,
                code_commit,created_at_ms
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row["condition_id"],row["market_id"],row["slug"],row["combo_key"],
                row["asset"],horizon,market_start_ms,market_end_ms,checkpoint,
                nominal_target,decision_ts,lag,int(row["snapshot_id"]),vector_json,
                names_json,vector_hash,self.settings.feature_schema_version,
                self._schema_hash,self.settings.extraction_policy_version,
                lineage["binance_trade_ts_ms"],lineage["binance_book_ts_ms"],
                lineage["clob_quote_ts_ms"],tick_id,lineage["chainlink_source_ts_ms"],
                max_source,row["up_bid"],row["up_ask"],row["up_mid"],row["down_bid"],
                row["down_ask"],row["down_mid"],row["clob_spread"],row["clob_age_ms"],
                row["source_age_ms"],row["book_age_ms"],quality_status,lineage_status,
                training_eligible,canonical_json(lineage),self.code_commit,
                int(time.time()*1000),
            ),
        )
        created = self.p26.total_changes > before
        return ("INSERTED" if created else "DUPLICATE"), lineage_status

    def _upsert_label(self, row: sqlite3.Row) -> bool:
        official = _label(row["official_result"])
        computed = _label(row["computed_result"])
        status = _computed_status(row["label_status"])
        if official is None and computed is None and status == "UNKNOWN":
            return False
        existing = self.p26.execute(
            "SELECT * FROM p26_labels WHERE condition_id=?",
            (row["condition_id"],),
        ).fetchone()
        resolved_at = _ms_from_seconds(row["official_resolved_at"])
        official_source = row["official_result_source"]
        if existing is not None:
            merged = (
                official if official is not None else existing["official_label"],
                official_source if official_source is not None else existing["official_result_source"],
                resolved_at if resolved_at is not None else existing["official_resolved_at_ms"],
                computed if computed is not None else existing["computed_result"],
                status,
            )
            current = (
                existing["official_label"], existing["official_result_source"],
                existing["official_resolved_at_ms"], existing["computed_result"],
                existing["computed_status"],
            )
            if merged == current:
                return False
        self.p26.execute(
            """
            INSERT INTO p26_labels(
                condition_id,official_label,official_result_source,
                official_resolved_at_ms,computed_result,computed_status,updated_at_ms
            ) VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(condition_id) DO UPDATE SET
                official_label=COALESCE(excluded.official_label,p26_labels.official_label),
                official_result_source=COALESCE(excluded.official_result_source,p26_labels.official_result_source),
                official_resolved_at_ms=COALESCE(excluded.official_resolved_at_ms,p26_labels.official_resolved_at_ms),
                computed_result=COALESCE(excluded.computed_result,p26_labels.computed_result),
                computed_status=excluded.computed_status,
                updated_at_ms=excluded.updated_at_ms
            """,
            (
                row["condition_id"],official,official_source,
                resolved_at,computed,status,
                int(time.time()*1000),
            ),
        )
        return True

    def _labels_due(self, now_ms: int) -> bool:
        last_ms = self._meta_int(self._label_sync_key, 0)
        return now_ms - last_ms >= self.settings.dataset_label_sync_interval_sec * 1000

    def sync(self) -> CanonicalExtractionResult:
        now_ms = int(time.time() * 1000)
        p25 = open_p25_read_only(self.settings.p25_db_path)
        try:
            self._required_tables(p25)
            p25.execute("BEGIN")
            highwater = self._snapshot_highwater(p25)
            cursor = self._meta_int(self._cursor_key, self._initial_snapshot_cursor())
            rows = self._scan_rows(
                p25,
                after_snapshot_id=cursor,
                through_snapshot_id=highwater,
            )
            advance_to = (
                int(rows[-1]["snapshot_id"])
                if len(rows) >= self.settings.dataset_max_snapshot_batch
                else highwater
            )
            label_rows = self._scan_labels(p25) if self._labels_due(now_ms) else []
            p25.rollback()
            result = CanonicalExtractionResult(
                snapshot_cursor_from=cursor,
                snapshot_cursor_to=cursor,
            )
            for row in rows:
                result = result.plus(scanned=1)
                status, lineage_status = self._insert_row(row)
                if status == "INSERTED":
                    result = result.plus(inserted=1)
                    if lineage_status == "COMPLETE_DERIVED_AGE":
                        result = result.plus(complete_lineage=1)
                    else:
                        result = result.plus(partial_lineage=1)
                elif status == "DUPLICATE":
                    result = result.plus(duplicate=1)
                elif status == "REJECTED_LAG":
                    result = result.plus(rejected_lag=1)
                elif status == "REJECTED_CHECKPOINT":
                    result = result.plus(rejected_checkpoint=1)
                elif status == "INVALID_FEATURES":
                    result = result.plus(invalid_features=1)
            self._set_meta_int(self._cursor_key, advance_to)
            result = result.plus(snapshot_cursor_to=advance_to - cursor)
            for row in label_rows:
                result = result.plus(label_markets_scanned=1)
                if self._upsert_label(row):
                    result = result.plus(labels_upserted=1)
            if label_rows:
                self._set_meta_int(self._label_sync_key, now_ms)
            self.p26.commit()
            return result
        finally:
            if p25.in_transaction:
                p25.rollback()
            p25.close()

    def canonical_rows(self, *, labeled_only: bool = False, eligible_only: bool = False) -> list[sqlite3.Row]:
        clauses: list[str] = []
        if labeled_only:
            clauses.append("l.official_label IS NOT NULL")
        if eligible_only:
            clauses.append("c.training_eligible=1")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return self.p26.execute(
            f"""
            SELECT c.*,l.official_label,l.official_result_source,l.computed_result,l.computed_status
            FROM p26_canonical_rows c
            LEFT JOIN p26_labels l ON l.condition_id=c.condition_id
            {where}
            ORDER BY c.decision_ts_ms ASC,c.condition_id ASC
            """
        ).fetchall()

    def close(self) -> None:
        self.oracle.close()
        self.p26.close()
