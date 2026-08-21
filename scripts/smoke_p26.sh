#!/usr/bin/env bash
set -Eeuo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
PY="$REPO_DIR/.venv/bin/python"
[[ -x "$PY" ]] || { echo "ERROR: missing venv" >&2; exit 1; }

http="$(curl -sS --connect-timeout 3 --max-time 12 -o /tmp/p26-smoke-health.json -w '%{http_code}' http://127.0.0.1:8091/health || true)"
[[ "$http" == "200" ]] || { echo "FAIL p25_health_http=$http"; exit 1; }
for service in oracle dataset book paper-v2; do
  systemctl is-active --quiet "direction-engine-p26-${service}.service" || {
    echo "FAIL inactive service=$service"; exit 1;
  }
done

"$PY" - <<'PY'
from pathlib import Path
from p26_config import get_p26_settings
from p26_schema import connect_p26, ensure_p26_schema, integrity_check
s=get_p26_settings()
assert s.paper_v2_enabled is False, "Paper V2 must remain disabled for initial AWS smoke"
conn=connect_p26(s.p26_db_path)
ensure_p26_schema(conn)
assert integrity_check(conn)=="ok"
assert conn.execute("SELECT COUNT(*) FROM p26_oracle_ticks").fetchone()[0] > 0
print("P26_AWS_SMOKE_PASS paper_v2_enabled=false execution=false signing=false orders=false")
conn.close()
PY
