#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")"

if [[ ! -x ./.venv/bin/python ]]; then
  echo "ERROR: .venv bulunamadi. Once python -m venv .venv calistir." >&2
  exit 1
fi

mkdir -p data models
backup_dir="$HOME/.direction-engine-env-backups"
mkdir -p "$backup_dir"
chmod 700 "$backup_dir"
if [[ -f .env ]]; then
  backup_path="$backup_dir/.env.p25.$(date +%Y%m%d-%H%M%S)"
  cp -f .env "$backup_path"
  chmod 600 "$backup_path"
  echo "P2.5 env backup=$backup_path"
fi

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
    'RESOLUTION_POLL_SEC': '10',
    'PAPER_TRADING_ENABLED': 'true',
    'PAPER_ENTRY_MODE': 'DEEP_VALUE_WATCH',
    'PAPER_STRATEGY_VERSION': 'DEEP_VALUE_25C_5M_DUAL_V1',
    'PAPER_STARTING_BANKROLL_USDC': '1000',
    'PAPER_STAKE_USDC': '1.00',
    'PAPER_ENTRY_CHECKPOINT_5M': '60',
    'PAPER_ENTRY_CHECKPOINT_15M': '240',
    'PAPER_ENTRY_CHECKPOINT_1H': '600',
    'PAPER_MIN_CONFIDENCE': '0.05',
    'PAPER_MIN_AGREEMENT': '0.50',
    'PAPER_MIN_EDGE': '0.00',
    'PAPER_MIN_PRICE': '0.01',
    'PAPER_MAX_PRICE': '0.95',
    'PAPER_SLIPPAGE': '0.005',
    'PAPER_FEE_BPS': '0',
    'PAPER_ALLOWED_STATUSES': 'PROVISIONAL,VALIDATED',
    'PAPER_ALLOWED_GRADES': 'LOW,MEDIUM,HIGH',
    'PAPER_RECENT_LIMIT': '50',
    'PAPER_DEEP_VALUE_MIN_ASK': '0.01',
    'PAPER_DEEP_VALUE_MAX_ASK': '0.25',
    'PAPER_DEEP_VALUE_PREFILTER_BUFFER': '0.03',
    'PAPER_DEEP_VALUE_MIN_TTE_SEC': '5',
    'PAPER_DEEP_VALUE_P26_DB_PATH': 'data/p26_research.sqlite',
    'PAPER_DEEP_VALUE_MAX_BOOK_AGE_MS': '1500',
    'PAPER_DEEP_VALUE_REQUIRE_DEPTH': 'true',
    'PAPER_DEEP_VALUE_REQUIRE_FEE_SCHEDULE': 'true',
    'PAPER_DEEP_VALUE_MIN_VALUE_MULTIPLE': '1.50',
    'PAPER_DEEP_VALUE_HORIZONS': '5m',
    # LIVE pilot is attached but starts fail-closed/unarmed after every deploy.
    'P25_LIVE_FEATURE_ENABLED': 'false',
    'P25_LIVE_ARMED': 'false',
    'P25_LIVE_ARM_NONCE': '',
    'P25_LIVE_ASSET': 'XRP',
    'P25_LIVE_HORIZON': '5m',
    'P25_LIVE_STRATEGY_VERSION': 'DEEP_VALUE_25C_5M_DUAL_V1',
    'P25_LIVE_MAX_STAKE_USDC': '1.10',
    'P25_LIVE_MAX_PRICE_DRIFT_PCT': '0.10',
    'P25_LIVE_MAX_LIMIT_PRICE': '0.255',
    'P25_LIVE_LEDGER_PATH': 'data/p25_live_direction.sqlite',
    'P25_LIVE_CLOB_HOST': 'https://clob.polymarket.com',
    'P25_LIVE_CHAIN_ID': '137',
    'P25_LIVE_GEOBLOCK_URL': 'https://polymarket.com/api/geoblock',
    'P25_LIVE_REQUIRE_GEOBLOCK_CLEAR': 'true',
    'P25_LIVE_SETTLEMENT_WAIT_SEC': '15',
    'P25_LIVE_SETTLEMENT_POLL_SEC': '0.5',
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
chmod 600 .env

echo "=== DEPENDENCIES ==="
./.venv/bin/python -m pip install -q -r requirements.txt
# The UI can arm LIVE at runtime, so install the already-pinned official LIVE SDKs
# during normal P2.5 deploy. No credential is touched and no order is submitted here.
./.venv/bin/python -m pip install -q -r requirements-live.txt

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

# Reuse the existing arbitrage credential/control file without copying secrets into
# the P2.5 .env. .env is sourced second so P2.5-specific safe defaults win.
set -a
if [[ -f .env.p3 ]]; then
  # shellcheck disable=SC1091
  source ./.env.p3
fi
# shellcheck disable=SC1091
source ./.env
set +a

if [[ -n "${P25_LIVE_CONTROL_PASSWORD:-${P3_WEB_PASSWORD:-}}" ]]; then
  echo "XRP5m LIVE UI control auth=READY (P25/P3 operator password)"
else
  echo "WARNING: XRP5m LIVE UI butonu gorunecek fakat kontrol sifresi yok; ARM endpoint fail-closed kalacak." >&2
fi
if [[ -n "${POLYMARKET_PRIVATE_KEY:-${PK:-}}" ]]; then
  echo "XRP5m LIVE credentials=FOUND via process env/.env.p3 (secret values hidden)"
