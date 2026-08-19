"""P2.3 shadow direction models and honest baselines.

Three online logistic variants are maintained in parallel:

- PTB_ONLY: official PTB state + TTE/volatility
- MODEL_B1_EXTERNAL: Binance momentum/flow/book + PTB, no Polymarket CLOB
- MODEL_B2_FULL: B1 plus Polymarket CLOB confirmation

A 50/50 baseline and Polymarket implied probability are recorded outside this
module. Training is chronological and market-weighted; one market cannot dominate
simply because it has more checkpoints. No execution code exists here.
"""
from __future__ import annotations

import logging
import math
import os
import pickle
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from sklearn.linear_model import SGDClassifier

from models import AssetHorizon

log = logging.getLogger("direction_engine.model")

MIN_MARKETS_PREDICT = 20
MODEL_SCHEMA_VERSION = 3
MODEL_VERSION = "MODEL_B2_LOGISTIC_V1"


def _phase_rank() -> int:
    raw = os.getenv("PHASE", "P2.5").strip().upper()
    return {
        "P1": 10,
        "P2.1": 21,
        "P2_1": 21,
        "P21": 21,
        "P2.2": 22,
        "P2_2": 22,
        "P22": 22,
        "P2.3": 23,
        "P2_3": 23,
        "P23": 23,
        "P2.4": 24,
        "P2_4": 24,
        "P24": 24,
        "P2.5": 25,
        "P2_5": 25,
        "P25": 25,
        "P3": 30,
    }.get(raw, -1)


def _runtime_model_active() -> bool:
    return _phase_rank() >= 23


def _clip_probability(p: float) -> float:
    return max(1e-6, min(1.0 - 1e-6, float(p)))


def _sigmoid(x: float) -> float:
    x = max(-30.0, min(30.0, x))
    return 1.0 / (1.0 + math.exp(-x))


def ptb_heuristic_probability(fv) -> float:  # noqa: ANN001
    """Untrained, explicitly-labelled PTB baseline."""
    z = float(getattr(fv, "ptb_z", 0.0) or 0.0)
    elapsed = max(0.0, min(1.0, float(getattr(fv, "elapsed_fraction", 0.0) or 0.0)))
    slope = float(getattr(fv, "distance_slope", 0.0) or 0.0)
    persistence = float(getattr(fv, "sign_persistence", 0.0) or 0.0)
    maturity = 0.30 + 0.70 * math.sqrt(elapsed)
    score = maturity * (0.85 * z + 0.08 * math.tanh(slope / 2.0))
    score *= 0.65 + 0.35 * max(0.0, min(1.0, persistence))
    return _clip_probability(_sigmoid(score))


class RunningNormalizer:
    """Welford online standardizer with schema-safe dimension checks."""

    def __init__(self) -> None:
        self.n = 0
        self.mean: list[float] = []
        self.M2: list[float] = []

    def _ensure(self, d: int) -> None:
        if not self.mean:
            self.mean = [0.0] * d
            self.M2 = [0.0] * d
        elif len(self.mean) != d:
            raise ValueError(
                f"feature dimension changed: trained={len(self.mean)} current={d}"
            )

    def update(self, x: list[float]) -> None:
        self._ensure(len(x))
        self.n += 1
        for i, xi in enumerate(x):
            delta = xi - self.mean[i]
            self.mean[i] += delta / self.n
            self.M2[i] += delta * (xi - self.mean[i])

    def transform(self, x: list[float]) -> list[float]:
        self._ensure(len(x))
        if self.n < 2:
            return list(x)
        out: list[float] = []
        for i, xi in enumerate(x):
            var = self.M2[i] / (self.n - 1)
            std = math.sqrt(var) if var > 1e-12 else 1.0
            out.append((xi - self.mean[i]) / std)
        return out


