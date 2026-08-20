"""Chronological OOS calibration and Wilson uncertainty for P2.6.

Only *past outer-test predictions* may contribute to a current estimate.  The
module never uses the current market label, in-sample predictions or future OOS
rows.  Scope fallback is explicit: combo -> horizon -> overall.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

import numpy as np
from sklearn.linear_model import LogisticRegression

from p26_config import P26Settings
from p26_eval import ensure_eval_schema
from p26_schema import connect_p26


@dataclass(frozen=True)
class WilsonInterval:
    lower: Optional[float]
    upper: Optional[float]
    wins: int
    n: int
    z: float


@dataclass(frozen=True)
class CalibrationRow:
    condition_id: str
    decision_ts_ms: int
    combo_key: str
    horizon: str
    p_up_raw: float
    label_up: int


@dataclass(frozen=True)
class ConservativeProbability:
    p_up_raw: float
    p_up_calibrated: float
    p_lower_up: Optional[float]
    p_lower_down: Optional[float]
    p_upper_up: Optional[float]
    bucket_low: Optional[float]
    bucket_high: Optional[float]
    bucket_wins: int
    bucket_n: int
    scope: str
    source: str
    calibrator_source: str
    history_max_ts_ms: Optional[int]
    cutoff_ts_ms: int

    @property
    def ready(self) -> bool:
        return self.p_lower_up is not None and self.p_lower_down is not None

    def selected_lower(self, side: str) -> Optional[float]:
        normalized = side.strip().upper()
        if normalized == "UP":
            return self.p_lower_up
        if normalized == "DOWN":
            return self.p_lower_down
        raise ValueError(f"invalid side: {side}")


def wilson_interval(wins: int, n: int, z: float = 1.96) -> WilsonInterval:
    """Two-sided Wilson score interval for a Bernoulli success rate."""
    wins = int(wins)
    n = int(n)
    z = float(z)
    if n < 0 or wins < 0 or wins > n:
        raise ValueError("wins/n invalid")
    if z <= 0:
        raise ValueError("z must be positive")
    if n == 0:
        return WilsonInterval(None, None, wins, n, z)
    phat = wins / n
    z2 = z * z
    denominator = 1.0 + z2 / n
    centre = (phat + z2 / (2.0 * n)) / denominator
    radius = (
        z
        * math.sqrt((phat * (1.0 - phat) / n) + z2 / (4.0 * n * n))
        / denominator
    )
    return WilsonInterval(
        max(0.0, centre - radius),
        min(1.0, centre + radius),
        wins,
        n,
        z,
    )


def _clip_probability(value: float) -> float:
    return max(1e-6, min(1.0 - 1e-6, float(value)))


def _logit(value: float) -> float:
    p = _clip_probability(value)
    return math.log(p / (1.0 - p))


def bucket_bounds(probability: float, width: float) -> tuple[float, float]:
    if not 0 < width <= 0.25:
        raise ValueError("bucket width must be in (0,0.25]")
    p = max(0.0, min(1.0, float(probability)))
    index = min(int(p / width), max(0, math.ceil(1.0 / width) - 1))
    low = index * width
    high = min(1.0, low + width)
    return low, high


class PlattCalibrator:
    """Frozen one-dimensional Platt calibrator fitted on past OOS rows only."""

    def __init__(self, model: Optional[LogisticRegression], source: str) -> None:
        self.model = model
        self.source = source

    @classmethod
    def fit(
        cls,
        rows: Sequence[CalibrationRow],
        *,
        min_rows: int,
        random_seed: int,
    ) -> "PlattCalibrator":
        labels = [row.label_up for row in rows]
        if len(rows) < min_rows or len(set(labels)) < 2:
            return cls(None, "IDENTITY_INSUFFICIENT")
        X = np.asarray([[_logit(row.p_up_raw)] for row in rows], dtype=float)
        y = np.asarray(labels, dtype=int)
        model = LogisticRegression(
            C=1.0,
            solver="lbfgs",
            random_state=random_seed,
            max_iter=1000,
        )
        model.fit(X, y)
        return cls(model, "PLATT_PAST_OOS")

    def transform(self, probability: float) -> float:
        if self.model is None:
            return _clip_probability(probability)
        p = float(self.model.predict_proba([[_logit(probability)]])[0, 1])
        return _clip_probability(p)


def ensure_calibration_schema(conn: sqlite3.Connection) -> None:
    ensure_eval_schema(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS p26_calibration_audit (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            query_id                TEXT NOT NULL,
            cutoff_ts_ms            INTEGER NOT NULL,
            combo_key               TEXT NOT NULL,
            horizon                 TEXT NOT NULL,
            p_up_raw                REAL NOT NULL,
            p_up_calibrated         REAL NOT NULL,
            p_lower_up              REAL,
            p_upper_up              REAL,
            p_lower_down            REAL,
            scope                   TEXT NOT NULL,
            source                  TEXT NOT NULL,
            calibrator_source       TEXT NOT NULL,
            bucket_low              REAL,
            bucket_high             REAL,
            bucket_wins             INTEGER NOT NULL,
            bucket_n                INTEGER NOT NULL,
            history_max_ts_ms       INTEGER,
            created_at_ms           INTEGER NOT NULL,
            UNIQUE(query_id, cutoff_ts_ms)
        );
        """
    )
    conn.commit()


