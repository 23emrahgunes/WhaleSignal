"""External-only P2.6 feature contract.

The independent fair-value model may use Binance/Chainlink/time features only.
Polymarket CLOB values are deliberately excluded and are reserved for execution,
latency and veto logic.
"""
from __future__ import annotations

import hashlib
import json
from typing import Iterable, Mapping

from features import FeatureVector


EXTERNAL_FEATURE_SCHEMA_VERSION = "P26_EXTERNAL_FEATURES_V1"
EXTERNAL_FEATURE_NAMES = tuple(FeatureVector._BASE_FIELDS)
FORBIDDEN_TOKENS = (
    "clob",
    "polymarket",
    "market_implied",
    "up_mid",
    "down_mid",
    "spread",
)


def assert_external_only(names: Iterable[str] = EXTERNAL_FEATURE_NAMES) -> None:
    for name in names:
        lowered = str(name).lower()
        if any(token in lowered for token in FORBIDDEN_TOKENS):
            raise ValueError(f"external fair-value feature contains forbidden term: {name}")


def schema_hash(
    names: Iterable[str] = EXTERNAL_FEATURE_NAMES,
    version: str = EXTERNAL_FEATURE_SCHEMA_VERSION,
) -> str:
    payload = json.dumps(
        {"version": version, "names": list(names)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def vector_from_mapping(
    values: Mapping[str, object],
    names: Iterable[str] = EXTERNAL_FEATURE_NAMES,
) -> list[float]:
    names = tuple(names)
    assert_external_only(names)
    return [float(values.get(name, 0.0) or 0.0) for name in names]


assert_external_only()
