#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: ./deploy_p3.sh [--no-pull] [--skip-tests]

Deploy the isolated P3 structural-arbitrage SHADOW lab. P3 reads P2.6 public
book/fee data and writes data/p3_arbitrage.sqlite. It never stops/replaces P2.5,
never changes P2.6 runtime flags, and contains no signing/order submission.
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

wait_http_200() {
  local name="$1"
  local url="$2"
  local output="$3"
  local attempts="${4:-30}"
  local code="000"
  for _ in $(seq 1 "$attempts"); do
    code="$(curl -sS --connect-timeout 1 --max-time 2 -o "$output" -w '%{http_code}' "$url" || true)"
    if [[ "$code" == "200" ]]; then
      echo "$code"
      return 0
    fi
    sleep 1
  done
  echo "health_gate_failed name=$name code=$code url=$url" >&2
  return 1
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

# P2.5 /health includes engine.snapshot(); under transient CPU/DB pressure a single
# 5s request can time out even though the service is healthy. Require eventual 200.
P25="$(wait_http_200 p25-pre http://127.0.0.1:8091/health /tmp/p3-p25-health.json 30)" || fail "P2.5 health is not HTTP 200"
for service in direction-engine-p26-oracle.service direction-engine-p26-dataset.service direction-engine-p26-book.service; do
  $SUDO systemctl is-active --quiet "$service" || fail "required P2.6 service inactive: $service"
done

[[ -x ./.venv/bin/python ]] || python3 -m venv .venv
PY="$REPO_DIR/.venv/bin/python"
PIP="$REPO_DIR/.venv/bin/pip"
mkdir -p data logs reports/p3
if [[ ! -f .env.p3 ]]; then
  cp .env.p3.example .env.p3
  chmod 600 .env.p3
fi

"$PIP" install -q -r requirements.txt
"$PY" - <<'PY'
from p3_config import get_p3_settings
s=get_p3_settings(); s.validate_research_safety(); s.ensure_directories()
print("p26_db=", s.p26_db_path)
print("p3_db=", s.p3_db_path)
assert s.p26_db_path != s.p3_db_path
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
Description=Direction Engine P3 Structural Arbitrage Lab (SHADOW ONLY)
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
P25_AFTER="$(wait_http_200 p25-post http://127.0.0.1:8091/health /tmp/p3-p25-health-after.json 30)" || fail "P2.5 health failed after P3 deploy"

echo "P3 ARBITRAGE LAB DEPLOY PASS | P2.5=200 | SHADOW=true | execution=false"
