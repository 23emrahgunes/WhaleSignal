#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: ./deploy_p3.sh [--no-pull] [--skip-tests]

Deploy P3 structural arbitrage. The process always starts DRY. Optional guarded
LIVE v2 support is installed only when P3_LIVE_FEATURE_ENABLED=true; LIVE also
requires authenticated 8093, preflight, equal-share depth/edge gates and single-leg
risk gates before any real order can be submitted.
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

fail() {
  echo "ERROR: $*" >&2
  $SUDO systemctl --no-pager --full status direction-engine-p3-arbitrage.service 2>/dev/null >&2 || true
  tail -n 120 logs/p3-arbitrage.log 2>/dev/null >&2 || true
  exit 1
}

p25_alive() {
  pgrep -f 'p25_main\.py' >/dev/null 2>&1
}

if [[ "$DO_PULL" == "1" ]]; then
  [[ -z "$(git status --porcelain --untracked-files=all)" ]] || fail "working tree dirty"
  git fetch origin direction-engine
  git checkout direction-engine
  git pull --ff-only origin direction-engine
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[[ "$BRANCH" == "direction-engine" ]] || fail "expected direction-engine branch"
COMMIT="$(git rev-parse HEAD)"
echo "branch=$BRANCH commit=$COMMIT"

p25_alive || fail "P2.5 process is not running"
for service in direction-engine-p26-oracle.service direction-engine-p26-dataset.service direction-engine-p26-book.service; do
  $SUDO systemctl is-active --quiet "$service" || fail "required P2.6 service inactive: $service"
done

[[ -x ./.venv/bin/python ]] || python3 -m venv .venv
PY="$REPO_DIR/.venv/bin/python"
PIP="$REPO_DIR/.venv/bin/pip"
mkdir -p data logs reports/p3
if [[ ! -f .env.p3 ]]; then
  cp .env.p3.example .env.p3
fi
chmod 600 .env.p3

"$PIP" install -q -r requirements.txt
LIVE_FEATURE="$("$PY" - <<'PY'
from p3_config import get_p3_settings
s=get_p3_settings(); s.validate_research_safety(); s.ensure_directories()
print("1" if s.live_feature_enabled else "0")
PY
)"
if [[ "$LIVE_FEATURE" == "1" ]]; then
  [[ -f requirements-live.txt ]] || fail "requirements-live.txt missing"
  "$PIP" install -q -r requirements-live.txt
fi

"$PY" - <<'PY'
from p3_config import get_p3_settings
from p3_live_ledger import ensure_live_ledger_schema
from p3_schema import connect_p3, ensure_p3_schema

s=get_p3_settings(); s.validate_research_safety(); s.ensure_directories()
print("p26_db=", s.p26_db_path)
print("p3_db=", s.p3_db_path)
print("web=", f"{s.web_host}:{s.web_port}")
print("web_auth_required=", s.web_auth_required)
print("web_cookie_secure=", s.web_cookie_secure)
print("live_feature=", s.live_feature_enabled)
print("live_auto_execute=", s.live_auto_execute_enabled)
print("live_control=authenticated_web_8093")
print("live_sizing=EQUAL_SHARES_FRESH_DEPTH")
print("live_target_shares_each_leg=", s.live_target_quantity_shares)
print("live_hard_max_shares_each_leg=", s.live_max_quantity_shares)
print("legacy_dollar_scaler_enabled=", False)
print("max_single_leg_notional_usdc=", s.live_max_single_leg_notional_usdc)
print("max_projected_unwind_loss_usdc=", s.live_max_projected_unwind_loss_usdc)
print("emergency_unwind_loss_usdc=", s.live_emergency_unwind_loss_usdc)
print("halt_after_one_leg=", s.live_halt_after_one_leg)
print("rolling_24h_gross_loss_limit_usdc=", s.live_rolling_24h_gross_loss_limit_usdc)
assert s.p26_db_path != s.p3_db_path
if s.live_feature_enabled:
    assert s.web_auth_required
    assert s.live_target_quantity_shares <= s.live_max_quantity_shares
conn=connect_p3(s.p3_db_path); ensure_p3_schema(conn); ensure_live_ledger_schema(conn); conn.close()
PY

"$PY" -m py_compile p3_*.py
"$PY" -m compileall -q .
bash -n deploy_p3.sh scripts/status_p3.sh scripts/stop_p3.sh scripts/smoke_p3.sh
if [[ "$RUN_TESTS" == "1" ]]; then
  PHASE=P1 MODEL_TRAINING_ENABLED=false CALIBRATION_ENABLED=false PAPER_TRADING_ENABLED=false "$PY" -m pytest -q
fi

RUN_USER="${P3_SERVICE_USER:-$(stat -c '%U' "$REPO_DIR")}"
RUN_GROUP="${P3_SERVICE_GROUP:-$(id -gn "$RUN_USER")}"
$SUDO chown -R "$RUN_USER:$RUN_GROUP" data logs reports/p3
UNIT=/etc/systemd/system/direction-engine-p3-arbitrage.service
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
cat > "$TMP" <<EOF
[Unit]
Description=Direction Engine P3 Structural Arbitrage (DRY default / guarded LIVE v2)
After=network-online.target direction-engine-p26-book.service
Wants=network-online.target direction-engine-p26-book.service

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$REPO_DIR
EnvironmentFile=-$REPO_DIR/.env.p26
EnvironmentFile=-$REPO_DIR/.env.p3
Environment=PYTHONUNBUFFERED=1
ExecStart=$PY $REPO_DIR/p3_daemon.py
Restart=always
RestartSec=3
TimeoutStopSec=30
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=$REPO_DIR/data $REPO_DIR/logs $REPO_DIR/reports
StandardOutput=append:$REPO_DIR/logs/p3-arbitrage.log
StandardError=append:$REPO_DIR/logs/p3-arbitrage.log

[Install]
WantedBy=multi-user.target
EOF
$SUDO install -m 0644 "$TMP" "$UNIT"
$SUDO systemctl daemon-reload
$SUDO systemctl enable direction-engine-p3-arbitrage.service
printf '\n=== P3 DEPLOY %s commit=%s ===\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$COMMIT" >> logs/p3-arbitrage.log
$SUDO systemctl restart direction-engine-p3-arbitrage.service
sleep 2
$SUDO systemctl is-active --quiet direction-engine-p3-arbitrage.service || fail "P3 service inactive"

bash scripts/smoke_p3.sh
p25_alive || fail "P2.5 process stopped during P3 deploy"

echo "P3 ARBITRAGE DEPLOY PASS | starts=DRY | sizing=equal_shares | control=authenticated_8093 | live_feature=$LIVE_FEATURE | p25=process_alive"
