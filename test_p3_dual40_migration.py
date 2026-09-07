from __future__ import annotations

import sqlite3

from p3_dual40_store import DUAL40_ASSETS, connect_dual40, ladder_state


def _legacy_db(path: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE p3_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at_ms INTEGER NOT NULL
            );
            CREATE TABLE p3_dual40_state (
                scope TEXT PRIMARY KEY CHECK(scope IN ('PAPER','LIVE')),
                level_index INTEGER NOT NULL DEFAULT 0,
                loss_pool_usdc REAL NOT NULL DEFAULT 0,
                hard_stopped INTEGER NOT NULL DEFAULT 0,
                hard_stop_reason TEXT,
                updated_at_ms INTEGER NOT NULL
            );
            CREATE TABLE p3_dual40_cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL CHECK(scope IN ('PAPER','LIVE')),
                session_id TEXT,
                condition_id TEXT NOT NULL,
                combo_key TEXT NOT NULL,
                market_end_ts_ms INTEGER NOT NULL,
                level_index INTEGER NOT NULL,
                target_shares REAL NOT NULL CHECK(target_shares > 0),
                maker_price REAL NOT NULL CHECK(maker_price > 0 AND maker_price < 1),
                status TEXT NOT NULL,
                gate_json TEXT NOT NULL DEFAULT '{}',
                up_token_id TEXT NOT NULL,
                down_token_id TEXT NOT NULL,
                up_order_id TEXT,
                down_order_id TEXT,
                before_up_shares REAL NOT NULL DEFAULT 0,
                before_down_shares REAL NOT NULL DEFAULT 0,
                up_filled_shares REAL NOT NULL DEFAULT 0,
                down_filled_shares REAL NOT NULL DEFAULT 0,
                matched_shares REAL NOT NULL DEFAULT 0,
                residual_side TEXT,
                residual_shares REAL NOT NULL DEFAULT 0,
                official_result TEXT,
                realized_pnl_usdc REAL,
                loss_pool_before_usdc REAL NOT NULL DEFAULT 0,
                loss_pool_after_usdc REAL,
                merge_tx_hash TEXT,
                heartbeat_id TEXT,
                last_heartbeat_ms INTEGER,
                near_touch_up_41 INTEGER NOT NULL DEFAULT 0,
                near_touch_down_41 INTEGER NOT NULL DEFAULT 0,
                error_code TEXT,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at_ms INTEGER NOT NULL,
                orders_posted_at_ms INTEGER,
                orders_cancelled_at_ms INTEGER,
                resolved_at_ms INTEGER,
                updated_at_ms INTEGER NOT NULL,
                UNIQUE(scope,condition_id)
            );
            INSERT INTO p3_dual40_state VALUES
                ('PAPER',1,2.0,0,NULL,1000),
                ('LIVE',0,0.0,0,NULL,1000);
            INSERT INTO p3_dual40_cycles(
                scope,session_id,condition_id,combo_key,market_end_ts_ms,
                level_index,target_shares,maker_price,status,gate_json,
                up_token_id,down_token_id,loss_pool_before_usdc,details_json,
                created_at_ms,updated_at_ms
            ) VALUES(
                'PAPER',NULL,'cond-btc','BTC:5m',2000,0,5,0.40,
                'NO_FILL','{}','up','down',0,'{}',1000,1000
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_legacy_global_schema_migration_is_idempotent(tmp_path):
    path = str(tmp_path / "legacy.sqlite")
    _legacy_db(path)

    conn = connect_dual40(path)
    conn.close()
    reopened = connect_dual40(path)
    try:
        columns = {
            row["name"]
            for row in reopened.execute("PRAGMA table_info(p3_dual40_cycles)")
        }
        assert "asset" in columns
        assert reopened.execute(
            "SELECT asset FROM p3_dual40_cycles WHERE condition_id='cond-btc'"
        ).fetchone()["asset"] == "BTC"
        for scope in ("PAPER", "LIVE"):
            for asset in DUAL40_ASSETS:
                assert ladder_state(reopened, scope, asset)["asset"] == asset
    finally:
        reopened.close()


def test_legacy_mixed_loss_pool_is_not_silently_assigned(tmp_path):
    path = str(tmp_path / "legacy.sqlite")
    _legacy_db(path)
    conn = connect_dual40(path)
    try:
        for asset in DUAL40_ASSETS:
            state = ladder_state(conn, "PAPER", asset)
            assert state["level_index"] == 0
            assert state["loss_pool_usdc"] == 0.0
        meta = conn.execute(
            "SELECT value FROM p3_meta WHERE key='dual40_legacy_global_state_review_required'"
        ).fetchone()
        assert meta is not None
        assert "LEGACY_GLOBAL_STATE_REVIEW_REQUIRED" in meta["value"]
    finally:
        conn.close()
