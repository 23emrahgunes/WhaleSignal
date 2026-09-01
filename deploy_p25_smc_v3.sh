#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")"

if [[ ! -x ./.venv/bin/python ]]; then
  echo "ERROR: .venv bulunamadi" >&2
  exit 1
fi

mkdir -p data models
backup_dir="$HOME/.direction-engine-env-backups"
mkdir -p "$backup_dir"
chmod 700 "$backup_dir"

had_env=0
backup_path="$backup_dir/.env.pre-smc-v3.$(date +%Y%m%d-%H%M%S)"
if [[ -f .env ]]; then
  had_env=1
  cp -f .env "$backup_path"
  chmod 600 "$backup_path"
  echo "SMC V3 rollback backup=$backup_path"
fi

echo "=== SMC V3 PRECHECK: DEPENDENCIES ==="
./.venv/bin/python -m pip install -q -r requirements.txt
./.venv/bin/python -m pip install -q -r requirements-live.txt

echo "=== SMC V3 PRECHECK: SYNTAX ==="
./.venv/bin/python -m py_compile *.py reference/*.py
bash -n deploy_p25_smc_v3.sh

echo "=== SMC V3 PRECHECK: TESTS (sterile process env) ==="
env -i PATH="$PATH" HOME="$HOME" \
  PHASE=P1 MODEL_TRAINING_ENABLED=false CALIBRATION_ENABLED=false PAPER_TRADING_ENABLED=false \
  ./.venv/bin/pytest -q

candidate_env="$(mktemp ./.env.smc-v3.XXXXXX)"
cleanup_candidate() { rm -f "$candidate_env" 2>/dev/null || true; }
trap cleanup_candidate EXIT

./.venv/bin/python - "$candidate_env" <<'PY'
from pathlib import Path
import sys

candidate = Path(sys.argv[1])
source = Path('.env')
text = source.read_text(encoding='utf-8') if source.exists() else ''

wanted = {
    'PHASE': 'P2.5',
    'MODEL_TRAINING_ENABLED': 'true',
    'CALIBRATION_ENABLED': 'true',
    'FORECAST_RECORDING_ENABLED': 'true',
    'MODEL_PATH': 'models/direction_model.pkl',
    'CALIBRATION_PATH': 'models/calibration_book.pkl',
    'FEATURE_PRICE_RING_MAX': '24000',
    'RESOLUTION_POLL_SEC': '10',

    # Separate cohort: PTB + Binance + SMC structural confirmation.
    'PAPER_TRADING_ENABLED': 'true',
    'PAPER_ENTRY_MODE': 'DEEP_VALUE_WATCH',
    'PAPER_STRATEGY_VERSION': 'INDEP_PTB_BINANCE_SMC_SELECTIVE_5M_V3',
    'PAPER_STARTING_BANKROLL_USDC': '1000',
    'PAPER_STAKE_USDC': '1.00',
    'PAPER_MIN_CONFIDENCE': '0.44',
    'PAPER_MIN_AGREEMENT': '0.00',
    'PAPER_MIN_EDGE': '0.10',
    'PAPER_MIN_PRICE': '0.01',
    'PAPER_MAX_PRICE': '0.95',
    'PAPER_SLIPPAGE': '0.005',
    'PAPER_FEE_BPS': '0',
    'PAPER_ALLOWED_STATUSES': 'PROVISIONAL,VALIDATED',
    'PAPER_ALLOWED_GRADES': 'MEDIUM,HIGH',
    'PAPER_RECENT_LIMIT': '50',

    'PAPER_DEEP_VALUE_MIN_ASK': '0.05',
    'PAPER_DEEP_VALUE_MAX_ASK': '0.75',
    'PAPER_DEEP_VALUE_PREFILTER_BUFFER': '0.02',
    'PAPER_DEEP_VALUE_MIN_TTE_SEC': '5',
    'PAPER_DEEP_VALUE_ENTRY_TTE_MIN_SEC': '60',
    'PAPER_DEEP_VALUE_ENTRY_TTE_MAX_SEC': '75',
    'PAPER_DEEP_VALUE_P26_DB_PATH': 'data/p26_research.sqlite',
    'PAPER_DEEP_VALUE_MAX_BOOK_AGE_MS': '600',
    'PAPER_DEEP_VALUE_REQUIRE_DEPTH': 'true',
    'PAPER_DEEP_VALUE_MIN_DEPTH_MULTIPLE': '1.50',
    'PAPER_DEEP_VALUE_REQUIRE_FEE_SCHEDULE': 'true',
    'PAPER_DEEP_VALUE_MIN_VALUE_MULTIPLE': '1.15',
    'PAPER_DEEP_VALUE_HORIZONS': '5m',

    'PAPER_INDEPENDENT_ALPHA_ENABLED': 'true',
    'PAPER_INDEPENDENT_DEADZONE_LOW': '0.28',
    'PAPER_INDEPENDENT_DEADZONE_HIGH': '0.72',
    'PAPER_INDEPENDENT_BINANCE_MAX_SIGMA_SHIFT': '0.25',
    'PAPER_INDEPENDENT_MAX_BASIS_BPS': '50',
    'PAPER_INDEPENDENT_MAX_BASIS_OPEN_GAP_MS': '5000',

    'PAPER_STRICT_ENTRY_ENABLED': 'true',
    'PAPER_STRICT_DIRECTION_LOCK': 'true',
    'PAPER_STRICT_REQUIRE_OFFICIAL_CURRENT': 'true',
    'PAPER_STRICT_REQUIRE_PTB_SIDE_ALIGNMENT': 'true',
    'PAPER_STRICT_MIN_ABS_Z': '0.58',
    'PAPER_STRICT_MAX_COUNTER_SIGMA': '0.08',
    'PAPER_STRICT_MAX_VOL_PERCENTILE': '0.90',
    'PAPER_STRICT_MAX_FLIP_RATE': '0.55',
    'PAPER_STRICT_MAX_VOL_ACCEL': '1.60',
    'PAPER_STRICT_STABILITY_SEC': '5.0',
    'PAPER_STRICT_STABILITY_MAX_GAP_SEC': '1.25',

    # Guarded ALL5m LIVE; deploy always starts unarmed and DRY-required.
    'P25_LIVE_FEATURE_ENABLED': 'false',
    'P25_LIVE_ARMED': 'false',
    'P25_LIVE_ARM_NONCE': '',
    'P25_LIVE_ASSET': 'XRP',
    'P25_LIVE_HORIZON': '5m',
    'P25_LIVE_STRATEGY_VERSION': 'INDEP_PTB_BINANCE_SMC_SELECTIVE_5M_V3',
    'P25_LIVE_MAX_STAKE_USDC': '1.10',
    'P25_LIVE_MAX_PRICE_DRIFT_PCT': '0.10',
    'P25_LIVE_MAX_LIMIT_PRICE': '0.83',
    'P25_LIVE_LEDGER_PATH': 'data/p25_live_direction.sqlite',
    'P25_LIVE_CLOB_HOST': 'https://clob.polymarket.com',
    'P25_LIVE_CHAIN_ID': '137',
    'P25_LIVE_GEOBLOCK_URL': 'https://polymarket.com/api/geoblock',
    'P25_LIVE_REQUIRE_GEOBLOCK_CLEAR': 'true',
    'P25_LIVE_SETTLEMENT_WAIT_SEC': '15',
    'P25_LIVE_SETTLEMENT_POLL_SEC': '0.5',
}

out = []
seen = set()
for line in text.splitlines():
    stripped = line.strip()
    key = stripped.split('=', 1)[0] if '=' in stripped else ''
    if key in wanted:
        out.append(f'{key}={wanted[key]}')
        seen.add(key)
    else:
        out.append(line)
for key, value in wanted.items():
    if key not in seen:
        out.append(f'{key}={value}')
candidate.write_text('\n'.join(out).rstrip() + '\n', encoding='utf-8')
PY
chmod 600 "$candidate_env"

echo "=== SMC V3 PRECHECK: CANDIDATE CONFIG ==="
env -i PATH="$PATH" HOME="$HOME" ./.venv/bin/python - "$candidate_env" <<'PY'
import sys
from p25_deep_value_config import DeepValuePaperSettings
from p25_smc_patch import SMC_STRATEGY

cfg = DeepValuePaperSettings(_env_file=sys.argv[1])
cfg.enforce_phase_lock()
assert cfg.paper_strategy_version == SMC_STRATEGY
assert cfg.p25_live_strategy_version == SMC_STRATEGY
assert cfg.paper_strict_entry_enabled is True
assert cfg.paper_independent_alpha_enabled is True
assert abs(cfg.paper_independent_deadzone_low - 0.28) < 1e-12
assert abs(cfg.paper_independent_deadzone_high - 0.72) < 1e-12
assert abs(cfg.paper_strict_min_abs_z - 0.58) < 1e-12
assert abs(cfg.paper_strict_stability_sec - 5.0) < 1e-12
assert abs(cfg.paper_min_edge - 0.10) < 1e-12
assert abs(cfg.paper_deep_value_min_value_multiple - 1.15) < 1e-12
assert cfg.paper_deep_value_max_book_age_ms == 600
assert cfg.p25_live_armed is False
print('SMC V3 CANDIDATE CONFIG PASS')
PY

applied=0
new_pid=""
start_engine() {
  nohup bash -c '
    set -Eeuo pipefail
    set -a
    if [[ -f .env.p3 ]]; then source ./.env.p3; fi
    source ./.env
    set +a
    exec ./.venv/bin/python p25_main_smc.py
  ' > engine.log 2>&1 &
  echo $!
}

rollback_and_restart() {
  local rc=$?
  trap - ERR
  set +e
  if [[ "$applied" == "1" ]]; then
    echo "ERROR: SMC V3 activation failed; rolling back" >&2
    [[ -n "${new_pid:-}" ]] && kill "$new_pid" 2>/dev/null || true
    pkill -f 'python.*p25_main_smc.py' 2>/dev/null || true
    pkill -f 'python.*p25_main.py' 2>/dev/null || true
    sleep 1
    if [[ "$had_env" == "1" && -f "$backup_path" ]]; then
      cp -f "$backup_path" .env
      chmod 600 .env
      # Previous profile may be V2; choose entrypoint from strategy string.
      if grep -q '^PAPER_STRATEGY_VERSION=INDEP_PTB_BINANCE_SMC_SELECTIVE_5M_V3$' .env; then
        old_pid="$(start_engine)"
      else
        old_pid="$(nohup bash -c 'set -a; [[ -f .env.p3 ]] && source ./.env.p3; source ./.env; set +a; exec ./.venv/bin/python p25_main.py' > engine.log 2>&1 & echo $!)"
      fi
      echo "$old_pid" > direction-engine.pid
      echo "ROLLBACK previous profile restarted pid=$old_pid" >&2
    fi
  fi
  exit "$rc"
}
trap rollback_and_restart ERR

echo "=== ACTIVATE SMC SELECTIVE V3 (atomic) ==="
pkill -f 'python.*p25_main_smc.py' 2>/dev/null || true
pkill -f 'python.*p25_main.py' 2>/dev/null || true
sleep 2
mv -f "$candidate_env" .env
candidate_env=""
chmod 600 .env
applied=1

new_pid="$(start_engine)"
echo "$new_pid" > direction-engine.pid

base_url="http://127.0.0.1:8091"
code="000"
for i in $(seq 1 35); do
  code="$(curl -sS --connect-timeout 1 --max-time 15 -o /tmp/smc-v3-state.json -w '%{http_code}' "$base_url/api/state" 2>/dev/null || true)"
  [[ "$code" == "200" ]] && break
  if ! kill -0 "$new_pid" 2>/dev/null; then
    echo "ERROR: SMC V3 process stopped" >&2
    tail -n 200 engine.log >&2 || true
    false
  fi
  sleep 1
done
[[ "$code" == "200" ]] || { echo "ERROR: /api/state HTTP=$code" >&2; false; }

curl -fsS --connect-timeout 2 --max-time 15 "$base_url/api/all5m-live/status" > /tmp/smc-v3-live.json

./.venv/bin/python - <<'PY'
import json
from pathlib import Path
from p25_smc_patch import SMC_SOURCE, SMC_STRATEGY

state=json.loads(Path('/tmp/smc-v3-state.json').read_text())
live=json.loads(Path('/tmp/smc-v3-live.json').read_text())
safety=state.get('safety') or {}
paper=state.get('paper_trading') or {}
policy=paper.get('policy') or {}
strict=safety.get('paper_strict_profile') or {}

print('strategy=', policy.get('strategy_version'))
print('alpha=', safety.get('paper_independent_alpha_source'))
print('strict=', safety.get('paper_strict_entry_enabled'))
print('window=', strict.get('entry_tte_max_sec'), '->', strict.get('entry_tte_min_sec'))
print('deadzone=', strict.get('deadzone_low'), strict.get('deadzone_high'))
print('z_min=', strict.get('min_abs_z'))
print('stability=', strict.get('stability_sec'))
print('smc_enabled=', strict.get('smc_enabled'))
print('smc_components=', strict.get('smc_components'))
print('smc_min_confirmations=', strict.get('smc_min_confirmations'))
print('smc_structure_required=', strict.get('smc_require_structure'))
print('smc_min_score=', strict.get('smc_min_aligned_score'))
print('live_scope=', live.get('scope'))
print('order_mode=', live.get('order_mode'))
print('paper_drift=', live.get('paper_drift_enforced'))
print('book_precheck=', live.get('pre_submit_book_check'))
print('parallel=', live.get('max_parallel_workers'))
print('armed=', live.get('armed'))

assert policy.get('strategy_version') == SMC_STRATEGY
assert safety.get('paper_independent_alpha_source') == SMC_SOURCE
assert safety.get('paper_strict_entry_enabled') is True
assert strict.get('smc_enabled') is True
assert int(strict.get('smc_min_confirmations')) == 2
assert strict.get('smc_require_structure') is True
assert abs(float(strict.get('smc_min_aligned_score')) - 0.45) < 1e-9
assert float(strict.get('deadzone_low')) == 0.28
assert float(strict.get('deadzone_high')) == 0.72
assert float(strict.get('min_abs_z')) == 0.58
assert float(strict.get('stability_sec')) == 5.0
assert live.get('scope') == 'BTC/ETH/SOL/XRP:5m'
assert live.get('order_mode') == 'SIGNAL_IMMEDIATE_FAK_LIVE_EDGE_CAP'
assert live.get('paper_drift_enforced') is False
assert live.get('pre_submit_book_check') is False
assert int(live.get('max_parallel_workers')) == 4
assert live.get('armed') is False
PY

applied=0
trap - ERR
printf '%s\n' 'SMC SELECTIVE V3 DEPLOY PASS | transactional=true | strategy=INDEP_PTB_BINANCE_SMC_SELECTIVE_5M_V3 | entry=T-75..T-60 | P<=28/>=72 | z>=0.58 | stability=5s | flip<=0.55 | SMC=STRUCTURE+2of3+score>=0.45 | edge>=10pt | value>=1.15x | book<=600ms | LIVE=DRY_REQUIRED+UNARMED | FAK-$1@SIGNAL_LIMIT | drift=OFF | book_precheck=OFF | parallel=4 | hard=83c'
