from __future__ import annotations

import sqlite3

from p26_book_store import ensure_book_schema
from p26_retention_v2 import RETENTION_VERSION, prune_p26_v2
from p26_schema import connect_p26, ensure_p26_schema


def test_retention_v2_prunes_old_history_but_keeps_latest_token_row(tmp_path):
    db = str(tmp_path / "p26.sqlite")
    now = 2_000_000_000_000
    c = connect_p26(db)
    try:
        ensure_p26_schema(c)
        ensure_book_schema(c)
        rows = [
            (1, now - 20 * 60_000),
            (2, now - 10 * 60_000),
            (3, now - 1 * 60_000),
        ]
        for seq, recv in rows:
            c.execute(
                """
                INSERT INTO p26_clob_books(
                    condition_id,combo_key,side,token_id,recv_ts_ms,source_ts_ms,
                    sequence,bids_json,asks_json,payload_sha256,schema_version,inserted_at_ms
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "cond", "BTC:5m", "UP", "token", recv, recv - 60_000, seq,
                    "[]", "[[0.1,10.0]]", f"sha-{seq}", "TEST", recv,
                ),
            )
        # The only row for another token is old; it is still the latest truth and stays.
        recv = now - 40 * 60_000
        c.execute(
            """
            INSERT INTO p26_clob_books(
                condition_id,combo_key,side,token_id,recv_ts_ms,source_ts_ms,
                sequence,bids_json,asks_json,payload_sha256,schema_version,inserted_at_ms
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            ("rest", "ETH:5m", "UP", "rest-token", recv, recv - 60_000, 1,
             "[]", "[[0.2,10.0]]", "rest-sha", "TEST", recv),
        )
        c.commit()
    finally:
        c.close()

    result = prune_p26_v2(db, now_ms=now, book_hours=0.25, batch_size=100)
    assert result["status"] == "OK"
    assert result["version"] == RETENTION_VERSION
    assert result["books_deleted"] == 1

    c = sqlite3.connect(db)
    try:
        assert c.execute(
            "SELECT COUNT(*) FROM p26_clob_books WHERE token_id='token'"
        ).fetchone()[0] == 2
        assert c.execute(
            "SELECT COUNT(*) FROM p26_clob_books WHERE token_id='rest-token'"
        ).fetchone()[0] == 1
    finally:
        c.close()


def test_retention_v2_missing_db_is_safe(tmp_path):
    assert prune_p26_v2(str(tmp_path / "missing.sqlite")) == {
        "status": "DB_MISSING",
        "version": RETENTION_VERSION,
    }