@dataclass
class _Model:
    min_markets_predict: int = MIN_MARKETS_PREDICT
    normalizer: RunningNormalizer = field(default_factory=RunningNormalizer)
    clf: SGDClassifier = field(
        default_factory=lambda: SGDClassifier(
            loss="log_loss",
            alpha=2e-4,
            penalty="l2",
            learning_rate="optimal",
            average=True,
            random_state=7,
        )
    )
    n_markets: int = 0
    n_samples: int = 0
    _initialized: bool = False

    @property
    def ready(self) -> bool:
        return self._initialized and self.n_markets >= self.min_markets_predict

    def learn_market(self, rows: list[list[float]], label: int) -> None:
        if not rows:
            return
        for row in rows:
            self.normalizer.update(row)
        X = [self.normalizer.transform(row) for row in rows]
        y = [int(label)] * len(X)
        weights = [1.0 / len(X)] * len(X)
        self.clf.partial_fit(X, y, classes=[0, 1], sample_weight=weights)
        self._initialized = True
        self.n_markets += 1
        self.n_samples += len(X)

    def predict_p_up(self, x: list[float]) -> Optional[float]:
        if not self.ready:
            return None
        try:
            xs = self.normalizer.transform(x)
            proba = self.clf.predict_proba([xs])[0]
            classes = list(self.clf.classes_)
            idx = classes.index(1) if 1 in classes else 0
            return _clip_probability(float(proba[idx]))
        except Exception as exc:  # noqa: BLE001
            log.warning("model prediction failed: %s", exc)
            return None


class _VariantBank:
    def __init__(
        self,
        name: str,
        per_combo_min: int,
        min_markets_predict: int,
    ) -> None:
        self.name = name
        self.per_combo_min = per_combo_min
        self.min_markets_predict = min_markets_predict
        self.shared = _Model(min_markets_predict=min_markets_predict)
        self.per_combo: dict[str, _Model] = {}

    def _combo_model(self, combo_key: str, create: bool) -> Optional[_Model]:
        model = self.per_combo.get(combo_key)
        if model is None and create:
            model = _Model(min_markets_predict=self.min_markets_predict)
            self.per_combo[combo_key] = model
        return model

    def learn(self, combo_key: str, rows: list[list[float]], label: int) -> None:
        self.shared.learn_market(rows, label)
        model = self._combo_model(combo_key, create=True)
        assert model is not None
        model.learn_market(rows, label)

    def predict(self, combo_key: str, x: list[float]) -> tuple[Optional[float], str]:
        combo_model = self.per_combo.get(combo_key)
        if (
            combo_model is not None
            and combo_model.n_markets >= self.per_combo_min
            and combo_model.ready
        ):
            return combo_model.predict_p_up(x), f"{self.name}:per_combo"
        return self.shared.predict_p_up(x), f"{self.name}:shared"

    def ready_for(self, combo_key: str) -> bool:
        combo_model = self.per_combo.get(combo_key)
        if (
            combo_model is not None
            and combo_model.n_markets >= self.per_combo_min
            and combo_model.ready
        ):
            return True
        return self.shared.ready

    def stats(self) -> dict:
        return {
            "name": self.name,
            "shared_markets": self.shared.n_markets,
            "shared_samples": self.shared.n_samples,
            "shared_ready": self.shared.ready,
            "per_combo": {
                key: {
                    "markets": model.n_markets,
                    "samples": model.n_samples,
                    "ready": model.ready,
                }
                for key, model in self.per_combo.items()
            },
        }


@dataclass
class ModelOutput:
    p_up: Optional[float]
    confidence: float
    ready: bool
    source: str
    p_up_no_clob: Optional[float] = None
    p_up_ptb: Optional[float] = None
    p_up_ptb_heuristic: Optional[float] = None
    model_version: str = MODEL_VERSION

    @property
    def p_up_external(self) -> Optional[float]:
        return self.p_up_no_clob


