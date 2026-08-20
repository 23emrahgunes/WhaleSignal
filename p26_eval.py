"""Purged nested walk-forward evaluation for the P2.6 fair-value champion."""
from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

import numpy as np
from sklearn.base import clone

from p26_config import P26Settings, get_p26_settings
from p26_fair_value import new_champion
from p26_features import EXTERNAL_FEATURE_NAMES
from p26_schema import connect_p26, ensure_p26_schema
from p26_walkforward import NestedFold, TemporalRecord, purged_nested_folds


@dataclass(frozen=True)
class EvalRecord(TemporalRecord):
    combo_key: str
    asset: str
    horizon: str
    features: tuple[float, ...]
    label_up: int
    market_p_up: Optional[float]


@dataclass(frozen=True)
class ProbabilityMetrics:
    n: int
    brier: Optional[float]
    log_loss: Optional[float]
    accuracy: Optional[float]


@dataclass(frozen=True)
class FoldEvaluation:
    fold_id: str
    selected_c: float
    train_n: int
    validation_n: int
    test_n: int
    model: ProbabilityMetrics
    market: ProbabilityMetrics
    naive: ProbabilityMetrics
    paired_brier_delta_vs_market: Optional[float]
    paired_log_loss_delta_vs_market: Optional[float]


def _probability_metrics(y: np.ndarray, p: np.ndarray) -> ProbabilityMetrics:
    if len(y) == 0:
        return ProbabilityMetrics(0, None, None, None)
    clipped = np.clip(np.asarray(p, dtype=float), 1e-6, 1.0 - 1e-6)
    labels = np.asarray(y, dtype=int)
    brier = float(np.mean((clipped - labels) ** 2))
    loss = float(
        -np.mean(labels * np.log(clipped) + (1 - labels) * np.log(1 - clipped))
    )
    accuracy = float(np.mean((clipped >= 0.5).astype(int) == labels))
    return ProbabilityMetrics(len(labels), brier, loss, accuracy)


