#!/usr/bin/env bash
set -Eeuo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
PY="$REPO_DIR/.venv/bin/python"
[[ -x "$PY" ]] || { echo "FAIL missing venv"; exit 1; }

wait_http_200() {
  local name="$1"
  local url="$2"
  local output="$3"
  local attempts="${4:-30}"
  local code="000"
  for _ in $(seq 1 "$attempts"); do
    code="$(curl -sS --connect-timeout 1 --max-time 2 -o "$output" -w '%{http_code}' "$url" || true)"
    if [[ "$code" == "200" ]]; then
      printf '%s' "$code"
      return 0
    fi
    sleep 1
  done
  echo "FAIL ${name}_health_http=$code" >&2
  return 1
}

# P2.5 /health calls engine.snapshot(), so a busy research loop can make a single
# 5s curl a false negative. Require eventual HTTP 200 instead of one-shot timing.
P25="$(wait_http_200 p25 http://127.0.0.1:8091/health /tmp/p3-smoke-p25.json 30)" || exit 1

# P3 should stay responsive even while replay backlog drains. Give startup a bounded
# retry window so imports/schema setup do not cause a false deployment failure.
if ! P3="$(wait_http_200 p3 http://127.0.0.1:8093/health /tmp/p3-smoke-health.json 30)"; then
  systemctl --no-pager --full status direction-engine-p3-arbitrage.service || true
  tail -n 120 logs/p3-arbitrage.log || true
  exit 1
fi
systemctl is-active --quiet direction-engine-p3-arbitrage.service || { echo "FAIL p3 service inactive"; exit 1; }

"$PY" - <<'PY'
from p3_config import get_p3_settings
from p3_schema import connect_p3, ensure_p3_schema, integrity_check
s=get_p3_settings(); s.validate_research_safety()
conn=connect_p3(s.p3_db_path); ensure_p3_schema(conn)
assert integrity_check(conn)=="ok"
assert s.p26_db_path != s.p3_db_path
print("P3_AWS_SMOKE_PASS p25=200 p3=200 shadow=true execution=false signing=false private_key=false orders=false")
conn.close()
PY
