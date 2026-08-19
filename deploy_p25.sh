#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")"

if [[ ! -x ./.venv/bin/python ]]; then
  echo "ERROR: .venv bulunamadi. Once python -m venv .venv calistir." >&2
  exit 1
fi

mkdir -p data models
[[ -f .env ]] && cp -f .env ".env.backup.$(date +%Y%m%d-%H%M%S)"

./.venv/bin/python - <<'PY'
from pathlib import Path

path = Path('.env')
text = path.read_text(encoding='utf-8') if path.exists() else ''
wanted = {
    'PHASE': 'P2.5',
    'MODEL_TRAINING_ENABLED': 'true',
    'CALIBRATION_ENABLED': 'true',
    'FORECAST_RECORDING_ENABLED': 'true',
    'MODEL_PATH': 'models/direction_model.pkl',
    'CALIBRATION_PATH': 'models/calibration_book.pkl',
    'FEATURE_PRICE_RING_MAX': '24000',
}
lines = text.splitlines()
seen = set()
out = []
for line in lines:
    stripped = line.strip()
    replaced = False
    for key, value in wanted.items():
        if stripped.startswith(key + '='):
            out.append(f'{key}={value}')
            seen.add(key)
            replaced = True
            break
    if not replaced:
        out.append(line)
for key, value in wanted.items():
    if key not in seen:
        out.append(f'{key}={value}')
path.write_text('\n'.join(out).rstrip() + '\n', encoding='utf-8')
PY

echo "=== DEPENDENCIES ==="
./.venv/bin/python -m pip install -q -r requirements.txt

echo "=== SYNTAX ==="
./.venv/bin/python -m py_compile *.py reference/*.py

echo "=== TESTS (deterministic P1 env; P25 tests override their phase) ==="
PHASE=P1 \
MODEL_TRAINING_ENABLED=false \
CALIBRATION_ENABLED=false \
./.venv/bin/pytest -q

echo "=== STOP OLD PROCESS ==="
pkill -f 'python.*p25_main.py' 2>/dev/null || true
pkill -f 'python.*main.py' 2>/dev/null || true
sleep 2

echo "=== START P2.5 SHADOW ==="
nohup ./.venv/bin/python p25_main.py > engine.log 2>&1 &
new_pid=$!
echo "$new_pid" > direction-engine.pid
sleep 12

if ! kill -0 "$new_pid" 2>/dev/null; then
  echo "ERROR: P2.5 process baslatilamadi" >&2
  tail -n 150 engine.log >&2 || true
  exit 1
fi

http_code="$(curl -sS -o /tmp/direction-p25-state.json -w '%{http_code}' http://127.0.0.1:8091/api/state || true)"
if [[ "$http_code" != "200" ]]; then
  echo "ERROR: /api/state HTTP=$http_code" >&2
  tail -n 150 engine.log >&2 || true
  exit 1
fi

./.venv/bin/python - <<'PY'
import json
from pathlib import Path

state = json.loads(Path('/tmp/direction-p25-state.json').read_text(encoding='utf-8'))
safety = state.get('safety', {})
print('phase=', state.get('phase'))
print('mode=', state.get('mode'))
print('markets_active=', state.get('footer', {}).get('markets_active'))
print('training=', safety.get('model_training_enabled'))
print('calibration=', safety.get('calibration_enabled'))
print('forecast_recording=', safety.get('forecast_recording_enabled'))
print('execution=', safety.get('execution_enabled'))
print('orders=', safety.get('live_orders'))
assert state.get('phase') == 'P2.5'
assert state.get('mode') == 'SHADOW'
assert safety.get('execution_enabled') is False
assert int(safety.get('live_orders') or 0) == 0
PY

echo "P2.5 SHADOW DEPLOY PASS | pid=$new_pid | http=200"