def ensure_eval_schema(conn) -> None:  # noqa: ANN001
    ensure_p26_schema(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS p26_oos_predictions (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            condition_id        TEXT NOT NULL,
            fold_id             TEXT NOT NULL,
            decision_ts_ms      INTEGER NOT NULL,
            combo_key           TEXT NOT NULL,
            horizon             TEXT NOT NULL,
            p_up_raw            REAL NOT NULL,
            official_label      INTEGER NOT NULL CHECK(official_label IN (0,1)),
            market_p_up         REAL,
            selected_c          REAL NOT NULL,
            role                TEXT NOT NULL DEFAULT 'OUTER_TEST',
            model_version       TEXT NOT NULL,
            created_at_ms       INTEGER NOT NULL,
            UNIQUE(condition_id, fold_id, model_version)
        );
        CREATE INDEX IF NOT EXISTS idx_p26_oos_time
        ON p26_oos_predictions(decision_ts_ms,combo_key);
        """
    )
    conn.commit()


def load_eval_records(p26_db_path: str) -> list[EvalRecord]:
    conn = connect_p26(p26_db_path, read_only=True)
    try:
        rows = conn.execute(
            """
            SELECT c.condition_id,c.combo_key,c.asset,c.horizon,
                   c.market_start_ts_ms,c.market_end_ts_ms,c.decision_ts_ms,
                   c.feature_vector_json,c.up_mid,l.official_label
            FROM p26_canonical_rows c
            JOIN p26_labels l ON l.condition_id=c.condition_id
            WHERE c.training_eligible=1 AND l.official_label IS NOT NULL
            ORDER BY c.decision_ts_ms,c.condition_id
            """
        ).fetchall()
    finally:
        conn.close()
    output: list[EvalRecord] = []
    for row in rows:
        payload = json.loads(str(row["feature_vector_json"]))
        output.append(
            EvalRecord(
                condition_id=str(row["condition_id"]),
                combo_key=str(row["combo_key"]),
                asset=str(row["asset"]),
                horizon=str(row["horizon"]),
                market_start_ts_ms=int(row["market_start_ts_ms"]),
                market_end_ts_ms=int(row["market_end_ts_ms"]),
                decision_ts_ms=int(row["decision_ts_ms"]),
                features=tuple(
                    float(payload.get(name, 0.0) or 0.0)
                    for name in EXTERNAL_FEATURE_NAMES
                ),
                label_up=int(row["official_label"]),
                market_p_up=(float(row["up_mid"]) if row["up_mid"] is not None else None),
            )
        )
    return output


def _select(records: Sequence[EvalRecord], ids: Sequence[str]) -> list[EvalRecord]:
    wanted = set(ids)
    return [record for record in records if record.condition_id in wanted]


def _xy(records: Sequence[EvalRecord]) -> tuple[np.ndarray, np.ndarray]:
    return (
        np.asarray([record.features for record in records], dtype=float),
        np.asarray([record.label_up for record in records], dtype=int),
    )


def _predict_pipeline(settings: P26Settings, C: float, train: Sequence[EvalRecord], test: Sequence[EvalRecord]) -> np.ndarray:
    model = new_champion(settings)
    model.set_params(model__C=float(C))
    X_train, y_train = _xy(train)
    X_test, _ = _xy(test)
    model.fit(X_train, y_train)
    probabilities = model.predict_proba(X_test)
    classes = list(model.named_steps["model"].classes_)
    return np.asarray(probabilities[:, classes.index(1)], dtype=float)


def _select_c(
    settings: P26Settings,
    train: Sequence[EvalRecord],
    validation: Sequence[EvalRecord],
    candidates: Iterable[float],
) -> float:
    _, y_validation = _xy(validation)
    best: Optional[tuple[float, float]] = None
    for candidate in candidates:
        p = _predict_pipeline(settings, candidate, train, validation)
        score = _probability_metrics(y_validation, p).log_loss
        if score is None:
            continue
        item = (score, float(candidate))
        if best is None or item < best:
            best = item
    if best is None:
        raise RuntimeError("unable to select regularization")
    return best[1]


def evaluate_fold(
    settings: P26Settings,
    records: Sequence[EvalRecord],
    fold: NestedFold,
    *,
    c_candidates: Sequence[float] = (0.1, 0.3, 1.0, 3.0),
) -> tuple[FoldEvaluation, list[dict]]:
    train = _select(records, fold.train_ids)
    validation = _select(records, fold.validation_ids)
    test = _select(records, fold.test_ids)
    if len({record.label_up for record in train}) < 2:
        raise RuntimeError(f"{fold.fold_id}: train has one class")
    selected_c = _select_c(settings, train, validation, c_candidates)
    refit = [*train, *validation]
    p_model = _predict_pipeline(settings, selected_c, refit, test)
    y = np.asarray([record.label_up for record in test], dtype=int)
    model_metrics = _probability_metrics(y, p_model)
    naive = _probability_metrics(y, np.full(len(test), 0.5, dtype=float))

    market_indices = [i for i, record in enumerate(test) if record.market_p_up is not None]
    if market_indices:
        y_market = y[market_indices]
        p_market = np.asarray([test[i].market_p_up for i in market_indices], dtype=float)
        market_metrics = _probability_metrics(y_market, p_market)
        p_model_paired = p_model[market_indices]
        model_paired = _probability_metrics(y_market, p_model_paired)
        brier_delta = (
            model_paired.brier - market_metrics.brier
            if model_paired.brier is not None and market_metrics.brier is not None
            else None
        )
        log_delta = (
            model_paired.log_loss - market_metrics.log_loss
            if model_paired.log_loss is not None and market_metrics.log_loss is not None
            else None
        )
    else:
        market_metrics = ProbabilityMetrics(0, None, None, None)
        brier_delta = None
        log_delta = None

    predictions = [
        {
            "condition_id": record.condition_id,
            "fold_id": fold.fold_id,
            "decision_ts_ms": record.decision_ts_ms,
            "combo_key": record.combo_key,
            "horizon": record.horizon,
            "p_up_raw": float(p_model[index]),
            "official_label": record.label_up,
            "market_p_up": record.market_p_up,
            "selected_c": selected_c,
        }
        for index, record in enumerate(test)
    ]
    evaluation = FoldEvaluation(
        fold_id=fold.fold_id,
        selected_c=selected_c,
        train_n=len(train),
        validation_n=len(validation),
        test_n=len(test),
        model=model_metrics,
        market=market_metrics,
        naive=naive,
        paired_brier_delta_vs_market=brier_delta,
        paired_log_loss_delta_vs_market=log_delta,
    )
    return evaluation, predictions


def run_nested_evaluation(
    settings: P26Settings,
    *,
    min_train_records: Optional[int] = None,
    min_validation_records: int = 10,
    min_test_records: int = 10,
    validation_cluster_count: int = 1,
    test_cluster_count: int = 1,
    persist: bool = True,
) -> dict:
    records = load_eval_records(settings.p26_db_path)
    folds = purged_nested_folds(
        records,
        embargo_ms=settings.walkforward_embargo_ms,
        min_train_records=(
            settings.model_min_train_markets
            if min_train_records is None
            else min_train_records
        ),
        min_validation_records=min_validation_records,
        min_test_records=min_test_records,
        validation_cluster_count=validation_cluster_count,
        test_cluster_count=test_cluster_count,
    )
    evaluations: list[FoldEvaluation] = []
    all_predictions: list[dict] = []
    errors: list[dict] = []
    for fold in folds:
        try:
            evaluation, predictions = evaluate_fold(settings, records, fold)
        except Exception as exc:  # noqa: BLE001
            errors.append({"fold_id": fold.fold_id, "error": repr(exc)})
            continue
        evaluations.append(evaluation)
        all_predictions.extend(predictions)

    if persist and all_predictions:
        conn = connect_p26(settings.p26_db_path)
        ensure_eval_schema(conn)
        try:
            now_ms = int(time.time() * 1000)
            conn.executemany(
                """
                INSERT OR REPLACE INTO p26_oos_predictions(
                    condition_id,fold_id,decision_ts_ms,combo_key,horizon,p_up_raw,
                    official_label,market_p_up,selected_c,role,model_version,created_at_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,'OUTER_TEST',?,?)
                """,
                [
                    (
                        row["condition_id"],row["fold_id"],row["decision_ts_ms"],
                        row["combo_key"],row["horizon"],row["p_up_raw"],
                        row["official_label"],row["market_p_up"],row["selected_c"],
                        settings.model_artifact_version,now_ms,
                    )
                    for row in all_predictions
                ],
            )
            conn.commit()
        finally:
            conn.close()

    return {
        "status": "OK" if evaluations else "INSUFFICIENT_OR_INVALID_FOLDS",
        "record_count": len(records),
        "fold_count": len(evaluations),
        "folds": [
            {
                **asdict(evaluation),
                "model": asdict(evaluation.model),
                "market": asdict(evaluation.market),
                "naive": asdict(evaluation.naive),
            }
            for evaluation in evaluations
        ],
        "errors": errors,
        "prediction_count": len(all_predictions),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="reports/p26/nested_walkforward.json")
    args = parser.parse_args()
    settings = get_p26_settings()
    report = run_nested_evaluation(settings)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "fold_count": report["fold_count"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
