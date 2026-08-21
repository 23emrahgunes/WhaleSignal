"""Isolated SQLite recorder for RESEARCH_PAPER_V2 decisions and settlements."""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict
from typing import Optional

from p26_delay_replay import DelayReplayResult
from p26_paper_v2 import PaperV2Decision
from p26_schema import connect_p26, ensure_p26_schema


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, declaration in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def ensure_paper_v2_schema(conn: sqlite3.Connection) -> None:
    ensure_p26_schema(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS p26_paper_trades (
            id                          INTEGER PRIMARY KEY AUTOINCREMENT,
            condition_id                TEXT NOT NULL,
            combo_key                   TEXT NOT NULL,
            horizon                     TEXT NOT NULL,
            strategy_version            TEXT NOT NULL,
            forecast_ts_ms              INTEGER NOT NULL,
            fill_ts_ms                  INTEGER NOT NULL,
            side                        TEXT NOT NULL CHECK(side IN ('UP','DOWN')),
            status                      TEXT NOT NULL CHECK(status IN ('OPEN','SKIPPED','SETTLED')),
            reason                      TEXT NOT NULL,
            selected_probability_lower  REAL,
            net_edge                    REAL,
            stake_usdc                  REAL,
            filled_stake_usdc           REAL,
            shares                      REAL,
            fill_price                  REAL,
            fee_usdc                    REAL,
            diagnostics_json            TEXT NOT NULL,
            official_result             TEXT,
            correct                     INTEGER,
            gross_payout                REAL,
            realized_pnl                REAL,
            roi                         REAL,
            settled_at_ms               INTEGER,
            created_at_ms               INTEGER NOT NULL,
            UNIQUE(condition_id,strategy_version)
        );
        CREATE INDEX IF NOT EXISTS idx_p26_paper_status
        ON p26_paper_trades(status,forecast_ts_ms);

        CREATE TABLE IF NOT EXISTS p26_alpha_replays (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            condition_id            TEXT NOT NULL,
            strategy_version        TEXT NOT NULL,
            combo_key               TEXT NOT NULL,
            horizon                 TEXT NOT NULL,
            side                    TEXT NOT NULL CHECK(side IN ('UP','DOWN')),
            forecast_ts_ms          INTEGER NOT NULL,
            history_max_ts_ms       INTEGER,
            observations_json       TEXT NOT NULL,
            missing_delays_json     TEXT NOT NULL,
            initial_edge            REAL,
            last_edge               REAL,
            edge_retention_ratio    REAL,
            half_life_ms            REAL,
            time_to_zero_edge_ms    REAL,
            observation_count       INTEGER NOT NULL,
            created_at_ms           INTEGER NOT NULL,
            UNIQUE(condition_id,strategy_version)
        );
        """
    )
    _ensure_columns(
        conn,
        "p26_paper_trades",
        {
            "token_id": "TEXT",
            "calibration_scope": "TEXT",
            "model_artifact_id": "TEXT",
            "alpha_artifact_id": "TEXT",
            "fee_source": "TEXT",
            "fee_formula_version": "TEXT",
            "selection_reason": "TEXT",
        },
    )
    conn.commit()


class PaperV2Recorder:
    def __init__(self, db_path: str) -> None:
        self.conn = connect_p26(db_path)
        ensure_paper_v2_schema(self.conn)

    def record(
        self,
        *,
        condition_id: str,
        combo_key: str,
        horizon: str,
        forecast_ts_ms: int,
        fill_ts_ms: int,
        decision: PaperV2Decision,
        stake_usdc: float,
        model_artifact_id: Optional[str] = None,
        selection_reason: Optional[str] = None,
    ) -> bool:
        fill = decision.fill
        status = "OPEN" if decision.eligible else "SKIPPED"
        before = self.conn.total_changes
        self.conn.execute(
            """
            INSERT OR IGNORE INTO p26_paper_trades(
                condition_id,combo_key,horizon,strategy_version,forecast_ts_ms,
                fill_ts_ms,side,status,reason,selected_probability_lower,net_edge,
                stake_usdc,filled_stake_usdc,shares,fill_price,fee_usdc,
                diagnostics_json,created_at_ms,token_id,calibration_scope,
                model_artifact_id,alpha_artifact_id,fee_source,
                fee_formula_version,selection_reason
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                condition_id, combo_key, horizon, decision.strategy_version,
                int(forecast_ts_ms), int(fill_ts_ms), decision.side, status,
                decision.reason, decision.selected_probability_lower,
                decision.net_edge, float(stake_usdc),
                (fill.filled_stake_usdc if fill else None),
                (fill.shares if fill else None),
                (fill.all_in_cost_per_share if fill else None),
                (fill.fee_usdc if fill else None),
                json.dumps(decision.to_dict(), sort_keys=True, separators=(",", ":")),
                int(time.time() * 1000), decision.token_id,
                decision.calibration_scope, model_artifact_id,
                (decision.alpha_ttl.artifact_id if decision.alpha_ttl else None),
                (fill.fee_source if fill else None),
                (fill.fee_formula_version if fill else None),
                selection_reason,
            ),
        )
        self.conn.commit()
        return self.conn.total_changes > before

    def attempt_exists(self, condition_id: str, strategy_version: str = "RESEARCH_PAPER_V2") -> bool:
        return self.conn.execute(
            "SELECT 1 FROM p26_paper_trades WHERE condition_id=? AND strategy_version=?",
            (condition_id, strategy_version),
        ).fetchone() is not None

    def settle(self, condition_id: str, official_result: str, *, settled_at_ms: Optional[int] = None) -> int:
        result = official_result.strip().upper()
        if result not in {"UP", "DOWN"}:
            raise ValueError("official result must be UP or DOWN")
        rows = self.conn.execute(
            """
            SELECT id,side,shares,stake_usdc,fee_usdc
            FROM p26_paper_trades
            WHERE condition_id=? AND status='OPEN'
            """,
            (condition_id,),
        ).fetchall()
        now = int(time.time() * 1000) if settled_at_ms is None else int(settled_at_ms)
        for row in rows:
            correct = str(row["side"]) == result
            shares = float(row["shares"] or 0.0)
            stake = float(row["stake_usdc"] or 0.0)
            fee = float(row["fee_usdc"] or 0.0)
            payout = shares if correct else 0.0
            pnl = payout - stake - fee
            roi = pnl / stake if stake > 0 else None
            self.conn.execute(
                """
                UPDATE p26_paper_trades SET status='SETTLED',official_result=?,correct=?,
                    gross_payout=?,realized_pnl=?,roi=?,settled_at_ms=? WHERE id=?
                """,
                (result, int(correct), payout, pnl, roi, now, int(row["id"])),
            )
        self.conn.commit()
        return len(rows)

    def settle_available_labels(self) -> int:
        rows = self.conn.execute(
            """
            SELECT DISTINCT t.condition_id,l.official_label,l.official_resolved_at_ms
            FROM p26_paper_trades t
            JOIN p26_labels l ON l.condition_id=t.condition_id
            WHERE t.status='OPEN' AND l.official_label IS NOT NULL
            """
        ).fetchall()
        total = 0
        for row in rows:
            total += self.settle(
                str(row["condition_id"]),
                "UP" if int(row["official_label"]) == 1 else "DOWN",
                settled_at_ms=(
                    int(row["official_resolved_at_ms"])
                    if row["official_resolved_at_ms"] is not None else None
                ),
            )
        return total

    def record_ex_post_alpha(
        self,
        *,
        condition_id: str,
        combo_key: str,
        horizon: str,
        side: str,
        forecast_ts_ms: int,
        replay: DelayReplayResult,
        strategy_version: str = "RESEARCH_PAPER_V2",
    ) -> bool:
        decay = replay.decay
        history_max = (
            int(forecast_ts_ms) + max((item.delay_ms for item in replay.observations), default=0)
            if replay.observations else None
        )
        before = self.conn.total_changes
        self.conn.execute(
            """
            INSERT OR IGNORE INTO p26_alpha_replays(
                condition_id,strategy_version,combo_key,horizon,side,forecast_ts_ms,
                history_max_ts_ms,observations_json,missing_delays_json,
                initial_edge,last_edge,edge_retention_ratio,half_life_ms,
                time_to_zero_edge_ms,observation_count,created_at_ms
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                condition_id, strategy_version, combo_key, horizon, side,
                int(forecast_ts_ms), history_max,
                json.dumps([asdict(item) for item in replay.observations], separators=(",", ":")),
                json.dumps(list(replay.missing_delays_ms), separators=(",", ":")),
                (decay.initial_edge if decay else None),
                (decay.last_edge if decay else None),
                (decay.edge_retention_ratio if decay else None),
                (decay.half_life_ms if decay else None),
                (decay.time_to_zero_edge_ms if decay else None),
                len(replay.observations), int(time.time() * 1000),
            ),
        )
        self.conn.commit()
        return self.conn.total_changes > before

    def close(self) -> None:
        self.conn.close()
