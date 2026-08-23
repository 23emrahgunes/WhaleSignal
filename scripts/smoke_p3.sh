#!/usr/bin/env bash
set -Eeuo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
PY="$REPO_DIR/.venv/bin/python"
[[ -x "$PY" ]] || { echo "FAIL missing venv"; exit 1; }

P25="$(curl -sS --connect-timeout 2 --max-time 5 -o /tmp/p3-smoke-p25.json -w '%{http_code}' http://127.0.0.1:8091/health || true)"
[[ "$P25" == "200" ]] || { echo "FAIL p25_health_http=$P25"; exit 1; }

# A restart may need a few seconds for Python imports/schema setup. Retry health
# rather than reporting a false deployment failure from one early connection race.
P3="000"
for _ in $(seq 1 30); do
  P3="$(curl -sS --connect-timeout 1 --max-time 2 -o /tmp/p3-smoke-health.json -w '%{http_code}' http://127.0.0.1:8093/health || true)"
  if [[ "$P3" == "200" ]]; then
    break
  fi
  sleep 1
done
if [[ "$P3" != "200" ]]; then
  echo "FAIL p3_health_http=$P3"
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
print("P3_AWS_SMOKE_PASS shadow=true execution=false signing=false private_key=false orders=false")
conn.close()
PY
