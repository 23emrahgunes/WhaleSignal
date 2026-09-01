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

had_env=0
backup_path="$backup_dir/.env.pre-strict.$(date +%Y%m%d-%H%M%S)"
if [[ -f .env ]]; then
  had_env=1
  cp -f .env "$backup_path"
  chmod 600 "$backup_path"
  echo "STRICT env rollback backup=$backup_path"
fi

# ---------------------------------------------------------------------------
# IMPORTANT: validation is non-destructive.
# Do NOT call deploy_p25.sh here: that script intentionally writes the baseline
# profile and starts it. If a later STRICT step fails, that would leave production
# silently running the wrong strategy. Tests/compile finish first while the current
# engine keeps running unchanged.
# ---------------------------------------------------------------------------
echo "=== STRICT PRECHECK: DEPENDENCIES (current process stays untouched) ==="
./.venv/bin/python -m pip install -q -r requirements.txt
./.venv/bin/python -m pip install -q -r requirements-live.txt

echo "=== STRICT PRECHECK: SYNTAX ==="
./.venv/bin/python -m py_compile *.py reference/*.py

echo "=== STRICT PRECHECK: TESTS (dotenv isolated by conftest.py) ==="
PHASE=P1 \
MODEL_TRAINING_ENABLED=false \
CALIBRATION_ENABLED=false \
PAPER_TRADING_ENABLED=false \
./.venv/bin/pytest -q

candidate_env="$(mktemp ./.env.directional-v2.XXXXXX)"
cleanup_candidate() {
  rm -f "$candidate_env" 2>/dev/null || true
}
trap cleanup_candidate EXIT

# Build the complete V2 env in a temporary file. The live .env is not touched yet.
./.venv/bin/python - "$candidate_env" <<'PY'
from pathlib import Path
import sys

candidate = Path(sys.argv[1])
source = Path('.env')
text = source.read_text(encoding='utf-8') if source.exists() else ''

wanted = {
    # Core P2.5 runtime
    'PHASE': 'P2.5',
    'MODEL_TRAINING_ENABLED': 'true',
    'CALIBRATION_ENABLED': 'true',
    'FORECAST_RECORDING_ENABLED': 'true',
    'MODEL_PATH': 'models/direction_model.pkl',
    'CALIBRATION_PATH': 'models/calibration_book.pkl',
    'FEATURE_PRICE_RING_MAX': '24000',
    'RESOLUTION_POLL_SEC': '10',

    # Paper / Directional Edge V2
    'PAPER_TRADING_ENABLED': 'true',
    'PAPER_ENTRY_MODE': 'DEEP_VALUE_WATCH',
    'PAPER_STRATEGY_VERSION': 'INDEP_PTB_BINANCE_DIRECTIONAL_5M_V2',
    'PAPER_STARTING_BANKROLL_USDC': '1000',
    'PAPER_STAKE_USDC': '1.00',
    'PAPER_ENTRY_CHECKPOINT_5M': '60',
    'PAPER_ENTRY_CHECKPOINT_15M': '240',
    'PAPER_ENTRY_CHECKPOINT_1H': '600',
    'PAPER_MIN_CONFIDENCE': '0.34',
    'PAPER_MIN_AGREEMENT': '0.00',
    'PAPER_MIN_EDGE': '0.08',
    'PAPER_MIN_PRICE': '0.01',
    'PAPER_MAX_PRICE': '0.95',
    'PAPER_SLIPPAGE': '0.005',
    'PAPER_FEE_BPS': '0',
    'PAPER_ALLOWED_STATUSES': 'PROVISIONAL,VALIDATED',
    'PAPER_ALLOWED_GRADES': 'MEDIUM,HIGH',
    'PAPER_RECENT_LIMIT': '50',

    # Direction-locked value execution window
    'PAPER_DEEP_VALUE_MIN_ASK': '0.05',
    'PAPER_DEEP_VALUE_MAX_ASK': '0.75',
    'PAPER_DEEP_VALUE_PREFILTER_BUFFER': '0.02',
    'PAPER_DEEP_VALUE_MIN_TTE_SEC': '5',
    'PAPER_DEEP_VALUE_ENTRY_TTE_MIN_SEC': '60',
    'PAPER_DEEP_VALUE_ENTRY_TTE_MAX_SEC': '75',
    'PAPER_DEEP_VALUE_P26_DB_PATH': 'data/p26_research.sqlite',
    'PAPER_DEEP_VALUE_MAX_BOOK_AGE_MS': '750',
    'PAPER_DEEP_VALUE_REQUIRE_DEPTH': 'true',
    'PAPER_DEEP_VALUE_MIN_DEPTH_MULTIPLE': '1.50',
    'PAPER_DEEP_VALUE_REQUIRE_FEE_SCHEDULE': 'true',
    'PAPER_DEEP_VALUE_MIN_VALUE_MULTIPLE': '1.12',
    'PAPER_DEEP_VALUE_HORIZONS': '5m',

    # Independent PTB + Binance alpha
    'PAPER_INDEPENDENT_ALPHA_ENABLED': 'true',
    'PAPER_INDEPENDENT_DEADZONE_LOW': '0.33',
    'PAPER_INDEPENDENT_DEADZONE_HIGH': '0.67',
    'PAPER_INDEPENDENT_BINANCE_MAX_SIGMA_SHIFT': '0.35',
    'PAPER_INDEPENDENT_MAX_BASIS_BPS': '50',
    'PAPER_INDEPENDENT_MAX_BASIS_OPEN_GAP_MS': '5000',

    # STRICT gates
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

    # ALL-5m LIVE attaches but every deploy starts fail-closed/unarmed.
    'P25_LIVE_FEATURE_ENABLED': 'false',
    'P25_LIVE_ARMED': 'false',
    'P25_LIVE_ARM_NONCE': '',
    # Legacy envelope remains XRP because startup validation still owns that field;
    # controller selection is strategy-driven and V2 attaches BTC/ETH/SOL/XRP 5m.
    'P25_LIVE_ASSET': 'XRP',
    'P25_LIVE_HORIZON': '5m',
    'P25_LIVE_STRATEGY_VERSION': 'INDEP_PTB_BINANCE_DIRECTIONAL_5M_V2',
    'P25_LIVE_MAX_STAKE_USDC': '1.10',
    # Retained only for backwards-compatible config loading. The ALL5m fast-lane
    # executor no longer anchors its live price to paper_fill * (1+drift).
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

candidate.write_text('\n'.join(out).rstrip() + '\n', encoding='utf-8')
PY
chmod 600 "$candidate_env"

# Validate the candidate in a clean process environment so an exported shell value
# cannot override the candidate file during this check.
echo "=== STRICT PRECHECK: CANDIDATE CONFIG ==="
env -i PATH="$PATH" HOME="$HOME" \
  ./.venv/bin/python - "$candidate_env" <<'PY'
import sys
from p25_deep_value_config import DeepValuePaperSettings

path = sys.argv[1]
cfg = DeepValuePaperSettings(_env_file=path)
cfg.enforce_phase_lock()
assert cfg.phase == 'P2.5'
assert cfg.paper_strategy_version == 'INDEP_PTB_BINANCE_DIRECTIONAL_5M_V2'
assert cfg.paper_strict_entry_enabled is True
assert cfg.paper_independent_alpha_enabled is True
assert abs(cfg.paper_independent_deadzone_low - 0.33) < 1e-12
assert abs(cfg.paper_independent_deadzone_high - 0.67) < 1e-12
assert cfg.paper_allowed_grades() == {'MEDIUM', 'HIGH'}
assert abs(cfg.paper_min_edge - 0.08) < 1e-12
assert abs(cfg.paper_deep_value_min_value_multiple - 1.12) < 1e-12
assert abs(cfg.paper_deep_value_max_ask - 0.75) < 1e-12
assert abs(cfg.p25_live_max_stake_usdc - 1.10) < 1e-12
assert abs(cfg.p25_live_max_limit_price - 0.83) < 1e-12
assert cfg.p25_live_armed is False
print('CANDIDATE CONFIG PASS')
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
    exec ./.venv/bin/python p25_main.py
  ' > engine.log 2>&1 &
  echo $!
}

rollback_and_restart() {
  local rc=$?
  trap - ERR
  set +e
  if [[ "$applied" == "1" ]]; then
    echo "ERROR: STRICT deploy failed after activation; rolling back previous .env" >&2
    if [[ -n "${new_pid:-}" ]]; then kill "$new_pid" 2>/dev/null || true; fi
    pkill -f 'python.*p25_main.py' 2>/dev/null || true
    sleep 1
    if [[ "$had_env" == "1" && -f "$backup_path" ]]; then
      cp -f "$backup_path" .env
      chmod 600 .env
      old_pid="$(start_engine)"
      echo "$old_pid" > direction-engine.pid
      echo "ROLLBACK: previous profile restarted pid=$old_pid" >&2
    else
      rm -f .env
      echo "ROLLBACK: previous .env did not exist; engine left stopped" >&2
    fi
  fi
  exit "$rc"
}
trap rollback_and_restart ERR

# All validation passed. Only now replace production .env atomically and restart.
echo "=== ACTIVATE DIRECTIONAL EDGE V2 (atomic) ==="
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
for i in $(seq 1 30); do
  code="$(curl -sS --connect-timeout 1 --max-time 10 \
    -o /tmp/direction-p25-strict-state.json -w '%{http_code}' \
    "$base_url/api/state" 2>/dev/null || true)"
  if [[ "$code" == "200" ]]; then break; fi
  if ! kill -0 "$new_pid" 2>/dev/null; then
    echo "ERROR: DIRECTIONAL V2 process durdu" >&2
    tail -n 180 engine.log >&2 || true
    false
  fi
  sleep 1
done
if [[ "$code" != "200" ]]; then
  echo "ERROR: DIRECTIONAL V2 /api/state HTTP=$code" >&2
  tail -n 180 engine.log >&2 || true
  false
fi

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
print('live_order_mode=', live.get('order_mode'))
print('live_execution_price_mode=', live.get('execution_price_mode'))
print('live_paper_drift_enforced=', live.get('paper_drift_enforced'))
print('live_min_edge=', live.get('live_min_edge'))
print('live_parallel_execution=', live.get('parallel_execution'))
print('live_max_parallel_workers=', live.get('max_parallel_workers'))
print('live_market_buy_usdc=', live.get('market_buy_usdc'))
print('live_min_fak_depth_usdc=', live.get('min_fak_depth_usdc'))
print('live_positive_depth_only=', live.get('positive_depth_only'))
print('live_partial_fill_ok=', live.get('partial_fill_ok'))
print('live_fak_no_match_is_normal=', live.get('fak_no_match_is_normal'))
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
assert abs(float(policy.get('min_edge')) - 0.08) < 1e-9
assert abs(float(deep.get('min_value_multiple')) - 1.12) < 1e-9
assert live.get('scope') == 'BTC/ETH/SOL/XRP:5m'
assert set(live.get('assets') or []) == {'BTC','ETH','SOL','XRP'}
assert live.get('armed') is False
assert live.get('dry_ready') is False
assert live.get('continuous_session') is True
assert live.get('one_attempt_per_condition') is True
assert live.get('post_orders_called_by_dry') is False
assert live.get('order_mode') == 'MARKETABLE_FAK_LIVE_EDGE_CAP'
assert live.get('execution_price_mode') == 'CURRENT_BOOK_WITH_LIVE_EDGE_CAP'
assert live.get('paper_drift_enforced') is False
assert abs(float(live.get('live_min_edge')) - 0.08) < 1e-9
assert live.get('parallel_execution') is True
assert int(live.get('max_parallel_workers')) == 4
assert abs(float(live.get('market_buy_usdc')) - 1.00) < 1e-9
assert 0.0 < float(live.get('min_fak_depth_usdc')) <= 1e-8
assert live.get('positive_depth_only') is True
assert live.get('partial_fill_ok') is True
assert live.get('fak_no_match_is_normal') is True
assert abs(float(live.get('max_limit_price')) - 0.83) < 1e-9
assert abs(float(live.get('max_stake_usdc')) - 1.10) < 1e-9
assert safety.get('execution_enabled') is False
PY

applied=0
trap - ERR
printf '%s\n' 'DIRECTIONAL EDGE V2 DEPLOY PASS | transactional=true | strategy=INDEP_PTB_BINANCE_DIRECTIONAL_5M_V2 | entry=T-75..T-60 | P=<=33/>=67 | z>=0.45 | flip<=0.68 | stability=3s | ask=5-75c | edge>=8pt | value>=1.12x | book<=750ms | depth>=1.5x | ALL5M LIVE=DRY_REQUIRED+UNARMED | order=FAK-$1@LIVE_EDGE | paper_drift=OFF | live_edge>=8pt | parallel=4 | max=$1.10/order | hard=83c'
