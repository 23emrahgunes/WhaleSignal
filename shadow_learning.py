"""Restart-safe P2.5 shadow learning and calibration updates.

Only markets with authoritative official labels and label_status MATCH or
OFFICIAL_ONLY are eligible.  MISMATCH rows are never learned.  SQLite audit tables
make every model/calibration update idempotent across restarts.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from calibration import CALIBRATION_VERSION, CalSample, CalibrationBook
from direction_model import MODEL_VERSION, DirectionModel
from feature_codec import feature_vector_from_row
from recorder import Recorder

log = logging.getLogger("direction_engine.learning")


@dataclass(frozen=True)
class LearningUpdate:
    model_markets: int = 0
    calibration_markets: int = 0
    feature_rows: int = 0
    forecast_rows: int = 0

    def to_dict(self) -> dict:
        return {
            "model_markets": self.model_markets,
            "calibration_markets": self.calibration_markets,
            "feature_rows": self.feature_rows,
            "forecast_rows": self.forecast_rows,
        }


def _train_pending(recorder: Recorder, model: DirectionModel) -> tuple[int, int]:
    markets = rows_used = 0
    for pending in recorder.pending_model_updates(MODEL_VERSION):
        condition_id = str(pending["condition_id"])
        combo_key = str(pending["combo_key"])
        label = str(pending["official_result"])
        vectors = [
            vector
            for row in recorder.feature_rows(condition_id)
            for vector in [feature_vector_from_row(row)]
            if vector is not None and vector.feature_ready
        ]
        if not vectors:
            continue
        learned = model.learn_with_label(combo_key, vectors, 1 if label == "UP" else 0)
        if not learned:
            continue
        recorder.mark_model_updated(
            condition_id, MODEL_VERSION, combo_key, label, len(vectors)
        )
        markets += 1
        rows_used += len(vectors)
    return markets, rows_used


def _calibrate_pending(
    recorder: Recorder,
    calibration: CalibrationBook,
) -> tuple[int, int]:
    markets = rows_used = 0
    for pending in recorder.pending_calibration_updates(
        CALIBRATION_VERSION, MODEL_VERSION
    ):
        condition_id = str(pending["condition_id"])
        combo_key = str(pending["combo_key"])
        outcome_up = str(pending["official_result"]) == "UP"
        forecasts = [
            row for row in recorder.forecast_rows(condition_id, MODEL_VERSION)
            if row.get("p_up_raw") is not None
        ]
        if not forecasts:
            continue
        for row in forecasts:
            decision = str(row.get("decision") or "ABSTAIN")
            calibration.record(
                combo_key,
                CalSample(
                    decided=decision in {"UP", "DOWN"},
                    outcome_up=outcome_up,
                    p_up=float(row["p_up_raw"]),
                    decision_up=(decision == "UP") if decision in {"UP", "DOWN"} else None,
                    confidence=float(row.get("confidence") or 0.0),
                    market_implied_up=row.get("market_implied_up"),
                    market_id=condition_id,
                    checkpoint_sec=row.get("checkpoint_sec"),
                    model_version=row.get("model_version"),
                    p_up_no_clob=row.get("p_up_no_clob"),
                    ptb_baseline=row.get("baseline_ptb"),
                    coinflip=float(row.get("baseline_coinflip") or 0.5),
                ),
            )
        recorder.mark_calibration_updated(
            condition_id, CALIBRATION_VERSION, MODEL_VERSION,
            combo_key, len(forecasts),
        )
        markets += 1
        rows_used += len(forecasts)
    return markets, rows_used


def apply_pending_updates(
    recorder: Recorder,
    model: DirectionModel,
    calibration: CalibrationBook,
    *,
    training_enabled: bool,
    calibration_enabled: bool,
    model_path: str,
    calibration_path: str,
) -> LearningUpdate:
    model_markets = feature_rows = calibration_markets = forecast_rows = 0
    if training_enabled:
        model_markets, feature_rows = _train_pending(recorder, model)
        if model_markets and not model.save(model_path):
            log.warning("model artifact could not be saved")
    if calibration_enabled:
        calibration_markets, forecast_rows = _calibrate_pending(recorder, calibration)
        if calibration_markets and not calibration.save(calibration_path):
            log.warning("calibration artifact could not be saved")
    update = LearningUpdate(
        model_markets=model_markets,
        calibration_markets=calibration_markets,
        feature_rows=feature_rows,
        forecast_rows=forecast_rows,
    )
    if model_markets or calibration_markets:
        log.info("shadow learning update: %s", update.to_dict())
    return update
