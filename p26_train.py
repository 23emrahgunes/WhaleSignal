"""CLI for training the P2.6 external-only frozen champion model."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from p26_config import get_p26_settings
from p26_dataset import current_code_commit
from p26_fair_value import load_training_matrix, train_champion


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cutoff-ms", type=int)
    parser.add_argument("--output-dir")
    parser.add_argument("--artifact-id")
    args = parser.parse_args()
    settings = get_p26_settings()
    matrix = load_training_matrix(settings.p26_db_path, cutoff_ms=args.cutoff_ms)
    outcome = train_champion(
        matrix,
        settings,
        output_dir=Path(args.output_dir) if args.output_dir else None,
        code_commit=current_code_commit(),
        artifact_id=args.artifact_id,
    )
    payload = {
        "status": outcome.status,
        "reason": outcome.reason,
        "n_markets": outcome.n_markets,
        "up_count": outcome.up_count,
        "down_count": outcome.down_count,
        "manifest": str(outcome.artifact.manifest_path) if outcome.artifact else None,
    }
    print(json.dumps(payload, sort_keys=True))
    return 0 if outcome.status in {"TRAINED", "INSUFFICIENT_DATA", "INSUFFICIENT_CLASS_BALANCE"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
