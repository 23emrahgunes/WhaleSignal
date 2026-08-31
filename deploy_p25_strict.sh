#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")"

# If STRICT was deployed before, neutralize only the master strict switch before
# invoking the normal deploy. This keeps deploy_p25.sh idempotent and lets its own
# assertions run against the repository-standard non-strict baseline first.
if [[ -x ./.venv/bin/python ]]; then
  ./.venv/bin/python - <<'PY'
from pathlib import Path
p = Path('.env')
if p.exists():
    lines = p.read_text(encoding='utf-8').splitlines()
    out = []
    seen = False
    for line in lines:
        if line.strip().startswith('PAPER_STRICT_ENTRY_ENABLED='):
            out.append('PAPER_STRICT_ENTRY_ENABLED=false')
            seen = True
        else:
            out.append(line)
    if not seen:
        out.append('PAPER_STRICT_ENTRY_ENABLED=false')
    p.write_text('\n'.join(out).rstrip() + '\n', encoding='utf-8')
PY
fi

# First run the normal P2.5 deployment so dependencies, schema, compile and the full
# regression suite are validated with the repository-standard path.
bash ./deploy_p25.sh

./.venv/bin/python - <<'PY'
from pathlib import Path

path = Path('.env')
text = path.read_text(encoding='utf-8') if path.exists() else ''
wanted = {
    'PAPER_ENTRY_MODE': 'DEEP_VALUE_WATCH',
    'PAPER_STRATEGY_VERSION': 'INDEP_PTB_BINANCE_DIRECTIONAL_5M_V2',
    'PAPER_STAKE_USDC': '1.00',
    'PAPER_MIN_CONFIDENCE': '0.34',
    'PAPER_MIN_AGREEMENT': '0.00',
    'PAPER_MIN_EDGE': '0.08',
    'PAPER_ALLOWED_STATUSES': 'PROVISIONAL,VALIDATED',
    'PAPER_ALLOWED_GRADES': 'MEDIUM,HIGH',
    'PAPER_DEEP_VALUE_MIN_ASK': '0.05',
    'PAPER_DEEP_VALUE_MAX_ASK': '0.75',
    'PAPER_DEEP_VALUE_PREFILTER_BUFFER': '0.02',
    'PAPER_DEEP_VALUE_MIN_TTE_SEC': '5',
    'PAPER_DEEP_VALUE_ENTRY_TTE_MIN_SEC': '60',
    'PAPER_DEEP_VALUE_ENTRY_TTE_MAX_SEC': '75',
    'PAPER_DEEP_VALUE_MAX_BOOK_AGE_MS': '750',
    'PAPER_DEEP_VALUE_REQUIRE_DEPTH': 'true',
    'PAPER_DEEP_VALUE_MIN_DEPTH_MULTIPLE': '1.50',
    'PAPER_DEEP_VALUE_REQUIRE_FEE_SCHEDULE': 'true',
    'PAPER_DEEP_VALUE_MIN_VALUE_MULTIPLE': '1.12',
    'PAPER_DEEP_VALUE_HORIZONS': '5m',
    'PAPER_INDEPENDENT_ALPHA_ENABLED': 'true',
    'PAPER_INDEPENDENT_DEADZONE_LOW': '0.33',
    'PAPER_INDEPENDENT_DEADZONE_HIGH': '0.67',
    'PAPER_INDEPENDENT_BINANCE_MAX_SIGMA_SHIFT': '0.35',
    'PAPER_STRICT_ENTRY_ENABLED': 'true',
    'PAPER_STRICT_DIRECTION_LOCK': 'true',
    'PAPER_STRICT_REQUIRE_OFFICIAL_CURRENT': 'true',
    'PAPER_STRICT_REQUIRE_PTB_SIDE_ALIGNMENT': 'true',
    'PAPER_STRICT_MIN_ABS_Z': '0.45',
    'PAPER_STRICT_MAX_COUNTER_SIGMA': '0.10',
    'PAPER_STRICT_MAX_VOL_PERCENTILE': '0.92',
    'PAPER_STRICT_MAX_FLIP_RATE': '0.68',
    'PAPER_STRICT_MAX_VOL_ACCEL': '1.80',
    'PAPER_STRICT_STABILITY_SEC': '3.0',
    'PAPER_STRICT_STABILITY_MAX_GAP_SEC': '1.5',
    # ALL-5m LIVE is always fail-closed after deploy. DRY must pass in the UI before
    # the operator can arm BTC/ETH/SOL/XRP. Per-order paper stake remains $1.00;
    # 10% price drift means a live order can spend at most $1.10.
    'P25_LIVE_FEATURE_ENABLED': 'false',
    'P25_LIVE_ARMED': 'false',
    'P25_LIVE_ARM_NONCE': '',
    'P25_LIVE_STRATEGY_VERSION': 'INDEP_PTB_BINANCE_DIRECTIONAL_5M_V2',
    'P25_LIVE_MAX_STAKE_USDC': '1.10',
    'P25_LIVE_MAX_PRICE_DRIFT_PCT': '0.10',
    # Paper ask can reach 75c, fill ~75.5c and +10% drift ~83.05c. Keep a bounded
    # 83c hard cap; the notional cap remains the stronger $1.10/order constraint.
    'P25_LIVE_MAX_LIMIT_PRICE': '0.83',
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

echo "=== RESTART P2.5 WITH DIRECTIONAL EDGE V2 + ALL5M DRY-FIRST LIVE ==="
pkill -f 'python.*p25_main.py' 2>/dev/null || true
sleep 2

set -a
if [[ -f .env.p3 ]]; then
  # shellcheck disable=SC1091
  source ./.env.p3
fi
# shellcheck disable=SC1091
source ./.env
set +a

nohup ./.venv/bin/python p25_main.py > engine.log 2>&1 &
new_pid=$!
echo "$new_pid" > direction-engine.pid

base_url="http://127.0.0.1:8091"
code="000"
for i in $(seq 1 30); do
  code="$(curl -sS --connect-timeout 1 --max-time 10 \
    -o /tmp/direction-p25-strict-state.json -w '%{http_code}' \
    "$base_url/api/state" 2>/dev/null || true)"
  if [[ "$code" == "200" ]]; then
    break
  fi
  if ! kill -0 "$new_pid" 2>/dev/null; then
    echo "ERROR: DIRECTIONAL V2 process durdu" >&2
    tail -n 180 engine.log >&2 || true
    exit 1
  fi
  sleep 1
done
if [[ "$code" != "200" ]]; then
  echo "ERROR: DIRECTIONAL V2 /api/state HTTP=$code" >&2
  tail -n 180 engine.log >&2 || true
  exit 1
fi

# The new controller is exposed through the backwards-compatible snapshot key as
# well as the dedicated web status endpoint.
curl -fsS --connect-timeout 2 --max-time 10 \
  "$base_url/api/all5m-live/status" > /tmp/direction-p25-all5m-live.json

./.venv/bin/python - <<'PY'
import json
from pathlib import Path

state = json.loads(Path('/tmp/direction-p25-strict-state.json').read_text(encoding='utf-8'))
live = json.loads(Path('/tmp/direction-p25-all5m-live.json').read_text(encoding='utf-8'))
safety = state.get('safety', {})
paper = state.get('paper_trading', {})
policy = paper.get('policy', {})
deep = paper.get('deep_value', {})
strict = safety.get('paper_strict_profile', {})

print('strategy=', policy.get('strategy_version'))
print('alpha=', safety.get('paper_independent_alpha_source'))
print('strict=', safety.get('paper_strict_entry_enabled'))
print('entry_window=', strict.get('entry_tte_max_sec'), '->', strict.get('entry_tte_min_sec'))
print('deadzone=', strict.get('deadzone_low'), strict.get('deadzone_high'))
print('min_abs_z=', strict.get('min_abs_z'))
print('stability=', strict.get('stability_sec'))
print('book_age=', strict.get('max_book_age_ms'))
print('depth_multiple=', strict.get('min_depth_multiple'))
print('ask=', deep.get('min_ask'), deep.get('max_ask'))
print('min_edge=', policy.get('min_edge'))
print('min_value=', deep.get('min_value_multiple'))
print('live_scope=', live.get('scope'))
print('live_armed=', live.get('armed'))
print('dry_ready=', live.get('dry_ready'))
print('live_price_cap=', live.get('max_limit_price'))
print('live_order_cap=', live.get('max_stake_usdc'))
print('min_arm_collateral=', live.get('min_arm_collateral_usdc'))
print('execution=', safety.get('execution_enabled'))

assert policy.get('strategy_version') == 'INDEP_PTB_BINANCE_DIRECTIONAL_5M_V2'
assert safety.get('paper_independent_alpha_enabled') is True
assert safety.get('paper_independent_alpha_source') == 'INDEPENDENT_PTB_BINANCE_STRICT_V1'
assert safety.get('paper_strict_entry_enabled') is True
assert float(strict.get('entry_tte_min_sec')) == 60.0
assert float(strict.get('entry_tte_max_sec')) == 75.0
assert float(strict.get('deadzone_low')) == 0.33
assert float(strict.get('deadzone_high')) == 0.67
assert float(strict.get('min_abs_z')) == 0.45
assert float(strict.get('max_counter_sigma')) == 0.10
assert float(strict.get('stability_sec')) == 3.0
assert int(strict.get('max_book_age_ms')) == 750
assert float(strict.get('min_depth_multiple')) == 1.5
assert strict.get('require_official_current') is True
assert strict.get('direction_lock') is True
assert float(deep.get('min_ask')) == 0.05
assert float(deep.get('max_ask')) == 0.75
assert float(deep.get('min_depth_multiple')) == 1.5
assert abs(float(policy.get('min_edge')) - 0.08) < 1e-9
assert abs(float(deep.get('min_value_multiple')) - 1.12) < 1e-9
assert live.get('scope') == 'BTC/ETH/SOL/XRP:5m'
assert set(live.get('assets') or []) == {'BTC','ETH','SOL','XRP'}
assert live.get('armed') is False
assert live.get('dry_ready') is False
assert live.get('continuous_session') is True
assert live.get('one_attempt_per_condition') is True
assert live.get('post_orders_called_by_dry') is False
assert abs(float(live.get('max_limit_price')) - 0.83) < 1e-9
assert abs(float(live.get('max_stake_usdc')) - 1.10) < 1e-9
assert abs(float(live.get('min_arm_collateral_usdc')) - 4.40) < 1e-9
assert safety.get('execution_enabled') is False
PY

echo "DIRECTIONAL EDGE V2 DEPLOY PASS | strategy=INDEP_PTB_BINANCE_DIRECTIONAL_5M_V2 | entry=T-75..T-60 | P=<=33/>=67 | z>=0.45 | flip<=0.68 | stability=3s | ask=5-75c | edge>=8pt | value>=1.12x | book<=750ms | depth>=1.5x | ALL5M LIVE=DRY_REQUIRED+UNARMED | max=$1.10/order | hard=83c"
