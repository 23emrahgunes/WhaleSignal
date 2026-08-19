"""P2.4 probability calibration and selective decision thresholds.

Calibration is prequential: a forecast is recorded before its market label is used
for model training. Until enough resolved forecasts exist, raw probabilities and
conservative default thresholds are returned with an explicit source label.
"""
from __future__ import annotations

import math
import os
import pickle
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_BUCKET_EDGES = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 1.01]
_BUCKET_LABELS = ["50-55", "55-60", "60-65", "65-70", "70-75", "75-80", "80+"]
_SELECTIVE_THRESHOLDS = [0.50, 0.55, 0.575, 0.60, 0.625, 0.65, 0.675, 0.70, 0.75, 0.80]
CALIBRATION_SCHEMA_VERSION = 2


@dataclass
class CalSample:
    decided: bool
    outcome_up: bool
    p_up: Optional[float] = None
    decision_up: Optional[bool] = None
    confidence: float = 0.0
    market_implied_up: Optional[float] = None
    predictability: float = 0.0
    regime: Optional[str] = None
    model_version: Optional[str] = None
    checkpoint_sec: Optional[int] = None


@dataclass(frozen=True)
class CalibrationDecision:
    p_up: float
    source: str
    n: int


@dataclass(frozen=True)
class ThresholdDecision:
    threshold: float
    source: str
    n: int
    covered: int
    accuracy: Optional[float]
    wilson_lower: Optional[float]


def _clip_probability(p: float) -> float:
    return max(1e-6, min(1.0 - 1e-6, float(p)))


def _wilson_lower(wins: int, n: int, z: float = 1.96) -> Optional[float]:
    if n <= 0:
        return None
    phat = wins / n
    denom = 1.0 + z * z / n
    centre = phat + z * z / (2.0 * n)
    radius = z * math.sqrt((phat * (1.0 - phat) + z * z / (4.0 * n)) / n)
    return max(0.0, (centre - radius) / denom)


def _log_loss(p: float, outcome_up: bool) -> float:
    p = _clip_probability(p)
    y = 1.0 if outcome_up else 0.0
    return -(y * math.log(p) + (1.0 - y) * math.log(1.0 - p))


