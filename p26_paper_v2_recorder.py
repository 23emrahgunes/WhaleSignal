"""Isolated SQLite recorder for RESEARCH_PAPER_V2 decisions and settlements."""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Optional

from p26_paper_v2 import PaperV2Decision
from p26_schema import connect_p26, ensure_p26_schema


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
        """
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
                diagnostics_json,created_at_ms
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                condition_id,combo_key,horizon,decision.strategy_version,
                int(forecast_ts_ms),int(fill_ts_ms),decision.side,status,decision.reason,
                decision.selected_probability_lower,decision.net_edge,float(stake_usdc),
                (fill.filled_stake_usdc if fill else None),
                (fill.shares if fill else None),
                (fill.all_in_cost_per_share if fill else None),
                (fill.fee_usdc if fill else None),
                json.dumps(decision.to_dict(),sort_keys=True,separators=(",",":")),
                int(time.time()*1000),
            ),
        )
        self.conn.commit()
        return self.conn.total_changes > before

    def settle(self, condition_id: str, official_result: str, *, settled_at_ms: Optional[int] = None) -> int:
        result = official_result.strip().upper()
        if result not in {"UP","DOWN"}:
            raise ValueError("official result must be UP or DOWN")
        rows = self.conn.execute(
            "SELECT id,side,shares,stake_usdc,fee_usdc FROM p26_paper_trades WHERE condition_id=? AND status='OPEN'",
            (condition_id,),
        ).fetchall()
        now = int(time.time()*1000) if settled_at_ms is None else int(settled_at_ms)
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
                (result,int(correct),payout,pnl,roi,now,int(row["id"])),
            )
        self.conn.commit()
        return len(rows)

    def close(self) -> None:
        self.conn.close()
