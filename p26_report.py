"""Comprehensive P2.6 JSON/Markdown status and promotion report."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from p26_config import get_p26_settings
from p26_promotion import evaluate_promotion
from p26_schema import connect_p26, ensure_p26_schema


def _counts(db_path: str) -> dict:
    conn = connect_p26(db_path)
    ensure_p26_schema(conn)
    try:
        tables = [
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'p26_%'")
        ]
        return {
            table: int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in sorted(tables)
        }
    finally:
        conn.close()


def build_report() -> dict:
    settings = get_p26_settings()
    promotion = evaluate_promotion(settings)
    return {
        "phase": "P2.6",
        "mode": "SHADOW_PAPER_ONLY",
        "database": settings.p26_db_path,
        "table_counts": _counts(settings.p26_db_path),
        "promotion": promotion.to_dict(),
        "modules": {
            "P2.6.0": "IMPLEMENTED_LOCAL",
            "P2.6.1": "IMPLEMENTED_LOCAL",
            "P2.6.2": "IMPLEMENTED_LOCAL",
            "P2.6.3": "IMPLEMENTED_LOCAL",
            "P2.6.4": "IMPLEMENTED_LOCAL",
            "P2.6.5": "IMPLEMENTED_LOCAL",
            "P2.6.6": "IMPLEMENTED_LOCAL",
        },
        "runtime_acceptance": {
            "aws_security_group": "NOT_TESTED",
            "oracle_sidecar_live": "NOT_TESTED",
            "canonical_live_rows": "NOT_TESTED",
            "paper_v2_live_replay": "NOT_TESTED",
        },
        "safety": {
            "execution_enabled": False,
            "private_key_loaded": False,
            "signing_enabled": False,
            "order_submission_enabled": False,
        },
        "edge_claim": "NO_PROVEN_EDGE_UNLESS_PROMOTION_STATE_VALIDATED_PAPER_MODEL",
    }


def markdown(report: dict) -> str:
    promotion = report["promotion"]
    lines = [
        "# Direction Engine P2.6 Research Report",
        "",
        f"- Mode: `{report['mode']}`",
        f"- Promotion state: `{promotion['state']}`",
        f"- Promoted: `{promotion['promoted']}`",
        f"- Reasons: `{', '.join(promotion['reasons']) or 'NONE'}`",
        "- Live execution: `FALSE`",
        "",
        "## Table counts",
        "",
    ]
    lines.extend(f"- `{key}`: {value}" for key, value in report["table_counts"].items())
    lines.extend(["", "## Runtime acceptance", ""])
    lines.extend(f"- `{key}`: {value}" for key, value in report["runtime_acceptance"].items())
    lines.extend(["", "## Promotion evidence", "", "```json", json.dumps(promotion, indent=2, sort_keys=True), "```", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", default="reports/p26/p26_report.json")
    parser.add_argument("--markdown", default="reports/p26/p26_report.md")
    args = parser.parse_args()
    report = build_report()
    json_path = Path(args.json)
    md_path = Path(args.markdown)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(markdown(report), encoding="utf-8")
    print(json.dumps({"state": report["promotion"]["state"], "json": str(json_path), "markdown": str(md_path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
