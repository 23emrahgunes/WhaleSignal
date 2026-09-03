#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: ./deploy_p3_dual40.sh [--no-pull] [--skip-tests]

Installs the DUAL40_MAKER_RECOVERY_V1 P3 profile. The service always restarts DRY.
The script does not clear paper history or a persistent hard stop.

Optional environment controls:
  P3_DUAL40_DEPLOY_BRANCH   Branch to deploy (default: current checked-out branch)
  P3_DUAL40_EXPECTED_COMMIT Exact commit required after pull
EOF
}

DO_PULL=1
RUN_TESTS=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-pull) DO_PULL=0 ;;
    --skip-tests) RUN_TESTS=0 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "ERROR: unknown option $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"
SUDO=""
[[ "$EUID" -eq 0 ]] || SUDO=sudo
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[[ "$CURRENT_BRANCH" != "HEAD" ]] || {
  echo "ERROR: detached HEAD is not deployable; checkout an explicit branch" >&2
  exit 1
}
DEPLOY_BRANCH="${P3_DUAL40_DEPLOY_BRANCH:-$CURRENT_BRANCH}"
EXPECTED_COMMIT="${P3_DUAL40_EXPECTED_COMMIT:-}"

if [[ "$DO_PULL" == "1" ]]; then
  [[ -z "$(git status --porcelain --untracked-files=all)" ]] || {
    echo "ERROR: working tree dirty" >&2
    exit 1
  }
  git fetch origin "$DEPLOY_BRANCH"
  git checkout "$DEPLOY_BRANCH"
  git pull --ff-only origin "$DEPLOY_BRANCH"
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[[ "$BRANCH" == "$DEPLOY_BRANCH" ]] || {
  echo "ERROR: expected branch=$DEPLOY_BRANCH actual=$BRANCH" >&2
  exit 1
}
DEPLOY_COMMIT="$(git rev-parse HEAD)"
if [[ -n "$EXPECTED_COMMIT" && "$DEPLOY_COMMIT" != "$EXPECTED_COMMIT" ]]; then
  echo "ERROR: expected commit=$EXPECTED_COMMIT actual=$DEPLOY_COMMIT" >&2
  exit 1
fi
echo "dual40_branch=$BRANCH dual40_commit=$DEPLOY_COMMIT"

[[ -x ./.venv/bin/python ]] || python3 -m venv .venv
PY="$REPO_DIR/.venv/bin/python"

# Refuse a profile switch while the existing control plane says real execution is armed.
if curl -fsS --connect-timeout 1 --max-time 2 http://127.0.0.1:8093/health \
  -o /tmp/p3-dual40-predeploy-health.json 2>/dev/null; then
  "$PY" - <<'PY'
import json
from pathlib import Path
j=json.loads(Path('/tmp/p3-dual40-predeploy-health.json').read_text())
if j.get('execution_enabled') or str(j.get('mode') or '').startswith('LIVE'):
    raise SystemExit('ERROR: P3 LIVE is armed. Return to DRY before deploy.')
PY
fi

mkdir -p "$HOME/.direction-engine-env-backups"
chmod 700 "$HOME/.direction-engine-env-backups"
backup="$HOME/.direction-engine-env-backups/.env.p3.pre-dual40.$(date +%Y%m%d-%H%M%S)"
had_env=0
if [[ -f .env.p3 ]]; then
  had_env=1
  cp -f .env.p3 "$backup"
  chmod 600 "$backup"
  echo "DUAL40 rollback backup=$backup"
fi

source_env=".env.p3"
[[ -f "$source_env" ]] || source_env=".env.p3.example"
[[ -f "$source_env" ]] || { echo "ERROR: no P3 env template" >&2; exit 1; }
candidate="$(mktemp ./.env.p3.dual40.XXXXXX)"
chmod 600 "$candidate"

"$PY" - "$source_env" "$candidate" <<'PY'
from pathlib import Path
import sys

source=Path(sys.argv[1])
target=Path(sys.argv[2])
text=source.read_text(encoding='utf-8')

