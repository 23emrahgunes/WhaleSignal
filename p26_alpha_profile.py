"""Frozen pre-trade alpha/TTL artifacts built only from past OOS markets.

The current market's future order books are never consulted by this module.
Post-fill books belong to ex-post analytics and may be used only to build a
future artifact whose history ends strictly before a new decision.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Optional


ALPHA_ARTIFACT_VERSION = "P26_ALPHA_PROFILE_V1"


@dataclass(frozen=True)
class AlphaProfileBucket:
    scope: str
    key: str
    regime: str
    ttl_ms: int
    sample_count: int
    history_max_ts_ms: int
    quantile: float

    def __post_init__(self) -> None:
        if self.scope not in {"PER_COMBO", "HORIZON", "OVERALL"}:
            raise ValueError(f"invalid alpha scope: {self.scope}")
        if self.ttl_ms <= 0:
            raise ValueError("alpha ttl must be positive")
        if self.sample_count < 1:
            raise ValueError("alpha sample count must be positive")
        if not 0.0 < self.quantile <= 1.0:
            raise ValueError("alpha quantile must be in (0,1]")


@dataclass(frozen=True)
class FrozenAlphaProfile:
    artifact_id: str
    created_at_ms: int
    code_commit: str
    source_model_version: str
    minimum_samples: int
    buckets: tuple[AlphaProfileBucket, ...]
    artifact_version: str = ALPHA_ARTIFACT_VERSION
    is_frozen: bool = True

    @property
    def history_max_ts_ms(self) -> Optional[int]:
        return max((item.history_max_ts_ms for item in self.buckets), default=None)


@dataclass(frozen=True)
class AlphaTTLDecision:
    ready: bool
    reason: str
    ttl_ms: Optional[int]
    scope: str
    sample_count: int
    history_max_ts_ms: Optional[int]
    artifact_id: Optional[str]
    details: tuple[str, ...] = ()


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
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


def save_alpha_profile(profile: FrozenAlphaProfile, path: Path) -> None:
    payload = asdict(profile)
    payload["buckets"] = [asdict(item) for item in profile.buckets]
    _atomic_json(path, payload)


def load_alpha_profile(path: Path) -> FrozenAlphaProfile:
    payload = json.loads(path.read_text(encoding="utf-8"))
    buckets = tuple(AlphaProfileBucket(**item) for item in payload.pop("buckets"))
    profile = FrozenAlphaProfile(buckets=buckets, **payload)
    if not profile.is_frozen:
        raise ValueError("alpha profile is not frozen")
    if profile.artifact_version != ALPHA_ARTIFACT_VERSION:
        raise ValueError("ALPHA_ARTIFACT_VERSION_MISMATCH")
    return profile


def _candidate_keys(combo_key: str, horizon: str, regime: str) -> list[tuple[str, str]]:
    normalized_regime = str(regime or "UNKNOWN").upper()
    return [
        ("PER_COMBO", f"{combo_key}|{normalized_regime}"),
        ("PER_COMBO", f"{combo_key}|ALL"),
        ("HORIZON", f"{horizon}|{normalized_regime}"),
        ("HORIZON", f"{horizon}|ALL"),
        ("OVERALL", f"ALL|{normalized_regime}"),
        ("OVERALL", "ALL|ALL"),
    ]


def resolve_pretrade_ttl(
    profile: Optional[FrozenAlphaProfile],
    *,
    combo_key: str,
    horizon: str,
    regime: str,
    decision_ts_ms: int,
    approved_scopes: Iterable[str] = ("PER_COMBO",),
) -> AlphaTTLDecision:
    if profile is None:
        return AlphaTTLDecision(
            False, "ALPHA_PROFILE_MISSING", None, "NONE", 0, None, None
        )
    approved = {str(scope).upper() for scope in approved_scopes}
    by_key = {(item.scope, item.key): item for item in profile.buckets}
    for scope, key in _candidate_keys(combo_key, horizon, regime):
        item = by_key.get((scope, key))
        if item is None or item.sample_count < profile.minimum_samples:
            continue
        if item.history_max_ts_ms >= int(decision_ts_ms):
            return AlphaTTLDecision(
                False,
                "ALPHA_PROFILE_FUTURE_DATA",
                None,
                scope,
                item.sample_count,
                item.history_max_ts_ms,
                profile.artifact_id,
                (
                    f"history_max_ts_ms={item.history_max_ts_ms}"
                    f">=decision_ts_ms={decision_ts_ms}",
                ),
            )
        if scope not in approved:
            return AlphaTTLDecision(
                False,
                "ALPHA_SCOPE_NOT_APPROVED",
                None,
                scope,
                item.sample_count,
                item.history_max_ts_ms,
                profile.artifact_id,
            )
        return AlphaTTLDecision(
            True,
            "PASS",
            item.ttl_ms,
            scope,
            item.sample_count,
            item.history_max_ts_ms,
            profile.artifact_id,
        )
    return AlphaTTLDecision(
        False,
        "ALPHA_PROFILE_MISSING",
        None,
        "NONE",
        0,
        profile.history_max_ts_ms,
        profile.artifact_id,
    )