class CalibrationTracker:
    def __init__(self, maxlen: int = 20000) -> None:
        self.samples: deque[CalSample] = deque(maxlen=maxlen)

    def record(self, sample: CalSample) -> None:
        self.samples.append(sample)

    def _probability_rows(self) -> list[CalSample]:
        return [s for s in self.samples if s.p_up is not None]

    def _decided(self) -> list[CalSample]:
        return [
            s
            for s in self.samples
            if s.decided and s.p_up is not None and s.decision_up is not None
        ]

    def calibrate(
        self,
        p_up: float,
        min_bin_samples: int,
        prior_strength: float,
    ) -> CalibrationDecision:
        raw = _clip_probability(p_up)
        rows = self._probability_rows()
        if not rows:
            return CalibrationDecision(raw, "RAW_INSUFFICIENT", 0)

        idx = min(9, max(0, int(raw * 10.0)))
        lo, hi = idx / 10.0, (idx + 1) / 10.0
        bucket = [
            s for s in rows
            if lo <= float(s.p_up) < hi or (idx == 9 and float(s.p_up) == 1.0)
        ]
        n = len(bucket)
        if n < min_bin_samples:
            return CalibrationDecision(raw, "RAW_INSUFFICIENT_BIN", n)

        wins = sum(1 for s in bucket if s.outcome_up)
        mean_raw = sum(float(s.p_up) for s in bucket) / n
        calibrated = (
            wins + max(0.0, prior_strength) * mean_raw
        ) / (n + max(0.0, prior_strength))
        return CalibrationDecision(
            _clip_probability(calibrated),
            "RELIABILITY_BIN",
            n,
        )

    def threshold(
        self,
        default: float,
        min_samples: int,
        min_covered: int,
        target_accuracy: float,
    ) -> ThresholdDecision:
        rows = self._probability_rows()
        n_total = len(rows)
        if n_total < min_samples:
            return ThresholdDecision(
                default, "DEFAULT_INSUFFICIENT", n_total, 0, None, None
            )

        floor = max(0.5, float(default))
        for threshold in _SELECTIVE_THRESHOLDS:
            if threshold + 1e-12 < floor:
                continue
            covered = [
                s for s in rows
                if max(float(s.p_up), 1.0 - float(s.p_up)) >= threshold
            ]
            n = len(covered)
            if n < min_covered:
                continue
            wins = sum(
                1 for s in covered
                if ((float(s.p_up) >= 0.5) == bool(s.outcome_up))
            )
            accuracy = wins / n
            lower = _wilson_lower(wins, n)
            if accuracy >= target_accuracy and lower is not None and lower > 0.50:
                return ThresholdDecision(
                    threshold,
                    "LEARNED_SELECTIVE",
                    n_total,
                    n,
                    accuracy,
                    lower,
                )

        return ThresholdDecision(
            default, "DEFAULT_NO_STABLE_EDGE", n_total, 0, None, None
        )

    def brier(self) -> Optional[float]:
        rows = self._probability_rows()
        if not rows:
            return None
        return sum(
            (float(s.p_up) - (1.0 if s.outcome_up else 0.0)) ** 2
            for s in rows
        ) / len(rows)

    def log_loss(self) -> Optional[float]:
        rows = self._probability_rows()
        if not rows:
            return None
        return sum(_log_loss(float(s.p_up), s.outcome_up) for s in rows) / len(rows)

    def expected_calibration_error(self, n_bins: int = 10) -> Optional[float]:
        rows = self._probability_rows()
        if not rows:
            return None
        total = len(rows)
        ece = 0.0
        for idx in range(n_bins):
            lo, hi = idx / n_bins, (idx + 1) / n_bins
            bucket = [
                s for s in rows
                if lo <= float(s.p_up) < hi
                or (idx == n_bins - 1 and float(s.p_up) == 1.0)
            ]
            if not bucket:
                continue
            mean_p = sum(float(s.p_up) for s in bucket) / len(bucket)
            actual = sum(1 for s in bucket if s.outcome_up) / len(bucket)
            ece += len(bucket) / total * abs(mean_p - actual)
        return ece

    def confidence_buckets(self) -> list[dict]:
        rows = self._decided()
        output: list[dict] = []
        for idx, label in enumerate(_BUCKET_LABELS):
            lo, hi = _BUCKET_EDGES[idx], _BUCKET_EDGES[idx + 1]
            bucket: list[tuple[float, CalSample]] = []
            for sample in rows:
                p = float(sample.p_up)
                chosen = p if sample.decision_up else 1.0 - p
                if lo <= chosen < hi:
                    bucket.append((chosen, sample))
            if not bucket:
                output.append({"bucket": label, "n": 0})
                continue
            n = len(bucket)
            wins = sum(
                1 for _, sample in bucket
                if sample.decision_up == sample.outcome_up
            )
            mean_pred = sum(p for p, _ in bucket) / n
            brier = sum(
                (float(sample.p_up) - (1.0 if sample.outcome_up else 0.0)) ** 2
                for _, sample in bucket
            ) / n
            output.append(
                {
                    "bucket": label,
                    "n": n,
                    "actual_winrate": round(wins / n, 4),
                    "mean_predicted": round(mean_pred, 4),
                    "brier": round(brier, 4),
                }
            )
        return output

    def selective(self) -> list[dict]:
        rows = self._probability_rows()
        total = len(rows)
        if total == 0:
            return []
        output: list[dict] = []
        for threshold in _SELECTIVE_THRESHOLDS:
            covered = [
                s for s in rows
                if max(float(s.p_up), 1.0 - float(s.p_up)) >= threshold
            ]
            n = len(covered)
            wins = sum(
                1 for s in covered
                if ((float(s.p_up) >= 0.5) == s.outcome_up)
            )
            output.append(
                {
                    "prob_threshold": threshold,
                    "coverage": round(n / total, 4),
                    "n": n,
                    "accuracy": round(wins / n, 4) if n else None,
                    "wilson_lower": (
                        round(_wilson_lower(wins, n), 4) if n else None
                    ),
                }
            )
        return output

    def price_edge(self) -> Optional[dict]:
        rows = [
            s for s in self._probability_rows()
            if s.market_implied_up is not None
        ]
        if not rows:
            return None
        edges = [float(s.p_up) - float(s.market_implied_up) for s in rows]
        agree = sum(
            1 for s in rows
            if ((float(s.p_up) - float(s.market_implied_up)) > 0) == s.outcome_up
        )
        return {
            "n": len(rows),
            "mean_edge": round(sum(edges) / len(edges), 4),
            "edge_sign_accuracy": round(agree / len(rows), 4),
        }

    def summary(self, min_n: int) -> dict:
        rows = self._probability_rows()
        decided = self._decided()
        base = {
            "n_decided": len(decided),
            "n_probability": len(rows),
            "n_total": len(self.samples),
            "min_n": min_n,
        }
        if len(rows) < min_n:
            base["insufficient"] = True
            return base
        base["insufficient"] = False
        if decided:
            wins = sum(1 for s in decided if s.decision_up == s.outcome_up)
            base["accuracy"] = round(wins / len(decided), 4)
        else:
            base["accuracy"] = None
        base["brier"] = round(self.brier(), 5) if self.brier() is not None else None
        base["log_loss"] = (
            round(self.log_loss(), 5) if self.log_loss() is not None else None
        )
        ece = self.expected_calibration_error()
        base["ece"] = round(ece, 5) if ece is not None else None
        base["buckets"] = self.confidence_buckets()
        base["selective"] = self.selective()
        base["price_edge"] = self.price_edge()
        return base


