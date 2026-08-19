"""P2.4 probability calibration, selective accuracy and threshold policy.

Only resolved shadow forecasts enter this book.  Calibration is hierarchical
(combo -> horizon -> overall), uses equal total weight per market, and remains
fail-closed until both outcome classes and enough unique markets exist.

The module reports Brier/log-loss/ECE, confidence buckets, coverage-vs-accuracy,
baseline comparisons and Wilson-lower-bound threshold diagnostics.  It never
places orders and never claims edge when sample counts are insufficient.
"""
from __future__ import annotations

import math
import os
import pickle
from collections import Counter, deque
from dataclasses import dataclass
from typing import Optional

from sklearn.linear_model import LogisticRegression

CALIBRATION_VERSION = "P2.4-calibration-v2"
ARTIFACT_VERSION = 2

_BUCKET_EDGES = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 1.01]
_BUCKET_LABELS = ["50-55", "55-60", "60-65", "65-70", "70-75", "75-80", "80+"]
_SELECTIVE_THRESHOLDS = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5]
_MARGIN_CANDIDATES = [0.05, 0.075, 0.10, 0.125, 0.15, 0.20, 0.25]
DEFAULT_MARGIN = 0.18
DEFAULT_TARGET_ACCURACY = 0.52


def _clip_p(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return max(1e-6, min(1.0 - 1e-6, out))


def _logit(p: float) -> float:
    p = max(1e-6, min(1.0 - 1e-6, p))
    return math.log(p / (1.0 - p))


def _log_loss(rows: list[tuple[float, int]]) -> Optional[float]:
    if not rows:
        return None
    return sum(-(y * math.log(p) + (1 - y) * math.log(1 - p)) for p, y in rows) / len(rows)


def _brier(rows: list[tuple[float, int]]) -> Optional[float]:
    if not rows:
        return None
    return sum((p - y) ** 2 for p, y in rows) / len(rows)


def _wilson_lower(wins: int, n: int, z: float = 1.96) -> float:
    if n <= 0:
        return 0.0
    phat = wins / n
    denom = 1.0 + z * z / n
    centre = phat + z * z / (2.0 * n)
    radius = z * math.sqrt((phat * (1.0 - phat) + z * z / (4.0 * n)) / n)
    return max(0.0, (centre - radius) / denom)


def _horizon_key(combo_key: str) -> str:
    return combo_key.rsplit(":", 1)[-1] if ":" in combo_key else combo_key


@dataclass
class CalSample:
    decided: bool
    outcome_up: bool
    p_up: Optional[float] = None
    decision_up: Optional[bool] = None
    confidence: float = 0.0
    market_implied_up: Optional[float] = None
    market_id: Optional[str] = None
    checkpoint_sec: Optional[int] = None
    model_version: Optional[str] = None
    p_up_no_clob: Optional[float] = None
    ptb_baseline: Optional[float] = None
    coinflip: float = 0.5


@dataclass(frozen=True)
class CalibrationOutput:
    raw_p_up: Optional[float]
    calibrated_p_up: Optional[float]
    ready: bool
    source: str
    n_markets: int
    version: str = CALIBRATION_VERSION

    def to_dict(self) -> dict:
        return {
            "raw_p_up": self.raw_p_up,
            "calibrated_p_up": self.calibrated_p_up,
            "ready": self.ready,
            "source": self.source,
            "n_markets": self.n_markets,
            "version": self.version,
        }


@dataclass(frozen=True)
class ThresholdOutput:
    margin: float
    ready: bool
    source: str
    n: int = 0
    coverage: float = 0.0
    accuracy: Optional[float] = None
    wilson_lower: Optional[float] = None
    target_accuracy: float = DEFAULT_TARGET_ACCURACY

    def to_dict(self) -> dict:
        return {
            "margin": round(self.margin, 6),
            "ready": self.ready,
            "source": self.source,
            "n": self.n,
            "coverage": round(self.coverage, 6),
            "accuracy": round(self.accuracy, 6) if self.accuracy is not None else None,
            "wilson_lower": (
                round(self.wilson_lower, 6) if self.wilson_lower is not None else None
            ),
            "target_accuracy": self.target_accuracy,
        }


class CalibrationTracker:
    """One calibration scope (overall, horizon or combo)."""

    def __init__(
        self,
        maxlen: int = 20_000,
        min_fit_markets: int = 30,
        min_class_markets: int = 5,
        min_threshold_n: int = 30,
        target_accuracy: float = DEFAULT_TARGET_ACCURACY,
    ) -> None:
        self.samples: deque[CalSample] = deque(maxlen=maxlen)
        self.min_fit_markets = max(10, int(min_fit_markets))
        self.min_class_markets = max(2, int(min_class_markets))
        self.min_threshold_n = max(10, int(min_threshold_n))
        self.target_accuracy = float(target_accuracy)
        self._generation = 0
        self._fitted_generation = -1
        self._model: Optional[LogisticRegression] = None
        self._fit_markets = 0
        self._fit_class_markets: dict[int, int] = {0: 0, 1: 0}

    def record(self, sample: CalSample) -> None:
        self.samples.append(sample)
        self._generation += 1

    def _probability_samples(self) -> list[tuple[int, CalSample]]:
        return [
            (index, sample)
            for index, sample in enumerate(self.samples)
            if _clip_p(sample.p_up) is not None
        ]

    def _market_key(self, index: int, sample: CalSample) -> str:
        return sample.market_id or f"row:{index}"

    def _fit(self) -> None:
        if self._fitted_generation == self._generation:
            return
        self._fitted_generation = self._generation
        self._model = None
        self._fit_markets = 0
        self._fit_class_markets = {0: 0, 1: 0}

        rows = self._probability_samples()
        if not rows:
            return
        market_counts = Counter(self._market_key(i, s) for i, s in rows)
        market_outcome: dict[str, int] = {}
        for i, sample in rows:
            market_outcome[self._market_key(i, sample)] = 1 if sample.outcome_up else 0
        class_counts = Counter(market_outcome.values())
        self._fit_markets = len(market_outcome)
        self._fit_class_markets = {0: class_counts.get(0, 0), 1: class_counts.get(1, 0)}
        if (
            self._fit_markets < self.min_fit_markets
            or min(self._fit_class_markets.values()) < self.min_class_markets
        ):
            return

        X: list[list[float]] = []
        y: list[int] = []
        weights: list[float] = []
        for index, sample in rows:
            p = _clip_p(sample.p_up)
            assert p is not None
            key = self._market_key(index, sample)
            X.append([_logit(p)])
            y.append(1 if sample.outcome_up else 0)
            weights.append(1.0 / market_counts[key])
        try:
            model = LogisticRegression(
                C=1.0,
                solver="lbfgs",
                max_iter=1_000,
                random_state=11,
            )
            model.fit(X, y, sample_weight=weights)
            self._model = model
        except Exception:
            self._model = None

    @property
    def fitted(self) -> bool:
        self._fit()
        return self._model is not None

    @property
    def fit_markets(self) -> int:
        self._fit()
        return self._fit_markets

    def calibrate(self, raw_p_up: Optional[float]) -> CalibrationOutput:
        raw = _clip_p(raw_p_up)
        self._fit()
        if raw is None:
            return CalibrationOutput(None, None, False, "missing", self._fit_markets)
        if self._model is None:
            return CalibrationOutput(raw, raw, False, "identity", self._fit_markets)
        try:
            calibrated = float(self._model.predict_proba([[_logit(raw)]])[0][1])
            calibrated = _clip_p(calibrated)
        except Exception:
            calibrated = raw
        return CalibrationOutput(raw, calibrated, True, "platt", self._fit_markets)

    def _decided(self) -> list[CalSample]:
        return [
            s for s in self.samples
            if s.decided and _clip_p(s.p_up) is not None and s.decision_up is not None
        ]

    def probability_rows(self, *, calibrated: bool = False) -> list[tuple[float, int]]:
        rows: list[tuple[float, int]] = []
        for sample in self.samples:
            raw = _clip_p(sample.p_up)
            if raw is None:
                continue
            p = self.calibrate(raw).calibrated_p_up if calibrated else raw
            if p is not None:
                rows.append((p, 1 if sample.outcome_up else 0))
        return rows

    def brier(self, *, calibrated: bool = False) -> Optional[float]:
        return _brier(self.probability_rows(calibrated=calibrated))

    def log_loss(self, *, calibrated: bool = False) -> Optional[float]:
        return _log_loss(self.probability_rows(calibrated=calibrated))

    def expected_calibration_error(self, bins: int = 10) -> Optional[float]:
        rows = self.probability_rows(calibrated=True)
        if not rows:
            return None
        total = len(rows)
        ece = 0.0
        for bucket in range(bins):
            lo, hi = bucket / bins, (bucket + 1) / bins
            group = [(p, y) for p, y in rows if lo <= p < hi or (bucket == bins - 1 and p == 1.0)]
            if not group:
                continue
            mean_p = sum(p for p, _ in group) / len(group)
            rate = sum(y for _, y in group) / len(group)
            ece += len(group) / total * abs(mean_p - rate)
        return ece

    def confidence_buckets(self) -> list[dict]:
        decided = self._decided()
        output: list[dict] = []
        for i, label in enumerate(_BUCKET_LABELS):
            lo, hi = _BUCKET_EDGES[i], _BUCKET_EDGES[i + 1]
            rows: list[tuple[float, CalSample]] = []
            for sample in decided:
                p = self.calibrate(sample.p_up).calibrated_p_up
                if p is None:
                    continue
                p_chosen = p if sample.decision_up else 1.0 - p
                if lo <= p_chosen < hi:
                    rows.append((p_chosen, sample))
            if not rows:
                output.append({"bucket": label, "n": 0})
                continue
            wins = sum(1 for _, s in rows if s.decision_up == s.outcome_up)
            output.append({
                "bucket": label,
                "n": len(rows),
                "actual_winrate": round(wins / len(rows), 4),
                "mean_predicted": round(sum(p for p, _ in rows) / len(rows), 4),
            })
        return output

    def selective(self) -> list[dict]:
        total = len(self.samples)
        if total == 0:
            return []
        output: list[dict] = []
        for threshold in _SELECTIVE_THRESHOLDS:
            covered = [s for s in self._decided() if s.confidence >= threshold]
            wins = sum(1 for s in covered if s.decision_up == s.outcome_up)
            output.append({
                "conf_threshold": threshold,
                "coverage": round(len(covered) / total, 4),
                "n": len(covered),
                "accuracy": round(wins / len(covered), 4) if covered else None,
            })
        return output

    def price_edge(self) -> Optional[dict]:
        rows = [s for s in self._decided() if _clip_p(s.market_implied_up) is not None]
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

    def baseline_brier(self) -> dict:
        output: dict[str, Optional[float]] = {}
        fields = {
            "coinflip": lambda s: _clip_p(s.coinflip),
            "market_implied": lambda s: _clip_p(s.market_implied_up),
            "ptb_diffusion": lambda s: _clip_p(s.ptb_baseline),
            "model_no_clob": lambda s: _clip_p(s.p_up_no_clob),
        }
        for name, getter in fields.items():
            rows = [
                (p, 1 if sample.outcome_up else 0)
                for sample in self.samples
                for p in [getter(sample)]
                if p is not None
            ]
            output[name] = round(_brier(rows), 6) if rows else None
        return output

    def threshold_policy(self) -> ThresholdOutput:
        rows = [s for s in self.samples if _clip_p(s.p_up) is not None]
        total = len(rows)
        if total == 0:
            return ThresholdOutput(DEFAULT_MARGIN, False, "insufficient")
        candidates: list[ThresholdOutput] = []
        for margin in _MARGIN_CANDIDATES:
            selected: list[tuple[float, int]] = []
            for sample in rows:
                p = self.calibrate(sample.p_up).calibrated_p_up
                if p is None or abs(p - 0.5) < margin:
                    continue
                prediction = 1 if p > 0.5 else 0
                selected.append((p, int(prediction == int(sample.outcome_up))))
            n = len(selected)
            wins = sum(correct for _, correct in selected)
            accuracy = wins / n if n else None
            lower = _wilson_lower(wins, n) if n else None
            candidates.append(ThresholdOutput(
                margin=margin,
                ready=(
                    n >= self.min_threshold_n
                    and lower is not None
                    and lower >= self.target_accuracy
                ),
                source="scope",
                n=n,
                coverage=n / total,
                accuracy=accuracy,
                wilson_lower=lower,
                target_accuracy=self.target_accuracy,
            ))
        ready = [candidate for candidate in candidates if candidate.ready]
        if ready:
            # Prefer the widest honest coverage, then the smaller margin.
            return sorted(ready, key=lambda x: (-x.coverage, x.margin))[0]
        best = max(
            candidates,
            key=lambda x: (
                x.wilson_lower if x.wilson_lower is not None else -1.0,
                x.n,
            ),
        )
        return ThresholdOutput(
            DEFAULT_MARGIN,
            False,
            "insufficient",
            n=best.n,
            coverage=best.coverage,
            accuracy=best.accuracy,
            wilson_lower=best.wilson_lower,
            target_accuracy=self.target_accuracy,
        )

    def summary(self, min_n: int) -> dict:
        decided = self._decided()
        n = len(decided)
        base = {
            "n_decided": n,
            "n_total": len(self.samples),
            "n_probability": len(self._probability_samples()),
            "unique_markets": self.fit_markets,
            "min_n": min_n,
            "calibrator_ready": self.fitted,
            "class_markets": dict(self._fit_class_markets),
            "threshold": self.threshold_policy().to_dict(),
        }
        if n < min_n:
            base["insufficient"] = True
            return base
        wins = sum(1 for s in decided if s.decision_up == s.outcome_up)
        base.update({
            "insufficient": False,
            "accuracy": round(wins / n, 4),
            "brier": round(self.brier() or 0.0, 6),
            "brier_calibrated": (
                round(self.brier(calibrated=True) or 0.0, 6)
                if self.probability_rows(calibrated=True) else None
            ),
            "log_loss": round(self.log_loss() or 0.0, 6),
            "log_loss_calibrated": (
                round(self.log_loss(calibrated=True) or 0.0, 6)
                if self.probability_rows(calibrated=True) else None
            ),
            "ece": (
                round(self.expected_calibration_error() or 0.0, 6)
                if self.expected_calibration_error() is not None else None
            ),
            "buckets": self.confidence_buckets(),
            "selective": self.selective(),
            "price_edge": self.price_edge(),
            "baseline_brier": self.baseline_brier(),
        })
        return base


class CalibrationBook:
    """Hierarchical calibration and threshold registry."""

    def __init__(
        self,
        min_n: int = 30,
        min_fit_markets: Optional[int] = None,
        min_class_markets: int = 5,
        min_threshold_n: Optional[int] = None,
        target_accuracy: float = DEFAULT_TARGET_ACCURACY,
    ) -> None:
        self.min_n = int(min_n)
        self.min_fit_markets = max(10, int(min_fit_markets or min_n))
        self.min_class_markets = int(min_class_markets)
        self.min_threshold_n = max(10, int(min_threshold_n or min_n))
        self.target_accuracy = float(target_accuracy)
        self.overall = self._tracker()
        self.per_horizon: dict[str, CalibrationTracker] = {}
        self.per_combo: dict[str, CalibrationTracker] = {}

    def _tracker(self) -> CalibrationTracker:
        return CalibrationTracker(
            min_fit_markets=self.min_fit_markets,
            min_class_markets=self.min_class_markets,
            min_threshold_n=self.min_threshold_n,
            target_accuracy=self.target_accuracy,
        )

    def record(self, combo_key: str, sample: CalSample) -> None:
        self.overall.record(sample)
        self.per_horizon.setdefault(_horizon_key(combo_key), self._tracker()).record(sample)
        self.per_combo.setdefault(combo_key, self._tracker()).record(sample)

    def calibrate(self, combo_key: str, raw_p_up: Optional[float]) -> CalibrationOutput:
        raw = _clip_p(raw_p_up)
        if raw is None:
            return CalibrationOutput(None, None, False, "missing", 0)
        combo = self.per_combo.get(combo_key)
        if combo is not None and combo.fitted:
            out = combo.calibrate(raw)
            return CalibrationOutput(raw, out.calibrated_p_up, True, "per_combo", out.n_markets)
        horizon = self.per_horizon.get(_horizon_key(combo_key))
        if horizon is not None and horizon.fitted:
            out = horizon.calibrate(raw)
            return CalibrationOutput(raw, out.calibrated_p_up, True, "per_horizon", out.n_markets)
        if self.overall.fitted:
            out = self.overall.calibrate(raw)
            return CalibrationOutput(raw, out.calibrated_p_up, True, "overall", out.n_markets)
        return CalibrationOutput(raw, raw, False, "identity", self.overall.fit_markets)

    def threshold_for(self, combo_key: str) -> ThresholdOutput:
        scopes = [
            ("per_combo", self.per_combo.get(combo_key)),
            ("per_horizon", self.per_horizon.get(_horizon_key(combo_key))),
            ("overall", self.overall),
        ]
        for name, tracker in scopes:
            if tracker is None:
                continue
            threshold = tracker.threshold_policy()
            if threshold.ready:
                return ThresholdOutput(
                    threshold.margin, True, name, threshold.n, threshold.coverage,
                    threshold.accuracy, threshold.wilson_lower, threshold.target_accuracy,
                )
        fallback = self.overall.threshold_policy()
        return ThresholdOutput(
            DEFAULT_MARGIN, False, "insufficient", fallback.n, fallback.coverage,
            fallback.accuracy, fallback.wilson_lower, fallback.target_accuracy,
        )

    def status_for(self, combo_key: str) -> dict:
        calibration = self.calibrate(combo_key, 0.5)
        threshold = self.threshold_for(combo_key)
        return {
            "calibration_ready": calibration.ready,
            "calibration_source": calibration.source,
            "calibration_markets": calibration.n_markets,
            "threshold": threshold.to_dict(),
        }

    def summary(self) -> dict:
        return {
            "version": CALIBRATION_VERSION,
            "overall": self.overall.summary(self.min_n),
            "per_horizon": {
                key: tracker.summary(self.min_n)
                for key, tracker in self.per_horizon.items()
            },
            "per_combo": {
                key: tracker.summary(self.min_n)
                for key, tracker in self.per_combo.items()
            },
        }

    def save(self, path: str) -> bool:
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            temp = f"{path}.tmp"
            with open(temp, "wb") as handle:
                pickle.dump(
                    {
                        "artifact_version": ARTIFACT_VERSION,
                        "calibration_version": CALIBRATION_VERSION,
                        "book": self,
                    },
                    handle,
                    protocol=pickle.HIGHEST_PROTOCOL,
                )
            os.replace(temp, path)
            return True
        except Exception:
            return False

    @staticmethod
    def load(path: str) -> Optional["CalibrationBook"]:
        try:
            with open(path, "rb") as handle:
                payload = pickle.load(handle)
            if not isinstance(payload, dict):
                return None
            if payload.get("artifact_version") != ARTIFACT_VERSION:
                return None
            if payload.get("calibration_version") != CALIBRATION_VERSION:
                return None
            book = payload.get("book")
            return book if isinstance(book, CalibrationBook) else None
        except FileNotFoundError:
            return None
        except Exception:
            return None
