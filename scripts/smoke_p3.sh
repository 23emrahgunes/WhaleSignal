#!/usr/bin/env bash
set -Eeuo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
PY="$REPO_DIR/.venv/bin/python"
[[ -x "$PY" ]] || { echo "FAIL missing venv"; exit 1; }

wait_http_200() {
  local name="$1" url="$2" output="$3" attempts="${4:-30}" code="000"
  for _ in $(seq 1 "$attempts"); do
    code="$(curl -sS --connect-timeout 1 --max-time 2 -o "$output" -w '%{http_code}' "$url" || true)"
    if [[ "$code" == "200" ]]; then printf '%s' "$code"; return 0; fi
    sleep 1
  done
  echo "FAIL ${name}_health_http=$code" >&2
  return 1
}

pgrep -f 'p25_main\.py' >/dev/null 2>&1 || { echo "FAIL p25_process_missing"; exit 1; }
if ! P3="$(wait_http_200 p3 http://127.0.0.1:8093/health /tmp/p3-smoke-health.json 30)"; then
  systemctl --no-pager --full status direction-engine-p3-arbitrage.service || true
  tail -n 120 logs/p3-arbitrage.log || true
  exit 1
fi
systemctl is-active --quiet direction-engine-p3-arbitrage.service || { echo "FAIL p3 service inactive"; exit 1; }
for service in direction-engine-p26-oracle.service direction-engine-p26-dataset.service direction-engine-p26-book.service; do
  systemctl is-active --quiet "$service" || { echo "FAIL required service inactive: $service"; exit 1; }
done

"$PY" - <<'PY'
import json
from pathlib import Path
from p3_config import get_p3_settings
from p3_schema import connect_p3, ensure_p3_schema, integrity_check

s=get_p3_settings(); s.validate_research_safety()
health=json.loads(Path('/tmp/p3-smoke-health.json').read_text(encoding='utf-8'))
assert health['ok'] is True
assert health['mode'] == 'DRY', health
assert health['execution_enabled'] is False, health
assert health['order_submission_enabled'] is False, health
conn=connect_p3(s.p3_db_path); ensure_p3_schema(conn)
assert integrity_check(conn)=='ok'
assert s.p26_db_path != s.p3_db_path
print(f"P3_AWS_SMOKE_PASS starts=DRY p3=200 live_feature={s.live_feature_enabled} live_auto={s.live_auto_execute_enabled}")
conn.close()
PY

CONTROL_ENABLED="$("$PY" - <<'PY'
from p3_config import get_p3_settings
s=get_p3_settings(); print('1' if s.live_control_enabled else '0')
PY
)"
if [[ "$CONTROL_ENABLED" == "1" ]]; then
  CONTROL_PORT="$("$PY" - <<'PY'
from p3_config import get_p3_settings
print(get_p3_settings().live_control_port)
PY
)"
  wait_http_200 p3-live-control "http://127.0.0.1:${CONTROL_PORT}/api/status" /tmp/p3-live-control-status.json 30 >/dev/null
  "$PY" - <<'PY'
import json
from pathlib import Path
j=json.loads(Path('/tmp/p3-live-control-status.json').read_text(encoding='utf-8'))
assert j['ok'] is True
assert j['state']['mode'] == 'DRY'
print('P3_LIVE_CONTROL_SMOKE_PASS mode=DRY loopback=true')
PY
fi