class CalibrationBook:
    """Overall + per-combo calibration with atomic persistence."""

    schema_version = CALIBRATION_SCHEMA_VERSION

    def __init__(self, min_n: int = 30) -> None:
        self.min_n = min_n
        self.overall = CalibrationTracker()
        self.per_combo: dict[str, CalibrationTracker] = {}

    def record(self, combo_key: str, sample: CalSample) -> None:
        self.overall.record(sample)
        self.per_combo.setdefault(combo_key, CalibrationTracker()).record(sample)

    def calibrate(
        self,
        combo_key: str,
        p_up: float,
        *,
        min_samples: int,
        min_bin_samples: int,
        prior_strength: float,
    ) -> CalibrationDecision:
        tracker = self.per_combo.get(combo_key)
        if tracker is not None and len(tracker._probability_rows()) >= min_samples:
            decision = tracker.calibrate(p_up, min_bin_samples, prior_strength)
            if decision.source == "RELIABILITY_BIN":
                return CalibrationDecision(
                    decision.p_up, "PER_COMBO_RELIABILITY", decision.n
                )
        overall = self.overall.calibrate(p_up, min_bin_samples, prior_strength)
        if overall.source == "RELIABILITY_BIN":
            return CalibrationDecision(
                overall.p_up, "OVERALL_RELIABILITY", overall.n
            )
        return CalibrationDecision(
            _clip_probability(p_up), overall.source, overall.n
        )

    def decision_threshold(
        self,
        combo_key: str,
        *,
        default: float,
        min_samples: int,
        min_covered: int,
        target_accuracy: float,
    ) -> ThresholdDecision:
        tracker = self.per_combo.get(combo_key)
        if tracker is not None:
            decision = tracker.threshold(
                default, min_samples, min_covered, target_accuracy
            )
            if decision.source == "LEARNED_SELECTIVE":
                return ThresholdDecision(
                    decision.threshold,
                    "PER_COMBO_LEARNED",
                    decision.n,
                    decision.covered,
                    decision.accuracy,
                    decision.wilson_lower,
                )
        overall = self.overall.threshold(
            default, min_samples, min_covered, target_accuracy
        )
        if overall.source == "LEARNED_SELECTIVE":
            return ThresholdDecision(
                overall.threshold,
                "OVERALL_LEARNED",
                overall.n,
                overall.covered,
                overall.accuracy,
                overall.wilson_lower,
            )
        return overall

    def summary(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "overall": self.overall.summary(self.min_n),
            "per_combo": {
                key: tracker.summary(self.min_n)
                for key, tracker in self.per_combo.items()
            },
        }

    def save(self, path: str) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        try:
            with tmp.open("wb") as handle:
                pickle.dump(self, handle)
            os.replace(tmp, target)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    @staticmethod
    def load(path: str, min_n: int = 30) -> "CalibrationBook":
        try:
            with open(path, "rb") as handle:
                obj = pickle.load(handle)
            if (
                isinstance(obj, CalibrationBook)
                and getattr(obj, "schema_version", None)
                == CALIBRATION_SCHEMA_VERSION
            ):
                obj.min_n = min_n
                return obj
        except FileNotFoundError:
            pass
        except Exception:
            pass
        return CalibrationBook(min_n=min_n)
