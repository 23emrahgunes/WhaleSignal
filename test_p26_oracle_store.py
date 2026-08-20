from __future__ import annotations

import json
import time

from p26_oracle_store import OracleTick, OracleTickStore, iter_rtds_ticks
from p26_schema import connect_p26, ensure_p26_schema


def _tick(asset: str, source_ts_ms: int, value: float = 100.0) -> OracleTick:
    return OracleTick(
        asset=asset,
        source="POLYMARKET_RTDS_CHAINLINK",
        value_text=str(value),
        value_real=value,
        source_ts_ms=source_ts_ms,
        recv_ts_ms=source_ts_ms + 10,
        payload_sha256=f"{asset}-{source_ts_ms}-{value}",
    )


def test_parse_rtds_payload_and_fixed_point_normalization():
    recv_ms = 1_800_000_000_100
    obj = {
        "topic": "crypto_prices_chainlink",
        "payload": {
            "symbol": "btc/usd",
            "data": [
                {
                    "timestamp": 1_800_000_000_000,
                    "value": "64123.25",
                    "full_accuracy_value": "64123250000000000000000",
                }
            ],
        },
    }
    ticks = list(iter_rtds_ticks(obj, recv_ms))
    assert len(ticks) == 1
    tick = ticks[0]
    assert tick.asset == "BTC"
    assert tick.value_real == 64123.25
    assert tick.source_ts_ms == 1_800_000_000_000
    assert tick.recv_ts_ms == recv_ms
    assert len(tick.payload_sha256) == 64


def test_oracle_store_dedup_rehydrate_and_lookup(tmp_path):
    store = OracleTickStore(str(tmp_path / "p26.sqlite"))
    try:
        ticks = [_tick("BTC", 1000), _tick("BTC", 2000), _tick("ETH", 1500, 200.0)]
        assert store.insert_many(ticks) == 3
        assert store.insert_many(ticks) == 0
        assert store.latest("BTC").source_ts_ms == 2000
        assert store.at_or_before("BTC", 1800).source_ts_ms == 1000
        assert store.at_or_before("BTC", 1800, max_age_ms=500) is None
        hydrated = store.rehydrate(since_ts_ms=1200)
        assert [tick.source_ts_ms for tick in hydrated["BTC"]] == [2000]
        assert [tick.source_ts_ms for tick in hydrated["ETH"]] == [1500]
        assert store.stats()["ticks"] == 3
    finally:
        store.close()


def test_prune_preserves_tick_referenced_by_canonical_row(tmp_path):
    path = str(tmp_path / "p26.sqlite")
    store = OracleTickStore(path)
    old = _tick("BTC", 1000)
    newer = _tick("BTC", 2000)
    store.insert_many([old, newer])
    old_id = store.at_or_before("BTC", 1000).id
    conn = store.conn
    conn.execute(
        """
        INSERT INTO p26_canonical_rows(
            condition_id,combo_key,asset,horizon,market_start_ts_ms,market_end_ts_ms,
            checkpoint_sec,nominal_target_ts_ms,decision_ts_ms,capture_lag_ms,
            source_snapshot_id,feature_vector_json,feature_names_json,
            feature_vector_sha256,feature_schema_version,feature_schema_hash,
            extraction_policy_version,chainlink_tick_id,quality_status,lineage_status,
            training_eligible,lineage_json,code_commit,created_at_ms
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "c1","BTC:5m","BTC","5m",0,300000,60,240000,240100,100,1,
            "{}","[]","v","v1","h","p",old_id,"OK","COMPLETE",1,"{}","abc",300000,
        ),
    )
    conn.commit()
    assert store.prune(before_ts_ms=3000, batch_size=100) == 1
    remaining = conn.execute("SELECT source_ts_ms FROM p26_oracle_ticks ORDER BY source_ts_ms").fetchall()
    assert [row[0] for row in remaining] == [1000]
    store.close()


def test_schema_is_wal_and_integrity_ok(tmp_path):
    conn = connect_p26(str(tmp_path / "schema.sqlite"))
    try:
        ensure_p26_schema(conn)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
