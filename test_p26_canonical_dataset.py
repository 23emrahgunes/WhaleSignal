from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from features import FeatureVector
from p26_config import P26Settings
from p26_dataset import (
    EXTERNAL_FEATURE_WHITELIST,
    CanonicalDatasetBuilder,
    assert_external_feature_isolation,
)
from p26_oracle_store import OracleTick, OracleTickStore


def _p25_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE markets(
            condition_id TEXT PRIMARY KEY,
            market_id TEXT,
            slug TEXT,
            combo_key TEXT,
            asset TEXT,
            horizon TEXT,
            market_start REAL,
            market_end REAL,
            official_result TEXT,
            official_result_source TEXT,
            official_resolved_at REAL,
            computed_result TEXT,
            label_status TEXT
        );
        CREATE TABLE snapshots(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            condition_id TEXT,
            combo_key TEXT,
            checkpoint_sec INTEGER,
            ts REAL,
            market_start REAL,
            market_end REAL,
            tte_sec REAL,
            extra_json TEXT,
            quality_status TEXT,
            source_age_ms REAL,
            book_age_ms REAL,
            clob_age_ms REAL,
            up_bid REAL,up_ask REAL,up_mid REAL,
            down_bid REAL,down_ask REAL,down_mid REAL,
            clob_spread REAL
        );
        """
    )


def _features(ready: bool = True) -> str:
    payload = {name: 0.1 for name in EXTERNAL_FEATURE_WHITELIST}
    payload.update(
        {
            "feature_ready": ready,
            "feature_coverage": 1.0 if ready else 0.5,
            "missing_features": [] if ready else ["ret_slow"],
            "clob_spread": 0.02,
            "up_mid": 0.6,
        }
    )
    return json.dumps(payload)


def _insert_market_and_snapshot(
    conn: sqlite3.Connection,
    *,
    condition_id: str,
    horizon: str,
    checkpoint: int,
    lag_ms: int,
    source_age_ms: float = 50.0,
    quality: str = "OK",
) -> None:
    duration = {"5m": 300, "15m": 900, "1h": 3600}[horizon]
    end = 1_800_000_000.0
    start = end - duration
    target = end - checkpoint
    asset = "BTC"
    conn.execute(
        "INSERT INTO markets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            condition_id,
            f"market-{condition_id}",
            f"btc-updown-{horizon}-{int(start)}",
            f"BTC:{horizon}",
            asset,
            horizon,
            start,
            end,
            "UP",
            "gamma:outcomePrices",
            end + 20,
            "UP",
            "MATCH",
        ),
    )
    conn.execute(
        """
        INSERT INTO snapshots(
            condition_id,combo_key,checkpoint_sec,ts,market_start,market_end,tte_sec,
            extra_json,quality_status,source_age_ms,book_age_ms,clob_age_ms,
            up_bid,up_ask,up_mid,down_bid,down_ask,down_mid,clob_spread
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            condition_id,
            f"BTC:{horizon}",
            checkpoint,
            target + lag_ms / 1000.0,
            start,
            end,
            checkpoint - lag_ms / 1000.0,
            _features(),
            quality,
            source_age_ms,
            75.0,
            100.0,
            0.55,
            0.57,
            0.56,
            0.43,
            0.45,
            0.44,
            0.02,
        ),
    )
    conn.commit()


def _settings(tmp_path: Path, p25: Path) -> P26Settings:
    return P26Settings(
        p25_db_path=str(p25),
        p26_db_path=str(tmp_path / "p26.sqlite"),
        canonical_max_lag_ms=2000,
    )


def test_canonical_extracts_one_row_and_separate_label(tmp_path):
    p25 = tmp_path / "p25.sqlite"
    conn = sqlite3.connect(p25)
    _p25_schema(conn)
    _insert_market_and_snapshot(
        conn,
        condition_id="c-btc-5m",
        horizon="5m",
        checkpoint=60,
        lag_ms=450,
    )
    conn.close()
    settings = _settings(tmp_path, p25)
    oracle = OracleTickStore(settings.p26_db_path)
    oracle.insert(
        OracleTick(
            asset="BTC",
            source="POLYMARKET_RTDS_CHAINLINK",
            value_text="100",
            value_real=100.0,
            source_ts_ms=1_799_999_939_900,
            recv_ts_ms=1_799_999_939_950,
            payload_sha256="tick1",
        )
    )
    oracle.close()

    builder = CanonicalDatasetBuilder(settings, code_commit="abc123")
    try:
        first = builder.sync()
        second = builder.sync()
        assert first.inserted == 1
        assert first.scanned == 1
        assert first.labels_upserted == 1
        assert second.scanned == 0
        assert second.duplicate == 0
        assert second.labels_upserted == 0
        rows = builder.canonical_rows(labeled_only=True)
        assert len(rows) == 1
        row = rows[0]
        assert row["checkpoint_sec"] == 60
        assert row["capture_lag_ms"] == 450
        assert row["official_label"] == 1
        assert row["computed_status"] == "MATCH"
        assert row["training_eligible"] == 1
        assert row["lineage_status"] == "COMPLETE_DERIVED_AGE"
        assert row["max_source_event_ts_ms"] <= row["decision_ts_ms"]
        names = json.loads(row["feature_names_json"])
        assert names == list(EXTERNAL_FEATURE_WHITELIST)
        assert all("clob" not in name.lower() for name in names)
    finally:
        builder.close()


