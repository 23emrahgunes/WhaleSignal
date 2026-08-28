from __future__ import annotations

import sqlite3

from p26_book_store import ensure_book_schema
from p26_fee import ensure_fee_schema
from p26_retention import prune_p26
from p26_schema import connect_p26, ensure_p26_schema


def _seed(db: str, now_ms: int) -> None:
    c = connect_p26(db)
    try:
        ensure_p26_schema(c)
        ensure_book_schema(c)
        ensure_fee_schema(c)
        old_book = now_ms - 30 * 3_600_000
        fresh_book = now_ms - 1 * 3_600_000
        for idx, ts in enumerate((old_book, fresh_book), start=1):
            c.execute(
                """
                INSERT INTO p26_clob_books(
                    condition_id,combo_key,side,token_id,recv_ts_ms,source_ts_ms,
                    sequence,bids_json,asks_json,payload_sha256,schema_version,inserted_at_ms
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (f"c{idx}", "BTC:5m", "UP", f"t{idx}", ts, ts, idx,
                 "[]", "[[0.1,10.0]]", f"sha{idx}", "TEST", ts),
            )
        old_oracle = now_ms - 80 * 3_600_000
        fresh_oracle = now_ms - 1 * 3_600_000
        for idx, ts in enumerate((old_oracle, fresh_oracle), start=1):
            c.execute(
                """
                INSERT INTO p26_oracle_ticks(
                    asset,source,value_text,value_real,source_ts_ms,recv_ts_ms,
                    payload_sha256,schema_version,inserted_at_ms
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                ("BTC", "TEST", str(100+idx), 100+idx, ts, ts,
                 f"oracle{idx}", "TEST", ts),
            )
        old_health = now_ms - 60 * 3_600_000
        c.execute(
            "INSERT INTO p26_health_events(component,event_type,severity,message,details_json,ts_ms) VALUES(?,?,?,?,?,?)",
            ("test", "old", "INFO", "old", None, old_health),
        )
        c.commit()
    finally:
        c.close()


def test_retention_prunes_old_book_oracle_and_health(tmp_path):
    db = str(tmp_path / "p26.sqlite")
    now_ms = 2_000_000_000_000
    _seed(db, now_ms)
    result = prune_p26(db, now_ms=now_ms, batch_size=100, max_batches=10)
    assert result["status"] == "OK"
    c = sqlite3.connect(db)
    try:
        assert c.execute("SELECT COUNT(*) FROM p26_clob_books").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM p26_oracle_ticks").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM p26_health_events").fetchone()[0] == 0
    finally:
        c.close()


def test_missing_db_is_safe(tmp_path):
    result = prune_p26(str(tmp_path / "missing.sqlite"))
    assert result == {"status": "DB_MISSING"}
