"""P2.5 forecast recorder layered on top of the proven P1 recorder."""
from __future__ import annotations

import json
import math
import sqlite3
import time

from models import Decision, FeatureSnapshot, LabelStatus, MarketRef
from recorder import Recorder


def _brier(p, outcome_up):
    if p is None:
        return None
    return (float(p) - (1.0 if outcome_up else 0.0)) ** 2


def _log_loss(p, outcome_up):
    if p is None:
        return None
    value = max(1e-6, min(1.0 - 1e-6, float(p)))
    return -math.log(value if outcome_up else 1.0 - value)


class P25Recorder(Recorder):
    """Adds immutable checkpoint forecasts and resolved shadow analytics."""

    def __init__(self, db_path: str) -> None:
        super().__init__(db_path)
        self._ensure_forecast_schema()

    def _ensure_forecast_schema(self) -> None:
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS forecasts (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                condition_id          TEXT NOT NULL,
                combo_key             TEXT NOT NULL,
                checkpoint_sec        INTEGER NOT NULL,
                ts                    REAL NOT NULL,
                tte_sec               REAL,
                phase                 TEXT NOT NULL,
                model_version         TEXT NOT NULL,
                model_source          TEXT,
                feature_ready         INTEGER NOT NULL DEFAULT 0,
                feature_coverage      REAL,
                predictability        REAL,
                conflict_score        REAL,
                directional_consensus REAL,
                regime                TEXT,
                p_up_raw              REAL,
                p_up_calibrated       REAL,
                p_up_ptb              REAL,
                p_up_ptb_heuristic    REAL,
                p_up_external         REAL,
                p_up_market           REAL,
                confidence            REAL,
                decision              TEXT,
                abstain_reason        TEXT,
                threshold             REAL,
                threshold_source      TEXT,
                calibration_source    TEXT,
                price_edge            REAL,
                extra_json            TEXT,
                official_result       TEXT,
                resolved_at           REAL,
                correct               INTEGER,
                brier                 REAL,
                log_loss              REAL,
                ptb_brier             REAL,
                ptb_heuristic_brier   REAL,
                external_brier        REAL,
                market_brier          REAL,
                naive_brier           REAL,
                UNIQUE(condition_id, checkpoint_sec, model_version)
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_forecast_cond "
            "ON forecasts(condition_id)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_forecast_combo "
            "ON forecasts(combo_key)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_forecast_resolved "
            "ON forecasts(official_result, combo_key)"
        )
        self.conn.commit()

    def record_forecast(
        self,
        ref: MarketRef,
        snap: FeatureSnapshot,
        checkpoint: int,
        trace: dict,
    ) -> bool:
        if not ref.condition_id:
            return False
        model_version = str(trace.get("model_version") or "NO_MODEL")
        p_cal = trace.get("p_up_calibrated")
        p_market = trace.get("p_up_market")
        price_edge = (
            float(p_cal) - float(p_market)
            if p_cal is not None and p_market is not None
            else None
        )
        before = self.conn.total_changes
        self.conn.execute(
            """
            INSERT OR IGNORE INTO forecasts (
                condition_id, combo_key, checkpoint_sec, ts, tte_sec, phase,
                model_version, model_source, feature_ready, feature_coverage,
                predictability, conflict_score, directional_consensus, regime,
                p_up_raw, p_up_calibrated, p_up_ptb, p_up_ptb_heuristic,
                p_up_external, p_up_market, confidence, decision, abstain_reason,
                threshold, threshold_source, calibration_source, price_edge,
                extra_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ref.condition_id,
                ref.combo.key,
                checkpoint,
                snap.ts,
                snap.tte_sec,
                str(trace.get("phase") or ""),
                model_version,
                trace.get("model_source"),
                1 if trace.get("feature_ready") else 0,
                trace.get("feature_coverage"),
                trace.get("predictability"),
                trace.get("conflict_score"),
                trace.get("directional_consensus"),
                trace.get("regime"),
                trace.get("p_up_raw"),
                p_cal,
                trace.get("p_up_ptb"),
                trace.get("p_up_ptb_heuristic"),
                trace.get("p_up_external"),
                p_market,
                trace.get("confidence"),
                trace.get("decision"),
                trace.get("abstain_reason"),
                trace.get("threshold"),
                trace.get("threshold_source"),
                trace.get("calibration_source"),
                price_edge,
                json.dumps(trace, separators=(",", ":")),
            ),
        )
        self.conn.commit()
        return self.conn.total_changes > before

    def settle(self, ref: MarketRef) -> None:
        official = ref.official_result or ref.resolved_outcome
        if not ref.condition_id or not ref.resolved or official is None:
            return

        computed = ref.computed_result
        if computed is None:
            ref.label_status = LabelStatus.UNKNOWN
        elif computed == official:
            ref.label_status = LabelStatus.MATCH
        else:
            ref.label_status = LabelStatus.MISMATCH

        # Proven P1 path updates market/snapshot labels. MATCH remains the only
        # training label; forecasts are evaluated from explicit official outcome.
        super().settle(ref)

        outcome_up = official == Decision.UP
        rows = self.conn.execute(
            """
            SELECT id, decision, p_up_calibrated, p_up_raw, p_up_ptb,
                   p_up_ptb_heuristic, p_up_external, p_up_market
            FROM forecasts WHERE condition_id=?
            """,
            (ref.condition_id,),
        ).fetchall()
        resolved_at = ref.official_resolved_at or time.time()
        for row in rows:
            primary = (
                row["p_up_calibrated"]
                if row["p_up_calibrated"] is not None
                else row["p_up_raw"]
            )
            decision = row["decision"]
            correct = None
            if decision in ("UP", "DOWN"):
                correct = int((decision == "UP") == outcome_up)
            self.conn.execute(
                """
                UPDATE forecasts SET
                    official_result=?, resolved_at=?, correct=?,
                    brier=?, log_loss=?, ptb_brier=?,
                    ptb_heuristic_brier=?, external_brier=?,
                    market_brier=?, naive_brier=0.25
                WHERE id=?
                """,
                (
                    official.value,
                    resolved_at,
                    correct,
                    _brier(primary, outcome_up),
                    _log_loss(primary, outcome_up),
                    _brier(row["p_up_ptb"], outcome_up),
                    _brier(row["p_up_ptb_heuristic"], outcome_up),
                    _brier(row["p_up_external"], outcome_up),
                    _brier(row["p_up_market"], outcome_up),
                    row["id"],
                ),
            )
        self.conn.commit()

    @staticmethod
    def _analytics_rows(rows: list[sqlite3.Row], min_n: int) -> dict:
        total = len(rows)
        decided = [row for row in rows if row["decision"] in ("UP", "DOWN")]
        result = {
            "n": total,
            "n_decided": len(decided),
            "coverage": round(len(decided) / total, 4) if total else 0.0,
            "min_n": min_n,
            "insufficient": total < min_n,
        }
        if decided:
            wins = sum(int(row["correct"] or 0) for row in decided)
            result["accuracy"] = round(wins / len(decided), 4)
        else:
            result["accuracy"] = None
        for column, label in (
            ("brier", "brier_b2"),
            ("external_brier", "brier_b1"),
            ("ptb_brier", "brier_ptb_trained"),
            ("ptb_heuristic_brier", "brier_ptb_heuristic"),
            ("market_brier", "brier_market"),
            ("naive_brier", "brier_naive_50"),
        ):
            values = [
                float(row[column]) for row in rows if row[column] is not None
            ]
            result[label] = (
                round(sum(values) / len(values), 6) if values else None
            )
        losses = [
            float(row["log_loss"])
            for row in rows
            if row["log_loss"] is not None
        ]
        result["log_loss_b2"] = (
            round(sum(losses) / len(losses), 6) if losses else None
        )
        return result

    def forecast_analytics(self, min_n: int = 30) -> dict:
        rows = self.conn.execute(
            """
            SELECT combo_key, decision, correct, brier, log_loss, ptb_brier,
                   ptb_heuristic_brier, external_brier, market_brier, naive_brier
            FROM forecasts WHERE official_result IS NOT NULL
            """
        ).fetchall()
        per_combo: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            per_combo.setdefault(row["combo_key"], []).append(row)
        return {
            "overall": self._analytics_rows(rows, min_n),
            "per_combo": {
                key: self._analytics_rows(combo_rows, min_n)
                for key, combo_rows in sorted(per_combo.items())
            },
        }

    def stats(self) -> dict:
        stats = super().stats()
        stats.update(
            {
                "forecasts": self.conn.execute(
                    "SELECT COUNT(*) FROM forecasts"
                ).fetchone()[0],
                "labeled_forecasts": self.conn.execute(
                    "SELECT COUNT(*) FROM forecasts "
                    "WHERE official_result IS NOT NULL"
                ).fetchone()[0],
                "decided_forecasts": self.conn.execute(
                    "SELECT COUNT(*) FROM forecasts "
                    "WHERE decision IN ('UP','DOWN')"
                ).fetchone()[0],
            }
        )
        return stats