wanted={
    'P3_STRATEGY_MODE':'DUAL40_MAKER_RECOVERY_V1',
    'P3_SCANNER_ENABLED':'false',
    'P3_SCAN_INTERVAL_MS':'250',
    'P3_MAX_QUANTITY_SHARES':'30',
    'P3_MAX_CAPITAL_PER_CYCLE_USDC':'30',

    'P3_DUAL40_PAPER_ENABLED':'true',
    'P3_DUAL40_ASSETS':'BTC,ETH,SOL,XRP',
    'P3_DUAL40_HORIZON':'5m',
    'P3_DUAL40_PRICE':'0.40',
    'P3_DUAL40_LADDER':'5,10,30',
    'P3_DUAL40_MARKET_AGE_SEC':'30',
    'P3_DUAL40_MIN_TTE_SEC':'90',
    'P3_DUAL40_CANCEL_TTE_SEC':'40',
    'P3_DUAL40_LOOKBACK_SEC':'20',
    'P3_DUAL40_CONFIRM_SEC':'5',
    'P3_DUAL40_BALANCED_MID_LOW':'0.44',
    'P3_DUAL40_BALANCED_MID_HIGH':'0.56',
    'P3_DUAL40_MAX_MID_RANGE':'0.10',
    'P3_DUAL40_MAX_NET_DRIFT':'0.04',
    'P3_DUAL40_MAX_ABS_SLOPE_PER_SEC':'0.0030',
    'P3_DUAL40_MAX_ONE_WAY_RATIO':'0.72',
    'P3_DUAL40_MAX_SINGLE_JUMP':'0.06',
    'P3_DUAL40_MAX_COMPLEMENT_RESIDUAL':'0.04',
    'P3_DUAL40_MAX_SPREAD_EACH':'0.10',
    'P3_DUAL40_NEAR_TOUCH_PRICE':'0.41',
    'P3_DUAL40_BOOK_FRESH_MS':'1500',
    'P3_DUAL40_HEARTBEAT_SEC':'5',
    'P3_DUAL40_BALANCE_POLL_SEC':'1',
    'P3_DUAL40_RESOLUTION_POLL_SEC':'10',
    'P3_DUAL40_GAMMA_HOST':'https://gamma-api.polymarket.com',
    'P3_DUAL40_MIN_COLLATERAL_TO_ARM_USDC':'35',
    'P3_DUAL40_FILL_EPSILON':'0.00001',

    # Capability is present, but LiveState still starts DRY and requires the button.
    'P3_LIVE_FEATURE_ENABLED':'true',
    'P3_LIVE_AUTO_EXECUTE_ENABLED':'true',
    'P3_LIVE_REQUIRE_DRY_VALIDATED':'false',
    'P3_WEB_ENABLED':'true',
    'P3_WEB_HOST':'0.0.0.0',
    'P3_WEB_PORT':'8093',
    'P3_WEB_AUTH_REQUIRED':'true',
}

out=[]
seen=set()
for line in text.splitlines():
    stripped=line.strip()
    key=stripped.split('=',1)[0] if '=' in stripped and not stripped.startswith('#') else ''
    if key in wanted:
        out.append(f'{key}={wanted[key]}')
        seen.add(key)
    else:
        out.append(line)
for key,value in wanted.items():
    if key not in seen:
        out.append(f'{key}={value}')
target.write_text('\n'.join(out).rstrip()+'\n',encoding='utf-8')
PY

# Candidate must be valid in a sterile process before touching the active env.
env -i PATH="$PATH" HOME="$HOME" "$PY" - "$candidate" <<'PY'
import sys
from p3_config import DUAL40_MODE, P3Settings
from p3_dual40_capital import required_live_collateral
from p3_dual40_core import Dual40Policy

s=P3Settings(_env_file=(sys.argv[1], '.env.p26', '.env'))
s.validate_research_safety()
policy=Dual40Policy(price=s.dual40_price, ladder=s.dual40_ladder())
assert s.strategy_mode == DUAL40_MODE
assert s.dual40_ladder() == (5.0,10.0,30.0)
assert abs(s.dual40_price-0.40) < 1e-12
assert s.dual40_min_collateral_to_arm_usdc >= 35.0
assert policy.full_ladder_capital == 30.0
assert required_live_collateral(policy=policy, level_index=0, initial_arm_floor_usdc=35.0) == 35.0
assert required_live_collateral(policy=policy, level_index=1, initial_arm_floor_usdc=35.0) == 33.0
assert required_live_collateral(policy=policy, level_index=2, initial_arm_floor_usdc=35.0) == 29.0
assert s.live_feature_enabled is True
assert s.live_auto_execute_enabled is True
assert s.web_auth_required is True
print('DUAL40 CANDIDATE CONFIG PASS')
PY

rollback() {
  local rc=$?
  trap - ERR
  set +e
  echo "ERROR: DUAL40 activation failed; restoring previous .env.p3" >&2
  if [[ "$had_env" == "1" && -f "$backup" ]]; then
    cp -f "$backup" .env.p3
    chmod 600 .env.p3
  else
    rm -f .env.p3
  fi
  if $SUDO systemctl list-unit-files direction-engine-p3-arbitrage.service >/dev/null 2>&1; then
    $SUDO systemctl restart direction-engine-p3-arbitrage.service >/dev/null 2>&1 || true
  fi
  rm -f "$candidate"
  exit "$rc"
}
trap rollback ERR

mv -f "$candidate" .env.p3
chmod 600 .env.p3

args=(--no-pull)
[[ "$RUN_TESTS" == "0" ]] && args+=(--skip-tests)
P3_DEPLOY_BRANCH="$DEPLOY_BRANCH" \
P3_EXPECTED_COMMIT="$DEPLOY_COMMIT" \
  bash deploy_p3.sh "${args[@]}"

"$PY" - <<'PY'
from p3_config import get_p3_settings
from p3_dual40_store import connect_dual40, ladder_state
s=get_p3_settings(); s.validate_research_safety()
assert s.dual40_active
conn=connect_dual40(s.p3_db_path)
paper=ladder_state(conn,'PAPER')
live=ladder_state(conn,'LIVE')
conn.close()
print('strategy=',s.strategy_mode)
print('price=',s.dual40_price)
print('ladder=',s.dual40_ladder())
print('paper_state=',paper)
print('live_state=',live)
PY

trap - ERR
printf '%s\n' \
  "DUAL40 DEPLOY PASS | starts=DRY | branch=$DEPLOY_BRANCH | commit=$DEPLOY_COMMIT | price=40c+40c POST_ONLY_GTC | ladder=5->10->30 | hard_stop_after_30=true | entry=balanced_stable_two_way | one_global_market=true | paper_fill=ask<=40c | near_touch_41=diagnostic | initial_live_arm_floor=\$35 | remaining_path_floor=35->33->29"
