from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from p26_artifact import load_artifact
from p26_config import P26Settings
from p26_fair_value import (
    FrozenFairValueModel,
    TrainingMatrix,
    train_champion,
)
from p26_features import (
    EXTERNAL_FEATURE_NAMES,
    assert_external_only,
    schema_hash,
    vector_from_mapping,
)


def _settings(tmp_path: Path, min_markets: int = 20, min_class: int = 5) -> P26Settings:
    return P26Settings(
        p25_db_path=str(tmp_path / "p25.sqlite"),
        p26_db_path=str(tmp_path / "p26.sqlite"),
        model_dir=str(tmp_path / "models"),
        model_min_train_markets=min_markets,
        model_min_class_markets=min_class,
        model_random_seed=7,
    )


def _matrix(n: int = 80) -> TrainingMatrix:
    rng = np.random.default_rng(123)
    X = rng.normal(size=(n, len(EXTERNAL_FEATURE_NAMES)))
    signal = 1.2 * X[:, 0] + 0.7 * X[:, 7] - 0.5 * X[:, 14]
    y = (signal > np.median(signal)).astype(int)
    return TrainingMatrix(
        X=X,
        y=y,
        condition_ids=tuple(f"c{i}" for i in range(n)),
        decision_ts_ms=tuple(1_800_000_000_000 + i * 300_000 for i in range(n)),
    )


def test_external_feature_contract_contains_no_clob_terms():
    assert_external_only(EXTERNAL_FEATURE_NAMES)
    lowered = " ".join(EXTERNAL_FEATURE_NAMES).lower()
    assert "clob" not in lowered
    assert "up_mid" not in lowered
    assert "down_mid" not in lowered
    with pytest.raises(ValueError):
        assert_external_only(["ret_fast", "clob_spread"])


def test_vector_order_is_deterministic():
    values = {name: idx + 0.5 for idx, name in enumerate(EXTERNAL_FEATURE_NAMES)}
    vector = vector_from_mapping(values)
    assert vector[0] == 0.5
    assert vector[-1] == len(EXTERNAL_FEATURE_NAMES) - 0.5
    assert schema_hash() == schema_hash()


def test_champion_training_freezes_scaler_and_round_trips(tmp_path):
    settings = _settings(tmp_path)
    matrix = _matrix()
    outcome = train_champion(
        matrix,
        settings,
        output_dir=tmp_path / "artifacts",
        code_commit="abc123",
        artifact_id="unit-model",
    )
    assert outcome.status == "TRAINED"
    assert outcome.artifact is not None
    manifest = outcome.artifact.manifest
    assert manifest.model_type == "LogisticRegression_L2"
    assert manifest.scaler_type == "RobustScaler_frozen"
    assert manifest.train_market_count == matrix.n_markets
    assert manifest.feature_names_in_exact_order == list(EXTERNAL_FEATURE_NAMES)
    assert manifest.is_frozen is True

    model = FrozenFairValueModel.load(outcome.artifact.manifest_path, settings)
    state_before = model.scaler_state()
    p1 = model.predict_p_up(matrix.X[:10])
    p2 = model.predict_p_up(matrix.X[:10])
    assert np.allclose(p1, p2)
    assert np.all((p1 > 0) & (p1 < 1))
    assert model.scaler_state() == state_before


def test_artifact_hash_and_schema_are_verified(tmp_path):
    settings = _settings(tmp_path)
    outcome = train_champion(
        _matrix(),
        settings,
        output_dir=tmp_path / "artifacts",
        code_commit="abc123",
        artifact_id="verified-model",
    )
    assert outcome.artifact is not None
    loaded = load_artifact(
        outcome.artifact.manifest_path,
        expected_feature_schema_hash=schema_hash(),
    )
    assert loaded.manifest.artifact_id == "verified-model"

    payload = json.loads(outcome.artifact.manifest_path.read_text(encoding="utf-8"))
    payload["feature_schema_hash"] = "bad"
    outcome.artifact.manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="MODEL_SCHEMA_MISMATCH"):
        FrozenFairValueModel.load(outcome.artifact.manifest_path, settings)


def test_insufficient_data_and_class_balance_fail_closed(tmp_path):
    settings = _settings(tmp_path, min_markets=100, min_class=20)
    small = _matrix(30)
    outcome = train_champion(small, settings, output_dir=tmp_path / "artifacts")
    assert outcome.status == "INSUFFICIENT_DATA"
    assert outcome.artifact is None

    settings2 = _settings(tmp_path, min_markets=20, min_class=10)
    one_sided = TrainingMatrix(
        X=np.zeros((30, len(EXTERNAL_FEATURE_NAMES))),
        y=np.ones(30, dtype=int),
        condition_ids=tuple(f"u{i}" for i in range(30)),
        decision_ts_ms=tuple(range(30)),
    )
    outcome2 = train_champion(one_sided, settings2, output_dir=tmp_path / "artifacts2")
    assert outcome2.status == "INSUFFICIENT_CLASS_BALANCE"