def load_past_oos_rows(
    conn: sqlite3.Connection,
    *,
    cutoff_ts_ms: int,
    model_version: Optional[str] = None,
) -> list[CalibrationRow]:
    params: list[object] = [int(cutoff_ts_ms)]
    model_clause = ""
    if model_version:
        model_clause = " AND model_version=?"
        params.append(model_version)
    rows = conn.execute(
        f"""
        SELECT condition_id,decision_ts_ms,combo_key,horizon,p_up_raw,official_label
        FROM p26_oos_predictions
        WHERE role='OUTER_TEST'
          AND decision_ts_ms < ?
          {model_clause}
        ORDER BY decision_ts_ms,condition_id
        """,
        params,
    ).fetchall()
    return [
        CalibrationRow(
            condition_id=str(row["condition_id"]),
            decision_ts_ms=int(row["decision_ts_ms"]),
            combo_key=str(row["combo_key"]),
            horizon=str(row["horizon"]),
            p_up_raw=float(row["p_up_raw"]),
            label_up=int(row["official_label"]),
        )
        for row in rows
    ]


def _scope_rows(
    rows: Iterable[CalibrationRow],
    *,
    combo_key: str,
    horizon: str,
) -> list[tuple[str, list[CalibrationRow]]]:
    rows = list(rows)
    return [
        ("PER_COMBO", [row for row in rows if row.combo_key == combo_key]),
        ("HORIZON", [row for row in rows if row.horizon == horizon]),
        ("OVERALL", rows),
    ]


def conservative_probability(
    conn: sqlite3.Connection,
    settings: P26Settings,
    *,
    p_up_raw: float,
    combo_key: str,
    horizon: str,
    cutoff_ts_ms: int,
    model_version: Optional[str] = None,
) -> ConservativeProbability:
    """Return calibrated probability and side-specific conservative bounds.

    The current market label is unavailable by construction: only OOS rows with
    ``decision_ts_ms < cutoff_ts_ms`` are loaded.
    """
    ensure_calibration_schema(conn)
    raw = _clip_probability(p_up_raw)
    all_rows = load_past_oos_rows(
        conn,
        cutoff_ts_ms=cutoff_ts_ms,
        model_version=model_version,
    )
    history_max = max((row.decision_ts_ms for row in all_rows), default=None)

    fallback_calibrator = PlattCalibrator.fit(
        all_rows,
        min_rows=settings.calibration_min_bucket_n,
        random_seed=settings.model_random_seed,
    )
    calibrated = fallback_calibrator.transform(raw)

    for scope, scoped_rows in _scope_rows(
        all_rows,
        combo_key=combo_key,
        horizon=horizon,
    ):
        calibrator = PlattCalibrator.fit(
            scoped_rows,
            min_rows=settings.calibration_min_bucket_n,
            random_seed=settings.model_random_seed,
        )
        current_calibrated = calibrator.transform(raw)
        low, high = bucket_bounds(
            current_calibrated,
            settings.calibration_bucket_width,
        )
        bucket: list[CalibrationRow] = []
        for row in scoped_rows:
            transformed = calibrator.transform(row.p_up_raw)
            in_bucket = low <= transformed < high or (
                high >= 1.0 and transformed <= high
            )
            if in_bucket:
                bucket.append(row)
        if len(bucket) < settings.calibration_min_bucket_n:
            continue
        wins = sum(row.label_up for row in bucket)
        interval = wilson_interval(wins, len(bucket), settings.calibration_z)
        assert interval.lower is not None and interval.upper is not None
        # A small Beta(1,1) posterior mean is used as the point estimate, while
        # Wilson bounds drive the conservative edge decision.
        point = (wins + 1.0) / (len(bucket) + 2.0)
        return ConservativeProbability(
            p_up_raw=raw,
            p_up_calibrated=point,
            p_lower_up=interval.lower,
            p_lower_down=1.0 - interval.upper,
            p_upper_up=interval.upper,
            bucket_low=low,
            bucket_high=high,
            bucket_wins=wins,
            bucket_n=len(bucket),
            scope=scope,
            source=f"{scope}_WILSON",
            calibrator_source=calibrator.source,
            history_max_ts_ms=history_max,
            cutoff_ts_ms=int(cutoff_ts_ms),
        )

    return ConservativeProbability(
        p_up_raw=raw,
        p_up_calibrated=calibrated,
        p_lower_up=None,
        p_lower_down=None,
        p_upper_up=None,
        bucket_low=None,
        bucket_high=None,
        bucket_wins=0,
        bucket_n=0,
        scope="NONE",
        source="INSUFFICIENT_OOS_BUCKET",
        calibrator_source=fallback_calibrator.source,
        history_max_ts_ms=history_max,
        cutoff_ts_ms=int(cutoff_ts_ms),
    )