else
  echo "WARNING: Polymarket LIVE credential bulunamadi; ARM preflight fail-closed kalacak." >&2
fi

echo "=== START P2.5 SHADOW + DEEP VALUE DUAL-SIDE PAPER 5M ONLY ==="
nohup ./.venv/bin/python p25_main.py > engine.log 2>&1 &
new_pid=$!
echo "$new_pid" > direction-engine.pid
sleep 3

if ! kill -0 "$new_pid" 2>/dev/null; then
  echo "ERROR: P2.5 process baslatilamadi" >&2
  tail -n 150 engine.log >&2 || true
  exit 1
fi

base_url="http://127.0.0.1:8091"

wait_http_200() {
  local endpoint="$1"
  local output="$2"
  local attempts="${3:-24}"
  local max_time="${4:-5}"
  local code="000"
  local i

  for ((i=1; i<=attempts; i++)); do
    code="$(curl -sS \
      --connect-timeout 1 \
      --max-time "$max_time" \
      -o "$output" \
      -w '%{http_code}' \
      "$base_url$endpoint" 2>/dev/null || true)"
    if [[ "$code" == "200" ]]; then
      echo "HTTP PASS endpoint=$endpoint attempt=$i code=200 max_time=${max_time}s"
      return 0
    fi
    echo "HTTP WAIT endpoint=$endpoint attempt=$i/$attempts code=${code:-000} max_time=${max_time}s"
    if ! kill -0 "$new_pid" 2>/dev/null; then
      echo "ERROR: P2.5 process HTTP beklerken durdu endpoint=$endpoint" >&2
      tail -n 150 engine.log >&2 || true
      return 1
    fi
    sleep 1
  done

  echo "ERROR: $endpoint HTTP=${code:-000} after ${attempts} attempts" >&2
  tail -n 150 engine.log >&2 || true
  return 1
}

wait_http_200 "/health" /tmp/direction-p25-health.json 30 5
wait_http_200 "/api/state" /tmp/direction-p25-state.json 3 45
wait_http_200 "/paper-trades" /tmp/direction-p25-paper.html 24 5
wait_http_200 "/api/paper-trades?limit=1" /tmp/direction-p25-paper-api.json 24 10
wait_http_200 "/api/paper-summary" /tmp/direction-p25-paper-summary.json 6 20
wait_http_200 "/api/xrp5m-live/status" /tmp/direction-p25-xrp-live.json 6 10

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
live_api = json.loads(Path('/tmp/direction-p25-xrp-live.json').read_text(encoding='utf-8'))
safety = state.get('safety', {})
paper = state.get('paper_trading', {})
policy = paper.get('policy', {})
deep = paper.get('deep_value', {})
live = state.get('xrp5m_live_pilot', {})
print('phase=', state.get('phase'))
print('mode=', state.get('mode'))
print('markets_active=', state.get('footer', {}).get('markets_active'))
print('training=', safety.get('model_training_enabled'))
print('calibration=', safety.get('calibration_enabled'))
print('forecast_recording=', safety.get('forecast_recording_enabled'))
print('paper_trading=', safety.get('paper_trading_enabled'))
print('paper_entry_mode=', paper.get('entry_mode'))
print('paper_strategy=', policy.get('strategy_version'))
print('paper_stake=', policy.get('stake_usdc'))
print('deep_min_ask=', deep.get('min_ask'))
print('deep_max_ask=', deep.get('max_ask'))
print('deep_require_depth=', deep.get('require_depth'))
print('deep_max_book_age_ms=', deep.get('max_book_age_ms'))
print('deep_allowed_horizons=', deep.get('allowed_horizons'))
print('xrp_live_scope=', live.get('scope'))
print('xrp_live_armed=', live.get('armed'))
print('xrp_live_max_stake=', live.get('max_stake_usdc'))
print('xrp_live_drift=', live.get('max_price_drift_pct'))
print('xrp_live_control_available=', live_api.get('control_available'))
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
assert int(safety.get('live_orders') or 0) >= 0
assert paper.get('enabled') is True
assert paper.get('entry_mode') == 'DEEP_VALUE_WATCH'
assert policy.get('strategy_version') == 'DEEP_VALUE_25C_5M_DUAL_V1'
assert float(policy.get('stake_usdc') or 0) == 1.0
assert float(deep.get('min_ask') or 0) == 0.01
assert float(deep.get('max_ask') or 0) == 0.25
assert deep.get('require_depth') is True
assert deep.get('allowed_horizons') == ['5m']
assert live.get('scope') == 'XRP:5m'
assert live.get('armed') is False
assert abs(float(live.get('max_stake_usdc') or 0) - 1.10) < 1e-9
assert abs(float(live.get('max_price_drift_pct') or 0) - 0.10) < 1e-9
assert health.get('paper_records_page') == '/paper-trades'
assert health.get('paper_records_api') == '/api/paper-trades'
assert paper_api.get('paperOnly') is True
assert paper_api.get('source') == 'sqlite'
assert paper_summary.get('paperOnly') is True
assert paper_summary.get('source') == 'sqlite'
PY

echo "P2.5 DEPLOY PASS | paper=1.00 USDC | XRP5m LIVE attached+UNARMED | live_cap=1.10 USDC | max_price_drift=10% | UI=/paper-trades"
