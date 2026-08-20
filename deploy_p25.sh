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
    'PAPER_TRADING_ENABLED': 'true',
    'PAPER_STRATEGY_VERSION': 'RESEARCH_PAPER_V1',
    'PAPER_STARTING_BANKROLL_USDC': '1000',
    'PAPER_STAKE_USDC': '2.50',
    'PAPER_ENTRY_CHECKPOINT_5M': '60',
    'PAPER_ENTRY_CHECKPOINT_15M': '240',
    'PAPER_ENTRY_CHECKPOINT_1H': '600',
    'PAPER_MIN_CONFIDENCE': '0.05',
    'PAPER_MIN_AGREEMENT': '0.50',
    'PAPER_MIN_EDGE': '0.00',
    'PAPER_MIN_PRICE': '0.05',
    'PAPER_MAX_PRICE': '0.95',
    'PAPER_SLIPPAGE': '0.005',
    'PAPER_FEE_BPS': '0',
    'PAPER_ALLOWED_STATUSES': 'PROVISIONAL,VALIDATED',
    'PAPER_ALLOWED_GRADES': 'LOW,MEDIUM,HIGH',
    'PAPER_RECENT_LIMIT': '50',
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
PAPER_TRADING_ENABLED=false \
./.venv/bin/pytest -q

echo "=== STOP OLD PROCESS ==="
pkill -f 'python.*p25_main.py' 2>/dev/null || true
pkill -f 'python.*main.py' 2>/dev/null || true
sleep 2

echo "=== START P2.5 SHADOW + PAPER ==="
nohup ./.venv/bin/python p25_main.py > engine.log 2>&1 &
new_pid=$!
echo "$new_pid" > direction-engine.pid
sleep 12

if ! kill -0 "$new_pid" 2>/dev/null; then
  echo "ERROR: P2.5 process baslatilamadi" >&2
  tail -n 150 engine.log >&2 || true
  exit 1
fi

base_url="http://127.0.0.1:8091"
state_code="$(curl -sS -o /tmp/direction-p25-state.json -w '%{http_code}' "$base_url/api/state" || true)"
health_code="$(curl -sS -o /tmp/direction-p25-health.json -w '%{http_code}' "$base_url/health" || true)"
paper_page_code="$(curl -sS -o /tmp/direction-p25-paper.html -w '%{http_code}' "$base_url/paper-trades" || true)"
paper_api_code="$(curl -sS -o /tmp/direction-p25-paper-api.json -w '%{http_code}' "$base_url/api/paper-trades?limit=1" || true)"
paper_summary_code="$(curl -sS -o /tmp/direction-p25-paper-summary.json -w '%{http_code}' "$base_url/api/paper-summary" || true)"

for check in \
  "api/state:$state_code" \
  "health:$health_code" \
  "paper-trades:$paper_page_code" \
  "api/paper-trades:$paper_api_code" \
  "api/paper-summary:$paper_summary_code"
do
  endpoint="${check%%:*}"
  code="${check##*:}"
  if [[ "$code" != "200" ]]; then
    echo "ERROR: /$endpoint HTTP=$code" >&2
    tail -n 150 engine.log >&2 || true
    exit 1
  fi
done

if ! grep -q 'Paper Kayıtları' /tmp/direction-p25-paper.html; then
  echo "ERROR: /paper-trades eski router veya yanlis HTML dondu" >&2
  head -n 30 /tmp/direction-p25-paper.html >&2 || true
  exit 1
fi

./.venv/bin/python - <<'PY'
import json
from pathlib import Path

state = json.loads(Path('/tmp/direction-p25-state.json').read_text(encoding='utf-8'))
health = json.loads(Path('/tmp/direction-p25-health.json').read_text(encoding='utf-8'))
paper_api = json.loads(Path('/tmp/direction-p25-paper-api.json').read_text(encoding='utf-8'))
paper_summary = json.loads(Path('/tmp/direction-p25-paper-summary.json').read_text(encoding='utf-8'))
safety = state.get('safety', {})
paper = state.get('paper_trading', {})
policy = paper.get('policy', {})
print('phase=', state.get('phase'))
print('mode=', state.get('mode'))
print('markets_active=', state.get('footer', {}).get('markets_active'))
print('training=', safety.get('model_training_enabled'))
print('calibration=', safety.get('calibration_enabled'))
print('forecast_recording=', safety.get('forecast_recording_enabled'))
print('paper_trading=', safety.get('paper_trading_enabled'))
print('paper_strategy=', policy.get('strategy_version'))
print('paper_stake=', policy.get('stake_usdc'))
print('paper_records_page=', health.get('paper_records_page'))
print('paper_records_api=', health.get('paper_records_api'))
print('paper_records_total=', (paper_api.get('pagination') or {}).get('total'))
print('paper_summary_source=', paper_summary.get('source'))
print('execution=', safety.get('execution_enabled'))
print('orders=', safety.get('live_orders'))
print('paper_order_submissions=', safety.get('paper_order_submissions'))
assert state.get('phase') == 'P2.5'
assert state.get('mode') == 'SHADOW'
assert safety.get('paper_trading_enabled') is True
assert safety.get('paper_only') is True
assert int(safety.get('paper_order_submissions') or 0) == 0
assert safety.get('execution_enabled') is False
assert int(safety.get('live_orders') or 0) == 0
assert paper.get('enabled') is True
assert health.get('paper_records_page') == '/paper-trades'
assert health.get('paper_records_api') == '/api/paper-trades'
assert paper_api.get('paperOnly') is True
assert paper_api.get('source') == 'sqlite'
assert paper_summary.get('paperOnly') is True
assert paper_summary.get('source') == 'sqlite'
PY

echo "P2.5 SHADOW + PAPER DEPLOY PASS | pid=$new_pid | http=200 | paper-routes=200"