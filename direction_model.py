"""P2.3 explainable shadow direction model.

The learned model is an online logistic baseline (SGD log-loss), not a claim of
profitable edge.  It maintains two ablation variants:

- B1: external features only (PTB, Binance momentum/flow/vol/book),
- B2: B1 plus Polymarket CLOB confirmation.

Each resolved market contributes total sample weight one, regardless of how many
correlated checkpoints it contains.  Readiness requires both UP and DOWN labels.
The hierarchy is combo -> horizon -> shared, with schema fingerprints and atomic
persistence.  P1/P2.1 runtime still refuses to load trained artifacts.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import pickle
from dataclasses import dataclass, field
from typing import Optional

from sklearn.linear_model import SGDClassifier

from baselines import BaselineOutput, baseline_probabilities
from models import AssetHorizon

log = logging.getLogger("direction_engine.model")

MODEL_VERSION = "P2.3-logistic-v2"
ARTIFACT_VERSION = 2
MIN_MARKETS_PREDICT = 20
MIN_CLASS_MARKETS = 5


def _feature_only_phase() -> bool:
    phase = os.getenv("PHASE", "P2.1").strip().upper()
    return phase in {"P1", "P2.1", "P2_1", "P21"}


def _horizon_key(combo_key: str) -> str:
    return combo_key.rsplit(":", 1)[-1] if ":" in combo_key else combo_key


def _schema_hash(names: tuple[str, ...]) -> str:
    return hashlib.sha256("\x1f".join(names).encode("utf-8")).hexdigest()[:16]


def _finite_row(values: list[float]) -> Optional[list[float]]:
    out: list[float] = []
    for value in values:
        try:
            fv = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(fv):
            return None
        out.append(fv)
    return out


class RunningNormalizer:
    """Weighted Welford normalizer; one market has total weight one."""

    def __init__(self) -> None:
        self.n = 0  # physical rows observed (backward-compatible statistic)
        self.weight_sum = 0.0
        self.mean: list[float] = []
        self.M2: list[float] = []

    def _ensure(self, d: int) -> None:
        if not self.mean:
            self.mean = [0.0] * d
            self.M2 = [0.0] * d
        elif len(self.mean) != d:
            raise ValueError(f"normalizer dimension mismatch: {d}!={len(self.mean)}")

    def update(self, x: list[float], weight: float = 1.0) -> None:
        if weight <= 0:
            return
        self._ensure(len(x))
        new_total = self.weight_sum + weight
        for i, xi in enumerate(x):
            delta = xi - self.mean[i]
            new_mean = self.mean[i] + delta * weight / new_total
            self.M2[i] += weight * delta * (xi - new_mean)
            self.mean[i] = new_mean
        self.weight_sum = new_total
        self.n += 1

    def transform(self, x: list[float]) -> list[float]:
        self._ensure(len(x))
        if self.weight_sum <= 1.0:
            return list(x)
        out: list[float] = []
        for i, xi in enumerate(x):
            var = self.M2[i] / max(self.weight_sum, 1.0)
            std = math.sqrt(var) if var > 1e-12 else 1.0
            out.append((xi - self.mean[i]) / std)
        return out


@dataclass
class _Model:
    feature_names: tuple[str, ...] = field(default_factory=tuple)
    normalizer: RunningNormalizer = field(default_factory=RunningNormalizer)
    clf: SGDClassifier = field(
        default_factory=lambda: SGDClassifier(
            loss="log_loss",
            alpha=2e-4,
            learning_rate="optimal",
            average=True,
            random_state=7,
        )
    )
    n_markets: int = 0
    n_samples: int = 0
    class_markets: dict[int, int] = field(default_factory=lambda: {0: 0, 1: 0})
    _initialized: bool = False

    @property
    def ready(self) -> bool:
        return (
            self._initialized
            and self.n_markets >= MIN_MARKETS_PREDICT
            and self.class_markets.get(0, 0) >= MIN_CLASS_MARKETS
            and self.class_markets.get(1, 0) >= MIN_CLASS_MARKETS
        )

    @property
    def schema_hash(self) -> Optional[str]:
        return _schema_hash(self.feature_names) if self.feature_names else None

    def _accept_schema(self, names: tuple[str, ...]) -> bool:
        if not self.feature_names:
            self.feature_names = names
            return True
        return self.feature_names == names

    def learn_market(self, names: tuple[str, ...], rows: list[list[float]], label: int) -> bool:
        if label not in (0, 1) or not rows or not self._accept_schema(names):
            return False
        clean = [row for row in (_finite_row(r) for r in rows) if row is not None]
        clean = [row for row in clean if len(row) == len(names)]
        if not clean:
            return False

        # Correlated checkpoints from one market have aggregate weight 1.0.
        per_row_weight = 1.0 / len(clean)
        for row in clean:
            self.normalizer.update(row, per_row_weight)
        X = [self.normalizer.transform(row) for row in clean]
        y = [label] * len(X)
        weights = [per_row_weight] * len(X)
        self.clf.partial_fit(X, y, classes=[0, 1], sample_weight=weights)
        self._initialized = True
        self.n_markets += 1
        self.n_samples += len(X)
        self.class_markets[label] = self.class_markets.get(label, 0) + 1
        return True

    def predict_p_up(self, names: tuple[str, ...], x: list[float]) -> Optional[float]:
        if not self.ready or self.feature_names != names:
            return None
        row = _finite_row(x)
        if row is None or len(row) != len(names):
            return None
        try:
            proba = self.clf.predict_proba([self.normalizer.transform(row)])[0]
            classes = list(self.clf.classes_)
            idx = classes.index(1)
            return max(1e-6, min(1.0 - 1e-6, float(proba[idx])))
        except Exception as exc:  # noqa: BLE001
            log.warning("model probability failed: %s", exc)
            return None

    def stats(self) -> dict:
        return {
            "markets": self.n_markets,
            "samples": self.n_samples,
            "class_markets": dict(self.class_markets),
            "ready": self.ready,
            "schema_hash": self.schema_hash,
            "effective_weight": round(self.normalizer.weight_sum, 3),
        }


class _VariantBank:
    """One feature variant with combo -> horizon -> shared fallback."""

    def __init__(self, include_clob: bool, per_combo_min: int, horizon_min: Optional[int] = None) -> None:
        self.include_clob = include_clob
        self.per_combo_min = max(MIN_MARKETS_PREDICT, int(per_combo_min))
        self.horizon_min = max(
            MIN_MARKETS_PREDICT,
            int(horizon_min if horizon_min is not None else min(50, self.per_combo_min)),
        )
        self.shared = _Model()
        self.per_horizon: dict[str, _Model] = {}
        self.per_combo: dict[str, _Model] = {}

    def _get(self, registry: dict[str, _Model], key: str) -> _Model:
        if key not in registry:
            registry[key] = _Model()
        return registry[key]

    def learn(self, combo_key: str, names: list[str], rows: list[list[float]], label: int) -> bool:
        schema = tuple(names)
        learned = self.shared.learn_market(schema, rows, label)
        self._get(self.per_horizon, _horizon_key(combo_key)).learn_market(schema, rows, label)
        self._get(self.per_combo, combo_key).learn_market(schema, rows, label)
        return learned

    def selected_model(self, combo_key: str) -> tuple[_Model, str]:
        combo = self.per_combo.get(combo_key)
        if combo is not None and combo.n_markets >= self.per_combo_min and combo.ready:
            return combo, "per_combo"
        horizon = self.per_horizon.get(_horizon_key(combo_key))
        if horizon is not None and horizon.n_markets >= self.horizon_min and horizon.ready:
            return horizon, "per_horizon"
        return self.shared, "shared"

    def predict(self, combo_key: str, names: list[str], x: list[float]) -> tuple[Optional[float], str]:
        model, source = self.selected_model(combo_key)
        return model.predict_p_up(tuple(names), x), source

    def ready_for(self, combo_key: str) -> bool:
        model, _ = self.selected_model(combo_key)
        return model.ready

    def stats(self) -> dict:
        return {
            "include_clob": self.include_clob,
            "per_combo_min": self.per_combo_min,
            "horizon_min": self.horizon_min,
            "shared": self.shared.stats(),
            "per_horizon": {k: m.stats() for k, m in self.per_horizon.items()},
            "per_combo": {k: m.stats() for k, m in self.per_combo.items()},
        }


@dataclass
class ModelOutput:
    p_up: Optional[float]
    confidence: float
    ready: bool
    source: str
    p_up_no_clob: Optional[float] = None
    baselines: BaselineOutput = field(default_factory=BaselineOutput)
    model_version: str = MODEL_VERSION
    schema_hash: Optional[str] = None


class DirectionModel:
    """B2 (with CLOB) primary model plus B1 no-CLOB ablation."""

    def __init__(self, per_combo_min: int = 200, horizon_min: Optional[int] = None) -> None:
        self.model_version = MODEL_VERSION
        self.with_clob = _VariantBank(True, per_combo_min, horizon_min)
        self.no_clob = _VariantBank(False, per_combo_min, horizon_min)

    def learn_market(self, combo: AssetHorizon, fv_list: list) -> None:
        raise NotImplementedError("engine calls learn_with_label with the official label")

    def learn_with_label(self, combo_key: str, fv_list: list, label_up: int) -> bool:
        if label_up not in (0, 1) or not fv_list:
            return False
        names_clob, _ = fv_list[0].model_features(True)
        names_no, _ = fv_list[0].model_features(False)
        rows_clob = [fv.model_features(True)[1] for fv in fv_list]
        rows_no = [fv.model_features(False)[1] for fv in fv_list]
        learned = self.with_clob.learn(combo_key, names_clob, rows_clob, label_up)
        self.no_clob.learn(combo_key, names_no, rows_no, label_up)
        return learned

    def predict(self, combo_key: str, fv) -> ModelOutput:  # noqa: ANN001
        names_clob, x_clob = fv.model_features(True)
        names_no, x_no = fv.model_features(False)
        p_clob, source = self.with_clob.predict(combo_key, names_clob, x_clob)
        p_no, _ = self.no_clob.predict(combo_key, names_no, x_no)
        base = baseline_probabilities(fv)
        selected, _ = self.with_clob.selected_model(combo_key)
        if p_clob is None:
            return ModelOutput(
                None, 0.0, False, source, p_no, base,
                schema_hash=selected.schema_hash,
            )
        confidence = max(0.0, min(1.0, 2.0 * abs(p_clob - 0.5)))
        return ModelOutput(
            p_clob, confidence, True, source, p_no, base,
            schema_hash=selected.schema_hash,
        )

    def ready_for(self, combo_key: str) -> bool:
        return self.with_clob.ready_for(combo_key)

    def stats(self) -> dict:
        return {
            "model_version": self.model_version,
            "artifact_version": ARTIFACT_VERSION,
            "with_clob": self.with_clob.stats(),
            "no_clob": self.no_clob.stats(),
            "min_markets_predict": MIN_MARKETS_PREDICT,
            "min_class_markets": MIN_CLASS_MARKETS,
            "runtime_feature_only": _feature_only_phase(),
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
                        "model_version": MODEL_VERSION,
                        "model": self,
                    },
                    handle,
                    protocol=pickle.HIGHEST_PROTOCOL,
                )
            os.replace(temp, path)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("model save failed: %s", exc)
            return False

    @staticmethod
    def load(path: str) -> Optional["DirectionModel"]:
        if _feature_only_phase():
            return None
        try:
            with open(path, "rb") as handle:
                payload = pickle.load(handle)
            if not isinstance(payload, dict):
                log.warning("legacy model artifact ignored: %s", path)
                return None
            if payload.get("artifact_version") != ARTIFACT_VERSION:
                log.warning("model artifact version mismatch: %s", payload.get("artifact_version"))
                return None
            if payload.get("model_version") != MODEL_VERSION:
                log.warning("model version mismatch: %s", payload.get("model_version"))
                return None
            model = payload.get("model")
            return model if isinstance(model, DirectionModel) else None
        except FileNotFoundError:
            return None
        except Exception as exc:  # noqa: BLE001
            log.warning("model load failed: %s", exc)
            return None
