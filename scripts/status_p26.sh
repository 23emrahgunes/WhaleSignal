#!/usr/bin/env bash
set -Eeuo pipefail

MODE="human"
case "${1:-}" in
  "") ;;
  --json) MODE="json" ;;
  --help|-h)
    echo "Usage: scripts/status_p26.sh [--json]"
    exit 0
    ;;
  *) echo "ERROR: unknown option: ${1:-}" >&2; exit 2 ;;
esac

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
PY="$REPO_DIR/.venv/bin/python"
[[ -x "$PY" ]] || { echo "ERROR: missing $PY" >&2; exit 1; }

service_state() {
  if command -v systemctl >/dev/null 2>&1; then
    systemctl is-active "$1" 2>/dev/null || true
  else
    echo "SYSTEMD_UNAVAILABLE"
  fi
}

ORACLE_STATE="$(service_state direction-engine-p26-oracle.service)"
DATASET_STATE="$(service_state direction-engine-p26-dataset.service)"
BOOK_STATE="$(service_state direction-engine-p26-book.service)"
PAPER_STATE="$(service_state direction-engine-p26-paper-v2.service)"
P25_HEALTH="$(curl --connect-timeout 3 --max-time 8 -sS -o /tmp/p26-status-health.json -w '%{http_code}' http://127.0.0.1:8091/health || true)"

DB_JSON="$("$PY" - <<'PY'
import json
import time
from pathlib import Path

from p26_config import get_p26_settings
from p26_schema import connect_p26, ensure_p26_schema, integrity_check

settings = get_p26_settings()
path = Path(settings.p26_db_path)
if not path.exists():
    print(json.dumps({"db_exists": False, "db_path": str(path)}))
    raise SystemExit(0)

conn = connect_p26(settings.p26_db_path)
ensure_p26_schema(conn)
def count_if_exists(table, where="1=1"):
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]) if exists else 0
now_ms = int(time.time() * 1000)
latest = conn.execute("SELECT MAX(source_ts_ms) FROM p26_oracle_ticks").fetchone()[0]
by_asset = {
    str(row[0]): int(row[1])
    for row in conn.execute(
        "SELECT asset,COUNT(*) FROM p26_oracle_ticks GROUP BY asset ORDER BY asset"
    ).fetchall()
}
latest_by_asset = {
    str(row[0]): {
        "source_ts_ms": int(row[1]),
        "age_ms": now_ms - int(row[1]),
        "value": float(row[2]),
    }
    for row in conn.execute(
        """
        SELECT t.asset,t.source_ts_ms,t.value_real
        FROM p26_oracle_ticks t
        JOIN (
            SELECT asset,MAX(source_ts_ms) AS max_ts
            FROM p26_oracle_ticks GROUP BY asset
        ) latest ON latest.asset=t.asset AND latest.max_ts=t.source_ts_ms
        ORDER BY t.asset
        """
    ).fetchall()
}
counts = {
    "oracle_ticks": int(conn.execute("SELECT COUNT(*) FROM p26_oracle_ticks").fetchone()[0]),
    "canonical_rows": int(conn.execute("SELECT COUNT(*) FROM p26_canonical_rows").fetchone()[0]),
    "eligible_rows": int(conn.execute("SELECT COUNT(*) FROM p26_canonical_rows WHERE training_eligible=1").fetchone()[0]),
    "complete_lineage": int(conn.execute("SELECT COUNT(*) FROM p26_canonical_rows WHERE lineage_status='COMPLETE_DERIVED_AGE'").fetchone()[0]),
    "partial_lineage": int(conn.execute("SELECT COUNT(*) FROM p26_canonical_rows WHERE lineage_status='PARTIAL_LEGACY'").fetchone()[0]),
    "official_labels": int(conn.execute("SELECT COUNT(*) FROM p26_labels WHERE official_label IS NOT NULL").fetchone()[0]),
    "health_errors": int(conn.execute("SELECT COUNT(*) FROM p26_health_events WHERE severity IN ('ERROR','CRITICAL')").fetchone()[0]),
    "clob_books": count_if_exists("p26_clob_books"),
    "fee_schedules": count_if_exists("p26_fee_schedules"),
    "paper_v2_attempts": count_if_exists("p26_paper_trades"),
    "paper_v2_open": count_if_exists("p26_paper_trades", "status='OPEN'"),
    "paper_v2_settled": count_if_exists("p26_paper_trades", "status='SETTLED'"),
}
latest_health = [
    dict(row)
    for row in conn.execute(
        """
        SELECT component,event_type,severity,message,ts_ms
        FROM p26_health_events ORDER BY id DESC LIMIT 10
        """
    ).fetchall()
]
result = {
    "db_exists": True,
    "db_path": str(path.resolve()),
    "integrity": integrity_check(conn),
    **counts,
    "latest_oracle_ts_ms": int(latest) if latest else None,
    "latest_oracle_age_ms": now_ms - int(latest) if latest else None,
    "oracle_ticks_by_asset": by_asset,
    "latest_by_asset": latest_by_asset,
    "latest_health_events": latest_health,
    "safety": {
        "mode": "SHADOW_PAPER_ONLY",
        "execution_enabled": False,
        "private_key_loaded": False,
        "signing_enabled": False,
        "order_submission_enabled": False,
        "paper_v2_enabled": bool(settings.paper_v2_enabled),
    },
}
conn.close()
print(json.dumps(result, ensure_ascii=False, sort_keys=True))
PY
)"

if [[ "$MODE" == "json" ]]; then
  "$PY" - <<PY
import json
payload = json.loads('''$DB_JSON''')
payload["services"] = {
    "oracle": "$ORACLE_STATE",
    "dataset": "$DATASET_STATE",
    "book": "$BOOK_STATE",
    "paper_v2": "$PAPER_STATE",
}
payload["p25_health_http"] = "$P25_HEALTH"
print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
PY
  exit 0
fi

echo "=== P2.6 STATUS ==="
echo "oracle_service=$ORACLE_STATE"
echo "dataset_service=$DATASET_STATE"
echo "book_service=$BOOK_STATE"
echo "paper_v2_service=$PAPER_STATE"
echo "p25_health_http=$P25_HEALTH"
"$PY" - <<PY
import json
p = json.loads('''$DB_JSON''')
if not p.get("db_exists"):
    print("p26_database=MISSING", p.get("db_path"))
    raise SystemExit(0)
print("p26_database=", p["db_path"])
print("integrity=", p["integrity"])
for key in (
    "oracle_ticks",
    "canonical_rows",
    "eligible_rows",
    "complete_lineage",
    "partial_lineage",
    "official_labels",
    "health_errors",
    "latest_oracle_age_ms",
    "clob_books",
    "fee_schedules",
    "paper_v2_attempts",
    "paper_v2_open",
    "paper_v2_settled",
):
    print(f"{key}={p.get(key)}")
print("oracle_ticks_by_asset=", p.get("oracle_ticks_by_asset"))
print("latest_by_asset=", p.get("latest_by_asset"))
print("safety=", p.get("safety"))
PY

echo "=== RECENT LOGS ==="
tail -n 12 logs/p26-oracle.log 2>/dev/null || true
tail -n 12 logs/p26-dataset.log 2>/dev/null || true
tail -n 12 logs/p26-book.log 2>/dev/null || true
tail -n 12 logs/p26-paper-v2.log 2>/dev/null || true