class DirectionModel:
    """Shared-first model registry with PTB/B1/B2 ablation variants."""

    schema_version = MODEL_SCHEMA_VERSION
    model_version = MODEL_VERSION

    def __init__(
        self,
        per_combo_min: int = 200,
        min_markets_predict: int = MIN_MARKETS_PREDICT,
    ) -> None:
        self.per_combo_min = per_combo_min
        self.min_markets_predict = min_markets_predict
        self.with_clob = _VariantBank(
            "MODEL_B2_FULL", per_combo_min, min_markets_predict
        )
        self.no_clob = _VariantBank(
            "MODEL_B1_EXTERNAL", per_combo_min, min_markets_predict
        )
        self.ptb_only = _VariantBank(
            "PTB_ONLY", per_combo_min, min_markets_predict
        )

    @staticmethod
    def _combo_one_hot(combo_key: str) -> list[float]:
        asset, _, horizon = combo_key.partition(":")
        assets = ("BTC", "ETH", "SOL", "XRP")
        horizons = ("5m", "15m", "1h")
        return [1.0 if asset == x else 0.0 for x in assets] + [
            1.0 if horizon == x else 0.0 for x in horizons
        ]

    def _full_features(self, combo_key: str, fv, include_clob: bool) -> list[float]:  # noqa: ANN001
        _, values = fv.model_features(include_clob)
        return values + self._combo_one_hot(combo_key)

    def _ptb_features(self, combo_key: str, fv) -> list[float]:  # noqa: ANN001
        values = [
            float(getattr(fv, "distance_bps", 0.0) or 0.0),
            float(getattr(fv, "distance_slope", 0.0) or 0.0),
            float(getattr(fv, "ptb_z", 0.0) or 0.0),
            float(getattr(fv, "tte_fraction", 0.0) or 0.0),
            float(getattr(fv, "elapsed_fraction", 0.0) or 0.0),
            float(getattr(fv, "rv_slow", 0.0) or 0.0),
            float(getattr(fv, "vol_percentile", 0.5) or 0.5),
        ]
        return values + self._combo_one_hot(combo_key)

    def learn_market(self, combo: AssetHorizon, fv_list: list) -> None:
        raise NotImplementedError("use learn_with_label")

    def learn_with_label(self, combo_key: str, fv_list: list, label_up: int) -> None:
        rows = list(fv_list)
        if not rows:
            return
        rows_b2 = [self._full_features(combo_key, fv, True) for fv in rows]
        rows_b1 = [self._full_features(combo_key, fv, False) for fv in rows]
        rows_ptb = [self._ptb_features(combo_key, fv) for fv in rows]
        self.with_clob.learn(combo_key, rows_b2, label_up)
        self.no_clob.learn(combo_key, rows_b1, label_up)
        self.ptb_only.learn(combo_key, rows_ptb, label_up)

    def predict(self, combo_key: str, fv) -> ModelOutput:  # noqa: ANN001
        heuristic = ptb_heuristic_probability(fv)
        p_b2, source = self.with_clob.predict(
            combo_key, self._full_features(combo_key, fv, True)
        )
        p_b1, _ = self.no_clob.predict(
            combo_key, self._full_features(combo_key, fv, False)
        )
        p_ptb, _ = self.ptb_only.predict(
            combo_key, self._ptb_features(combo_key, fv)
        )
        if p_b2 is None:
            return ModelOutput(
                None,
                0.0,
                False,
                "none",
                p_up_no_clob=p_b1,
                p_up_ptb=p_ptb,
                p_up_ptb_heuristic=heuristic,
            )
        return ModelOutput(
            p_b2,
            2.0 * abs(p_b2 - 0.5),
            True,
            source,
            p_up_no_clob=p_b1,
            p_up_ptb=p_ptb,
            p_up_ptb_heuristic=heuristic,
        )

    def ready_for(self, combo_key: str) -> bool:
        return self.with_clob.ready_for(combo_key)

    def stats(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "model_version": self.model_version,
            "min_markets_predict": self.min_markets_predict,
            "per_combo_min": self.per_combo_min,
            "runtime_model_active": _runtime_model_active(),
            "b2_full": self.with_clob.stats(),
            "b1_external": self.no_clob.stats(),
            "ptb_only": self.ptb_only.stats(),
        }

    def save(self, path: str) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        try:
            with tmp.open("wb") as handle:
                pickle.dump(self, handle)
            os.replace(tmp, target)
        except Exception as exc:  # noqa: BLE001
            log.warning("model save failed: %s", exc)
            try:
                tmp.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def load(path: str) -> Optional["DirectionModel"]:
        # Runtime phase gating is enforced by main.Settings. Keeping load pure
        # also makes artifact compatibility independently testable.
        try:
            with open(path, "rb") as handle:
                obj = pickle.load(handle)
            if not isinstance(obj, DirectionModel):
                return None
            if getattr(obj, "schema_version", None) != MODEL_SCHEMA_VERSION:
                log.warning("model schema mismatch; starting fresh")
                return None
            return obj
        except FileNotFoundError:
            return None
        except Exception as exc:  # noqa: BLE001
            log.warning("model load failed: %s", exc)
            return None
