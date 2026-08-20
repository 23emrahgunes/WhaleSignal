"""Atomic frozen-model artifact and manifest handling for P2.6."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

import joblib


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


@dataclass(frozen=True)
class ModelManifest:
    artifact_id: str
    artifact_version: str
    created_at_utc: str
    code_commit: str
    feature_schema_version: str
    feature_schema_hash: str
    feature_names_in_exact_order: list[str]
    model_type: str
    scaler_type: str
    imputer_type: str
    regularization: dict[str, Any]
    random_seed: int
    training_cutoff_ms: int
    train_market_count: int
    train_up_count: int
    train_down_count: int
    train_condition_ids_sha256: str
    model_file: str
    model_sha256: str
    is_frozen: bool
    calibration_artifact_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LoadedArtifact:
    pipeline: Any
    manifest: ModelManifest
    model_path: Path
    manifest_path: Path


def save_artifact(
    *,
    pipeline: Any,
    manifest_without_hash: dict[str, Any],
    output_dir: Path,
    stem: str,
) -> LoadedArtifact:
    output_dir.mkdir(parents=True, exist_ok=True)
    model_path = output_dir / f"{stem}.joblib"
    manifest_path = output_dir / f"{stem}.manifest.json"
    temp_model = output_dir / f".{stem}.{os.getpid()}.joblib.tmp"
    joblib.dump(pipeline, temp_model, compress=3)
    os.replace(temp_model, model_path)
    payload = dict(manifest_without_hash)
    payload.update(
        {
            "model_file": model_path.name,
            "model_sha256": sha256_file(model_path),
            "is_frozen": True,
        }
    )
    manifest = ModelManifest(**payload)
    atomic_json(manifest_path, manifest.to_dict())
    return LoadedArtifact(pipeline, manifest, model_path, manifest_path)


def load_artifact(
    manifest_path: Path,
    *,
    expected_feature_schema_hash: Optional[str] = None,
) -> LoadedArtifact:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = ModelManifest(**payload)
    if not manifest.is_frozen:
        raise ValueError("artifact is not frozen")
    if expected_feature_schema_hash and manifest.feature_schema_hash != expected_feature_schema_hash:
        raise ValueError("MODEL_SCHEMA_MISMATCH")
    model_path = manifest_path.parent / manifest.model_file
    if not model_path.exists():
        raise FileNotFoundError(model_path)
    if sha256_file(model_path) != manifest.model_sha256:
        raise ValueError("MODEL_ARTIFACT_HASH_MISMATCH")
    pipeline = joblib.load(model_path)
    return LoadedArtifact(pipeline, manifest, model_path, manifest_path)
