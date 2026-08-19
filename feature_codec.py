"""Lossless-enough codec for persisted P2 feature JSON.

Snapshots store ``FeatureVector.to_dict()``.  This module reconstructs vectors after
a process restart so an officially resolved market can train exactly once without
requiring the in-memory accumulator to survive until settlement.
"""
from __future__ import annotations

from dataclasses import fields
from typing import Optional

from features import FeatureVector
from models import Asset, AssetHorizon, Horizon


def combo_from_key(combo_key: str) -> Optional[AssetHorizon]:
    try:
        asset_s, horizon_s = combo_key.split(":", 1)
        return AssetHorizon(Asset(asset_s.upper()), Horizon(horizon_s.lower()))
    except (ValueError, KeyError):
        return None


def feature_vector_from_payload(
    combo: AssetHorizon,
    ts: float,
    seconds_remaining: float,
    payload: dict,
) -> Optional[FeatureVector]:
    if not isinstance(payload, dict):
        return None
    allowed = {field.name for field in fields(FeatureVector)}
    kwargs = {
        key: value
        for key, value in payload.items()
        if key in allowed and key not in {"combo", "ts", "seconds_remaining"}
    }
    try:
        return FeatureVector(
            combo=combo,
            ts=float(ts),
            seconds_remaining=float(seconds_remaining),
            **kwargs,
        )
    except (TypeError, ValueError):
        return None


def feature_vector_from_row(row: dict) -> Optional[FeatureVector]:
    combo = combo_from_key(str(row.get("combo_key", "")))
    if combo is None:
        return None
    payload = row.get("features") or row.get("extra") or {}
    return feature_vector_from_payload(
        combo,
        float(row.get("ts") or 0.0),
        float(row.get("tte_sec") or row.get("seconds_remaining") or 0.0),
        payload,
    )