def test_incremental_cursor_processes_only_new_canonical_snapshots(tmp_path):
    p25 = tmp_path / "p25.sqlite"
    conn = sqlite3.connect(p25)
    _p25_schema(conn)
    _insert_market_and_snapshot(
        conn, condition_id="first", horizon="5m", checkpoint=60, lag_ms=100
    )
    conn.close()
    settings = _settings(tmp_path, p25).model_copy(
        update={"dataset_label_sync_interval_sec": 3600}
    )
    oracle = OracleTickStore(settings.p26_db_path)
    oracle.insert(
        OracleTick(
            asset="BTC", source="POLYMARKET_RTDS_CHAINLINK",
            value_text="100", value_real=100.0,
            source_ts_ms=1_799_999_939_000,
            recv_ts_ms=1_799_999_939_010,
            payload_sha256="cursor-tick",
        )
    )
    oracle.close()

    builder = CanonicalDatasetBuilder(settings, code_commit="cursor")
    try:
        first = builder.sync()
        assert first.scanned == 1 and first.inserted == 1

        conn = sqlite3.connect(p25)
        _insert_market_and_snapshot(
            conn, condition_id="noncanonical", horizon="5m",
            checkpoint=240, lag_ms=100,
        )
        _insert_market_and_snapshot(
            conn, condition_id="second", horizon="5m",
            checkpoint=60, lag_ms=200,
        )
        conn.close()

        second = builder.sync()
        assert second.scanned == 1
        assert second.inserted == 1
        assert second.rejected_checkpoint == 0
        assert len(builder.canonical_rows()) == 2

        third = builder.sync()
        assert third.scanned == 0
        assert third.duplicate == 0
        assert third.snapshot_cursor_from == third.snapshot_cursor_to
    finally:
        builder.close()


def test_label_sync_is_separate_and_only_writes_changes(tmp_path):
    p25 = tmp_path / "p25.sqlite"
    conn = sqlite3.connect(p25)
    _p25_schema(conn)
    _insert_market_and_snapshot(
        conn, condition_id="label-row", horizon="5m",
        checkpoint=60, lag_ms=100,
    )
    conn.close()
    settings = _settings(tmp_path, p25).model_copy(
        update={"dataset_label_sync_interval_sec": 1}
    )
    builder = CanonicalDatasetBuilder(settings, code_commit="labels")
    try:
        first = builder.sync()
        assert first.label_markets_scanned == 1
        assert first.labels_upserted == 1
        builder._set_meta_int(builder._label_sync_key, 0)
        before = builder.p26.execute(
            "SELECT updated_at_ms FROM p26_labels WHERE condition_id='label-row'"
        ).fetchone()[0]
        second = builder.sync()
        after = builder.p26.execute(
            "SELECT updated_at_ms FROM p26_labels WHERE condition_id='label-row'"
        ).fetchone()[0]
        assert second.label_markets_scanned == 1
        assert second.labels_upserted == 0
        assert after == before
    finally:
        builder.close()


def test_canonical_rejects_snapshot_outside_lag_window(tmp_path):
    p25 = tmp_path / "p25.sqlite"
    conn = sqlite3.connect(p25)
    _p25_schema(conn)
    _insert_market_and_snapshot(
        conn,
        condition_id="late",
        horizon="5m",
        checkpoint=60,
        lag_ms=2501,
    )
    conn.close()
    builder = CanonicalDatasetBuilder(_settings(tmp_path, p25), code_commit="abc")
    try:
        result = builder.sync()
        assert result.rejected_lag == 1
        assert builder.canonical_rows() == []
    finally:
        builder.close()


def test_future_source_age_is_preserved_and_training_rejected(tmp_path):
    p25 = tmp_path / "p25.sqlite"
    conn = sqlite3.connect(p25)
    _p25_schema(conn)
    _insert_market_and_snapshot(
        conn,
        condition_id="future",
        horizon="5m",
        checkpoint=60,
        lag_ms=100,
        source_age_ms=-500.0,
    )
    conn.close()
    settings = _settings(tmp_path, p25)
    oracle = OracleTickStore(settings.p26_db_path)
    oracle.insert(
        OracleTick(
            asset="BTC",
            source="POLYMARKET_RTDS_CHAINLINK",
            value_text="100",
            value_real=100.0,
            source_ts_ms=1_799_999_939_000,
            recv_ts_ms=1_799_999_939_010,
            payload_sha256="tick2",
        )
    )
    oracle.close()
    builder = CanonicalDatasetBuilder(settings, code_commit="abc")
    try:
        result = builder.sync()
        assert result.inserted == 1
        row = builder.canonical_rows()[0]
        assert row["lineage_status"] == "FUTURE_DATA_REJECTED"
        assert row["training_eligible"] == 0
        lineage = json.loads(row["lineage_json"])
        assert lineage["binance_trade_ts_ms"] > lineage["decision_ts_ms"]
        assert lineage["no_future"] is False
    finally:
        builder.close()


def test_external_feature_whitelist_rejects_clob_terms():
    assert_external_feature_isolation(["ret_fast", "ptb_z"])
    try:
        assert_external_feature_isolation(["ret_fast", "clob_spread"])
    except ValueError as exc:
        assert "forbidden" in str(exc)
    else:
        raise AssertionError("CLOB feature was not rejected")
