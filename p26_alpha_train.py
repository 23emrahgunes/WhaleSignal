"""Build a frozen pre-trade alpha TTL artifact from past ex-post replays."""
from __future__ import annotations

import argparse
import json
import math
import time
from collections import defaultdict
from pathlib import Path
from typing import Iterable

import numpy as np

from p26_alpha_profile import (
    AlphaProfileBucket,
    FrozenAlphaProfile,
    save_alpha_profile,
)
from p26_config import get_p26_settings
from p26_dataset import current_code_commit
from p26_paper_v2_recorder import ensure_paper_v2_schema
from p26_schema import connect_p26


def _ttl_from_row(row) -> int | None:  # noqa: ANN001
    zero = row["time_to_zero_edge_ms"]
    if zero is not None and float(zero) > 0:
        return max(1, int(float(zero)))
    try:
        observations = json.loads(str(row["observations_json"]))
    except json.JSONDecodeError:
        return None
    positive_delays = [
        int(item["delay_ms"])
        for item in observations
        if float(item.get("net_edge", 0.0)) > 0
    ]
    return max(positive_delays) if positive_delays else None


def build_profile(
    db_path: str,
    *,
    cutoff_ts_ms: int,
    minimum_samples: int,
    quantile: float,
    artifact_id: str,
    model_version: str,
) -> FrozenAlphaProfile:
    conn = connect_p26(db_path)
    ensure_paper_v2_schema(conn)
    try:
        rows = conn.execute(
            """
            SELECT combo_key,horizon,history_max_ts_ms,observations_json,
                   time_to_zero_edge_ms
            FROM p26_alpha_replays
            WHERE history_max_ts_ms IS NOT NULL AND history_max_ts_ms < ?
            ORDER BY history_max_ts_ms,condition_id
            """,
            (int(cutoff_ts_ms),),
        ).fetchall()
    finally:
        conn.close()
    grouped: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
    for row in rows:
        ttl = _ttl_from_row(row)
        if ttl is None:
            continue
        history_ts = int(row["history_max_ts_ms"])
        combo = str(row["combo_key"])
        horizon = str(row["horizon"])
        grouped[("PER_COMBO", f"{combo}|ALL")].append((ttl, history_ts))
        grouped[("HORIZON", f"{horizon}|ALL")].append((ttl, history_ts))
        grouped[("OVERALL", "ALL|ALL")].append((ttl, history_ts))
    buckets: list[AlphaProfileBucket] = []
    for (scope, key), values in sorted(grouped.items()):
        if len(values) < minimum_samples:
            continue
        ttls = np.asarray([value[0] for value in values], dtype=float)
        ttl = max(1, int(math.floor(float(np.quantile(ttls, quantile)))))
        buckets.append(
            AlphaProfileBucket(
                scope=scope,
                key=key,
                regime="ALL",
                ttl_ms=ttl,
                sample_count=len(values),
                history_max_ts_ms=max(value[1] for value in values),
                quantile=quantile,
            )
        )
    return FrozenAlphaProfile(
        artifact_id=artifact_id,
        created_at_ms=int(time.time() * 1000),
        code_commit=current_code_commit(),
        source_model_version=model_version,
        minimum_samples=minimum_samples,
        buckets=tuple(buckets),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cutoff-ms", type=int, required=True)
    parser.add_argument("--artifact-id", default="P26_ALPHA_PROFILE_V1")
    parser.add_argument("--output")
    args = parser.parse_args()
    settings = get_p26_settings()
    profile = build_profile(
        settings.p26_db_path,
        cutoff_ts_ms=args.cutoff_ms,
        minimum_samples=settings.paper_v2_alpha_min_samples,
        quantile=settings.paper_v2_alpha_ttl_quantile,
        artifact_id=args.artifact_id,
        model_version=settings.model_artifact_version,
    )
    output = Path(args.output or settings.paper_v2_alpha_artifact)
    save_alpha_profile(profile, output)
    print(json.dumps({"artifact": str(output), "buckets": len(profile.buckets)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
