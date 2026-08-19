"""Direction model scaffold.

P2.1 is feature-only SHADOW. This module may remain importable, but inference,
learning and persistence are hard-disabled while PHASE is P1/P2.1. Later phases
can explicitly enable the existing online logistic baseline.
"""
from __future__ import annotations

import logging
import math
import os
import pickle
from dataclasses import dataclass, field
from typing import Optional

from sklearn.linear_model import SGDClassifier

from models import AssetHorizon

log = logging.getLogger("direction_engine.model")
MIN_MARKETS_PREDICT = 20


def _feature_only_phase() -> bool:
    phase = os.getenv("PHASE", "P2.1").strip().upper()
    return phase in {"P1", "P2.1", "P2_1", "P21"}


class RunningNormalizer:
    def __init__(self) -> None:
        self.n = 0
        self.mean: list[float] = []
        self.M2: list[float] = []

    def _ensure(self, d: int) -> None:
        if not self.mean:
            self.mean = [0.0] * d
            self.M2 = [0.0] * d

    def update(self, x: list[float]) -> None:
        self._ensure(len(x))
        self.n += 1
        for i, xi in enumerate(x):
            delta = xi - self.mean[i]
            self.mean[i] += delta / self.n
            self.M2[i] += delta * (xi - self.mean[i])

    def transform(self, x: list[float]) -> list[float]:
        if self.n < 2:
            return list(x)
        out = []
        for i, xi in enumerate(x):
            var = self.M2[i] / (self.n - 1)
            std = math.sqrt(var) if var > 1e-12 else 1.0
            out.append((xi - self.mean[i]) / std)
        return out


@dataclass
class _Model:
    normalizer: RunningNormalizer = field(default_factory=RunningNormalizer)
    clf: SGDClassifier = field(
        default_factory=lambda: SGDClassifier(
            loss="log_loss", alpha=1e-4, learning_rate="optimal", random_state=7
        )
    )
    n_markets: int = 0
    n_samples: int = 0
    _initialized: bool = False

    @property
    def ready(self) -> bool:
        return self._initialized and self.n_markets >= MIN_MARKETS_PREDICT

    def learn_market(self, rows: list[list[float]], label: int) -> None:
        if not rows:
            return
        for x in rows:
            self.normalizer.update(x)
        X = [self.normalizer.transform(x) for x in rows]
        y = [label] * len(X)
        self.clf.partial_fit(X, y, classes=[0, 1])
        self._initialized = True
        self.n_markets += 1
        self.n_samples += len(X)

    def predict_p_up(self, x: list[float]) -> Optional[float]:
        if not self.ready:
            return None
        xs = self.normalizer.transform(x)
        try:
            proba = self.clf.predict_proba([xs])[0]
            classes = list(self.clf.classes_)
            idx = classes.index(1) if 1 in classes else 0
            return float(proba[idx])
        except Exception:  # noqa: BLE001
            return None


class _VariantBank:
    def __init__(self, include_clob: bool, per_combo_min: int) -> None:
        self.include_clob = include_clob
        self.per_combo_min = per_combo_min
        self.shared = _Model()
        self.per_combo: dict[str, _Model] = {}

    def _combo_model(self, combo_key: str, create: bool) -> Optional[_Model]:
        m = self.per_combo.get(combo_key)
        if m is None and create:
            m = _Model()
            self.per_combo[combo_key] = m
        return m

    def learn(self, combo_key: str, rows: list[list[float]], label: int) -> None:
        self.shared.learn_market(rows, label)
        cm = self._combo_model(combo_key, create=True)
        assert cm is not None
        cm.learn_market(rows, label)

    def predict(self, combo_key: str, x: list[float]) -> tuple[Optional[float], str]:
        cm = self.per_combo.get(combo_key)
        if cm is not None and cm.n_markets >= self.per_combo_min and cm.ready:
            return cm.predict_p_up(x), "per_combo"
        return self.shared.predict_p_up(x), "shared"

    def stats(self) -> dict:
        return {
            "shared_markets": self.shared.n_markets,
            "shared_ready": self.shared.ready,
            "per_combo": {k: m.n_markets for k, m in self.per_combo.items()},
        }


@dataclass
class ModelOutput:
    p_up: Optional[float]
    confidence: float
    ready: bool
    source: str
    p_up_no_clob: Optional[float] = None


class DirectionModel:
    def __init__(self, per_combo_min: int = 200) -> None:
        self.with_clob = _VariantBank(include_clob=True, per_combo_min=per_combo_min)
        self.no_clob = _VariantBank(include_clob=False, per_combo_min=per_combo_min)

    def learn_market(self, combo: AssetHorizon, fv_list: list) -> None:
        raise NotImplementedError("engine learn_with_label kullanir")

    def learn_with_label(self, combo_key: str, fv_list: list, label_up: int) -> None:
        if _feature_only_phase():
            log.warning("feature-only phase: model learn blocked")
            return
        rows_clob = [fv.model_features(True)[1] for fv in fv_list]
        rows_noclob = [fv.model_features(False)[1] for fv in fv_list]
        self.with_clob.learn(combo_key, rows_clob, label_up)
        self.no_clob.learn(combo_key, rows_noclob, label_up)

    def predict(self, combo_key: str, fv) -> ModelOutput:  # noqa: ANN001
        if _feature_only_phase():
            return ModelOutput(None, 0.0, False, "feature_only", None)
        _, x_clob = fv.model_features(True)
        _, x_noclob = fv.model_features(False)
        p_clob, source = self.with_clob.predict(combo_key, x_clob)
        p_noclob, _ = self.no_clob.predict(combo_key, x_noclob)
        if p_clob is None:
            return ModelOutput(None, 0.0, False, "none", p_noclob)
        confidence = 2.0 * abs(p_clob - 0.5)
        return ModelOutput(p_clob, confidence, True, source, p_noclob)

    def ready_for(self, combo_key: str) -> bool:
        if _feature_only_phase():
            return False
        cm = self.with_clob.per_combo.get(combo_key)
        if cm is not None and cm.n_markets >= self.with_clob.per_combo_min and cm.ready:
            return True
        return self.with_clob.shared.ready

    def stats(self) -> dict:
        return {
            "with_clob": self.with_clob.stats(),
            "no_clob": self.no_clob.stats(),
            "min_markets_predict": MIN_MARKETS_PREDICT,
            "feature_only_lock": _feature_only_phase(),
        }

    def save(self, path: str) -> None:
        if _feature_only_phase():
            log.warning("feature-only phase: model save blocked")
            return
        try:
            with open(path, "wb") as f:
                pickle.dump(self, f)
        except Exception as exc:  # noqa: BLE001
            log.warning("model kaydedilemedi: %s", exc)

    @staticmethod
    def load(path: str) -> Optional["DirectionModel"]:
        # Loading a stale artifact is unnecessary in feature-only P2.1 and can
        # confuse readiness displays, so fail closed.
        if _feature_only_phase():
            return None
        try:
            with open(path, "rb") as f:
                obj = pickle.load(f)
            if isinstance(obj, DirectionModel):
                return obj
        except FileNotFoundError:
            return None
        except Exception as exc:  # noqa: BLE001
            log.warning("model yuklenemedi: %s", exc)
        return None
