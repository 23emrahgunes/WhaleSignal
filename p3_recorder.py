"""Persistence helpers for the isolated P3 arbitrage lab."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from typing import Iterable, Optional

from p3_models import StructuralOpportunity
from p3_schema import connect_p3, ensure_p3_schema


def opportunity_key(opp: StructuralOpportunity) -> str:
    raw = (
        f"{opp.strategy}|{opp.condition_id}|{opp.up_book_id}|{opp.down_book_id}|"
        f"{opp.quantity_shares:.12f}|{opp.net_profit_usdc:.12f}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class P3Recorder:
    def __init__(self, db_path: str) -> None:
        self.conn = connect_p3(db_path)
        ensure_p3_schema(self.conn)

    def record_opportunity(self, opp: StructuralOpportunity) -> tuple[int, bool]:
        key = opportunity_key(opp)
        now_ms = int(time.time() * 1000)
        before = self.conn.total_changes
        self.conn.execute(
            """
            INSERT OR IGNORE INTO p3_opportunities(
                opportunity_key,strategy,condition_id,combo_key,detected_ts_ms,
                up_book_id,down_book_id,up_book_ts_ms,down_book_ts_ms,
                source_skew_ms,max_book_age_ms,quantity_shares,up_vwap,down_vwap,
                up_fee_usdc,down_fee_usdc,gross_edge_per_share,gross_profit_usdc,
                execution_buffer_usdc,net_profit_usdc,capital_usdc,net_roi,
                up_limit_price,down_limit_price,fee_lineage_ok,quality_status,
                payload_json,created_at_ms
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                key, opp.strategy, opp.condition_id, opp.combo_key,
                int(opp.detected_ts_ms), int(opp.up_book_id), int(opp.down_book_id),
                int(opp.up_book_ts_ms), int(opp.down_book_ts_ms), int(opp.source_skew_ms),
                int(opp.max_book_age_ms), float(opp.quantity_shares), float(opp.up_vwap),
                float(opp.down_vwap), float(opp.up_fee_usdc), float(opp.down_fee_usdc),
                float(opp.gross_edge_per_share), float(opp.gross_profit_usdc),
                float(opp.execution_buffer_usdc), float(opp.net_profit_usdc),
                float(opp.capital_usdc), float(opp.net_roi), float(opp.up_limit_price),
                float(opp.down_limit_price), int(bool(opp.fee_lineage_ok)),
                str(opp.quality_status), json.dumps(opp.to_dict(), sort_keys=True), now_ms,
            ),
        )
        self.conn.commit()
        created = self.conn.total_changes > before
        row = self.conn.execute(
            "SELECT id FROM p3_opportunities WHERE opportunity_key=?", (key,)
        ).fetchone()
        if row is None:
            raise RuntimeError("opportunity insert lookup failed")
        return int(row["id"]), created

    def touch_window(self, opportunity_id: int, opp: StructuralOpportunity) -> int:
        row = self.conn.execute(
            """
            SELECT * FROM p3_windows
            WHERE strategy=? AND condition_id=? AND status='OPEN'
            ORDER BY id DESC LIMIT 1
            """,
            (opp.strategy, opp.condition_id),
        ).fetchone()
        if row is None:
            cur = self.conn.execute(
                """
                INSERT INTO p3_windows(
                    strategy,condition_id,combo_key,opened_ts_ms,last_seen_ts_ms,
                    observations,peak_net_profit_usdc,peak_net_roi,peak_quantity_shares,
                    peak_opportunity_id,status
                ) VALUES (?,?,?,?,?,1,?,?,?,?, 'OPEN')
                """,
                (
                    opp.strategy, opp.condition_id, opp.combo_key, int(opp.detected_ts_ms),
                    int(opp.detected_ts_ms), float(opp.net_profit_usdc), float(opp.net_roi),
                    float(opp.quantity_shares), int(opportunity_id),
                ),
            )
            self.conn.commit()
            return int(cur.lastrowid)

        peak_profit = float(row["peak_net_profit_usdc"])
        is_peak = opp.net_profit_usdc > peak_profit
        self.conn.execute(
            """
            UPDATE p3_windows SET
                last_seen_ts_ms=?, observations=observations+1,
                peak_net_profit_usdc=CASE WHEN ? THEN ? ELSE peak_net_profit_usdc END,
                peak_net_roi=CASE WHEN ? THEN ? ELSE peak_net_roi END,
                peak_quantity_shares=CASE WHEN ? THEN ? ELSE peak_quantity_shares END,
                peak_opportunity_id=CASE WHEN ? THEN ? ELSE peak_opportunity_id END
            WHERE id=?
            """,
            (
                int(opp.detected_ts_ms), int(is_peak), float(opp.net_profit_usdc),
                int(is_peak), float(opp.net_roi), int(is_peak), float(opp.quantity_shares),
                int(is_peak), int(opportunity_id), int(row["id"]),
            ),
        )
        self.conn.commit()
        return int(row["id"])

    def close_stale_windows(
        self,
        active_keys: set[tuple[str, str]],
        *,
        now_ms: int,
        grace_ms: int,
        reason: str = "OPPORTUNITY_GONE",
    ) -> int:
        rows = self.conn.execute(
            "SELECT id,strategy,condition_id,last_seen_ts_ms FROM p3_windows WHERE status='OPEN'"
        ).fetchall()
        closed = 0
        for row in rows:
            key = (str(row["strategy"]), str(row["condition_id"]))
            if key in active_keys:
                continue
            if int(now_ms) - int(row["last_seen_ts_ms"]) < int(grace_ms):
                continue
            self.conn.execute(
                """
                UPDATE p3_windows SET status='CLOSED',closed_ts_ms=?,close_reason=?
                WHERE id=? AND status='OPEN'
                """,
                (int(now_ms), reason, int(row["id"])),
            )
            closed += 1
        self.conn.commit()
        return closed

    def record_health(
        self,
        component: str,
        severity: str,
        event_type: str,
        message: str,
        details: Optional[dict] = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO p3_health_events(component,severity,event_type,message,details_json,ts_ms)
            VALUES(?,?,?,?,?,?)
            """,
            (
                component, severity, event_type, message,
                json.dumps(details or {}, sort_keys=True), int(time.time() * 1000),
            ),
        )
        self.conn.commit()

    def recent_opportunities(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM p3_opportunities ORDER BY detected_ts_ms DESC,id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        self.conn.close()
