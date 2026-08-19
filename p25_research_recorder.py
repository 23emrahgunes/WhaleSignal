"""Persistence and evaluation for the always-on research forecast layer.

The existing P2.5 recorder continues to store and score the validated signal.  This
subclass adds separate columns for the provisional/validated research forecast so
its accuracy and Brier score can be measured without pretending it was an actionable
signal.  Existing SQLite databases are migrated in place; no dataset is deleted.
"""
from __future__ import annotations

import math
import sqlite3
from typing import Optional

from models import Decision, FeatureSnapshot, MarketRef
from p25_recorder import P25Recorder


class P25ResearchRecorder(P25Recorder):
    def __init__(self, db_path: str) -> None:
        super().__init__(db_path)
        self._ensure_research_forecast_schema()

    def _ensure_research_forecast_schema(self) -> None:
        wanted = {
            "forecast_direction": "TEXT",
            "forecast_p_up": "REAL",
            "forecast_confidence": "REAL",
            "forecast_grade": "TEXT",
            "forecast_status": "TEXT",
            "forecast_source": "TEXT",
            "forecast_agreement": "REAL",
            "forecast_model_maturity": "REAL",
            "forecast_correct": "INTEGER",
            "forecast_brier": "REAL",
        }
        existing = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(forecasts)").fetchall()
        }
        for name, declaration in wanted.items():
            if name not in existing:
                self.conn.execute(
                    f"ALTER TABLE forecasts ADD COLUMN {name} {declaration}"
                )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_forecast_research_direction "
            "ON forecasts(forecast_direction, official_result)"
        )
        self.conn.commit()

    def record_forecast(
        self,
        ref: MarketRef,
        snap: FeatureSnapshot,
        checkpoint: int,
        trace: dict,
    ) -> bool:
        inserted = super().record_forecast(ref, snap, checkpoint, trace)
        if not inserted:
            return False
        self.conn.execute(
            """
            UPDATE forecasts SET
                forecast_direction=?,
                forecast_p_up=?,
                forecast_confidence=?,
                forecast_grade=?,
                forecast_status=?,
                forecast_source=?,
                forecast_agreement=?,
                forecast_model_maturity=?
            WHERE condition_id=? AND checkpoint_sec=? AND model_version=?
            """,
            (
                trace.get("forecast_direction"),
                trace.get("forecast_p_up"),
                trace.get("forecast_confidence"),
                trace.get("forecast_grade"),
                trace.get("forecast_status"),
                trace.get("forecast_source"),
                trace.get("forecast_agreement"),
                trace.get("forecast_model_maturity"),
                ref.condition_id,
                checkpoint,
                str(trace.get("model_version") or "NO_MODEL"),
            ),
        )
        self.conn.commit()
        return True

    def settle(self, ref: MarketRef) -> None:
        official = ref.official_result or ref.resolved_outcome
        super().settle(ref)
        if not ref.condition_id or official is None:
            return

        outcome_up = official == Decision.UP
        rows = self.conn.execute(
            """
            SELECT id, forecast_direction, forecast_p_up
            FROM forecasts
            WHERE condition_id=?
            """,
            (ref.condition_id,),
        ).fetchall()
        for row in rows:
            direction = row["forecast_direction"]
            p_up = row["forecast_p_up"]
            correct: Optional[int] = None
            if direction in ("UP", "DOWN"):
                correct = int((direction == "UP") == outcome_up)
            brier = None
            if p_up is not None:
                brier = (float(p_up) - (1.0 if outcome_up else 0.0)) ** 2
            self.conn.execute(
                """
                UPDATE forecasts
                SET forecast_correct=?, forecast_brier=?
                WHERE id=?
                """,
                (correct, brier, row["id"]),
            )
        self.conn.commit()

    @staticmethod
    def _research_metrics(rows: list[sqlite3.Row], min_n: int) -> dict:
        probability_rows = [row for row in rows if row["forecast_p_up"] is not None]
        directional = [
            row
            for row in probability_rows
            if row["forecast_direction"] in ("UP", "DOWN")
        ]
        wins = sum(int(row["forecast_correct"] or 0) for row in directional)
        briers = [
            float(row["forecast_brier"])
            for row in probability_rows
            if row["forecast_brier"] is not None
        ]
        status_counts: dict[str, int] = {}
        grade_counts: dict[str, int] = {}
        for row in probability_rows:
            status = str(row["forecast_status"] or "UNKNOWN")
            grade = str(row["forecast_grade"] or "UNKNOWN")
            status_counts[status] = status_counts.get(status, 0) + 1
            grade_counts[grade] = grade_counts.get(grade, 0) + 1
        n = len(probability_rows)
        return {
            "n": n,
            "n_directional": len(directional),
            "coverage": round(len(directional) / n, 4) if n else 0.0,
            "accuracy": round(wins / len(directional), 4) if directional else None,
            "brier": round(sum(briers) / len(briers), 6) if briers else None,
            "insufficient": n < min_n,
            "min_n": min_n,
            "status_counts": status_counts,
            "grade_counts": grade_counts,
        }

    def forecast_analytics(self, min_n: int = 30) -> dict:
        analytics = super().forecast_analytics(min_n)
        rows = self.conn.execute(
            """
            SELECT combo_key, forecast_direction, forecast_p_up,
                   forecast_confidence, forecast_grade, forecast_status,
                   forecast_correct, forecast_brier
            FROM forecasts
            WHERE official_result IS NOT NULL
            """
        ).fetchall()
        analytics.setdefault("overall", {})["research_forecast"] = (
            self._research_metrics(rows, min_n)
        )
        per_combo_rows: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            per_combo_rows.setdefault(row["combo_key"], []).append(row)
        per_combo = analytics.setdefault("per_combo", {})
        for combo_key, combo_rows in per_combo_rows.items():
            per_combo.setdefault(combo_key, {})["research_forecast"] = (
                self._research_metrics(combo_rows, min_n)
            )
        return analytics

    def stats(self) -> dict:
        stats = super().stats()
        stats["research_forecasts"] = self.conn.execute(
            "SELECT COUNT(*) FROM forecasts WHERE forecast_p_up IS NOT NULL"
        ).fetchone()[0]
        stats["labeled_research_forecasts"] = self.conn.execute(
            """
            SELECT COUNT(*) FROM forecasts
            WHERE forecast_p_up IS NOT NULL AND official_result IS NOT NULL
            """
        ).fetchone()[0]
        return stats
