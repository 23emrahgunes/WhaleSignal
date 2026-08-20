"""Independent external-only fair-value champion for P2.6.

Champion model: median imputer + frozen RobustScaler + L2 LogisticRegression.
The model never consumes Polymarket/CLOB features.  HistGradientBoosting remains
an offline challenger and cannot replace the champion without P2.6.6 promotion.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import RobustScaler

from p26_artifact import LoadedArtifact, load_artifact, save_artifact
from p26_config import P26Settings
from p26_features import (
    EXTERNAL_FEATURE_NAMES,
    EXTERNAL_FEATURE_SCHEMA_VERSION,
    assert_external_only,
    schema_hash,
)
from p26_schema import connect_p26


@dataclass(frozen=True)
class TrainingMatrix:
    X: np.ndarray
    y: np.ndarray
    condition_ids: tuple[str, ...]
    decision_ts_ms: tuple[int, ...]
    feature_names: tuple[str, ...] = EXTERNAL_FEATURE_NAMES

    @property
    def n_markets(self) -> int:
        return len(self.y)

    @property
    def up_count(self) -> int:
        return int(np.sum(self.y == 1))

    @property
    def down_count(self) -> int:
        return int(np.sum(self.y == 0))


@dataclass(frozen=True)
class TrainingOutcome:
    status: str
    reason: str
    n_markets: int
    up_count: int
    down_count: int
    artifact: Optional[LoadedArtifact] = None


def load_training_matrix(
    p26_db_path: str,
    *,
    cutoff_ms: Optional[int] = None,
    eligible_only: bool = True,
) -> TrainingMatrix:
    conn = connect_p26(p26_db_path, read_only=True)
    try:
        clauses = ["l.official_label IS NOT NULL"]
        params: list[object] = []
        if eligible_only:
            clauses.append("c.training_eligible=1")
        if cutoff_ms is not None:
            clauses.append("c.decision_ts_ms<=?")
            params.append(int(cutoff_ms))
        rows = conn.execute(
            f"""
            SELECT c.condition_id,c.decision_ts_ms,c.feature_vector_json,l.official_label
            FROM p26_canonical_rows c
            JOIN p26_labels l ON l.condition_id=c.condition_id
            WHERE {' AND '.join(clauses)}
            ORDER BY c.decision_ts_ms ASC,c.condition_id ASC
            """,
            params,
        ).fetchall()
    finally:
        conn.close()
    X: list[list[float]] = []
    y: list[int] = []
    condition_ids: list[str] = []
    decision_times: list[int] = []
    names = tuple(EXTERNAL_FEATURE_NAMES)
    assert_external_only(names)
    for row in rows:
        values = json.loads(str(row["feature_vector_json"]))
        X.append([float(values.get(name, 0.0) or 0.0) for name in names])
        y.append(int(row["official_label"]))
        condition_ids.append(str(row["condition_id"]))
        decision_times.append(int(row["decision_ts_ms"]))
    matrix = np.asarray(X, dtype=float)
    if matrix.size == 0:
        matrix = np.empty((0, len(names)), dtype=float)
    return TrainingMatrix(
        X=matrix,
        y=np.asarray(y, dtype=int),
        condition_ids=tuple(condition_ids),
        decision_ts_ms=tuple(decision_times),
        feature_names=names,
    )


def new_champion(settings: P26Settings) -> Pipeline:
    return Pipeline(
        steps=[
            ("imputer", SimpleImputer(strategy="median", keep_empty_features=True)),
            (
                "scaler",
                RobustScaler(quantile_range=(10.0, 90.0), unit_variance=True),
            ),
            (
                "model",
                LogisticRegression(
                    C=float(settings.model_regularization_c),
                    solver="liblinear",
                    class_weight="balanced",
                    max_iter=2_000,
                    random_state=int(settings.model_random_seed),
                ),
            ),
        ]
    )


def new_challenger(settings: P26Settings) -> Pipeline:
    return Pipeline(
        steps=[
            ("imputer", SimpleImputer(strategy="median", keep_empty_features=True)),
            (
                "model",
                HistGradientBoostingClassifier(
                    learning_rate=0.05,
                    max_iter=200,
                    l2_regularization=1.0,
                    random_state=int(settings.model_random_seed),
                ),
            ),
        ]
    )


def _condition_hash(condition_ids: Sequence[str]) -> str:
    return hashlib.sha256("\n".join(condition_ids).encode("utf-8")).hexdigest()


def train_champion(
    matrix: TrainingMatrix,
    settings: P26Settings,
    *,
    output_dir: Optional[Path] = None,
    code_commit: str = "UNKNOWN",
    artifact_id: Optional[str] = None,
) -> TrainingOutcome:
    n = matrix.n_markets
    if n < settings.model_min_train_markets:
        return TrainingOutcome(
            "INSUFFICIENT_DATA",
            f"n_markets={n}<{settings.model_min_train_markets}",
            n,
            matrix.up_count,
            matrix.down_count,
        )
    if min(matrix.up_count, matrix.down_count) < settings.model_min_class_markets:
        return TrainingOutcome(
            "INSUFFICIENT_CLASS_BALANCE",
            f"up={matrix.up_count} down={matrix.down_count} min={settings.model_min_class_markets}",
            n,
            matrix.up_count,
            matrix.down_count,
        )
    assert_external_only(matrix.feature_names)
    pipeline = new_champion(settings)
    pipeline.fit(matrix.X, matrix.y)
    artifact_id = artifact_id or (
        settings.model_artifact_version
        + "_"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    cutoff = max(matrix.decision_ts_ms) if matrix.decision_ts_ms else 0
    output_dir = output_dir or Path(settings.model_dir)
    artifact = save_artifact(
        pipeline=pipeline,
        output_dir=output_dir,
        stem=artifact_id,
        manifest_without_hash={
            "artifact_id": artifact_id,
            "artifact_version": settings.model_artifact_version,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "code_commit": code_commit,
            "feature_schema_version": settings.feature_schema_version,
            "feature_schema_hash": schema_hash(
                matrix.feature_names,
                settings.feature_schema_version,
            ),
            "feature_names_in_exact_order": list(matrix.feature_names),
            "model_type": "LogisticRegression_L2",
            "scaler_type": "RobustScaler_frozen",
            "imputer_type": "SimpleImputer_median_frozen",
            "regularization": {"C": settings.model_regularization_c, "penalty": "l2"},
            "random_seed": settings.model_random_seed,
            "training_cutoff_ms": cutoff,
            "train_market_count": n,
            "train_up_count": matrix.up_count,
            "train_down_count": matrix.down_count,
            "train_condition_ids_sha256": _condition_hash(matrix.condition_ids),
            "calibration_artifact_id": None,
        },
    )
    return TrainingOutcome(
        "TRAINED",
        "frozen champion artifact created",
        n,
        matrix.up_count,
        matrix.down_count,
        artifact,
    )


class FrozenFairValueModel:
    def __init__(self, artifact: LoadedArtifact) -> None:
        assert_external_only(artifact.manifest.feature_names_in_exact_order)
        self.artifact = artifact
        self.pipeline = artifact.pipeline
        self.feature_names = tuple(artifact.manifest.feature_names_in_exact_order)
        self._scaler_snapshot = self.scaler_state()

    @classmethod
    def load(cls, manifest_path: Path, settings: P26Settings) -> "FrozenFairValueModel":
        artifact = load_artifact(
            manifest_path,
            expected_feature_schema_hash=schema_hash(
                EXTERNAL_FEATURE_NAMES,
                settings.feature_schema_version,
            ),
        )
        return cls(artifact)

    def scaler_state(self) -> dict[str, list[float]]:
        scaler = self.pipeline.named_steps.get("scaler")
        if scaler is None:
            return {}
        result: dict[str, list[float]] = {}
        for name in ("center_", "scale_"):
            value = getattr(scaler, name, None)
            if value is not None:
                result[name] = np.asarray(value, dtype=float).tolist()
        return result

    def predict_p_up(self, X: np.ndarray) -> np.ndarray:
        before = self.scaler_state()
        probabilities = self.pipeline.predict_proba(np.asarray(X, dtype=float))
        classes = list(self.pipeline.named_steps["model"].classes_)
        idx = classes.index(1)
        after = self.scaler_state()
        if before != after or after != self._scaler_snapshot:
            raise RuntimeError("FROZEN_SCALER_MUTATED_DURING_INFERENCE")
        return np.asarray(probabilities[:, idx], dtype=float)
