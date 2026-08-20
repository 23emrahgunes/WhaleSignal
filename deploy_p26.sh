#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: ./deploy_p26.sh [options]

Deploy the isolated P2.6 oracle/dataset sidecars without replacing or stopping
P2.5. The script freezes a P2.5 baseline, validates the complete test suite,
installs dynamic systemd units, restarts the sidecars and verifies persisted RTDS
oracle ticks.

Options:
  --no-pull       Do not fetch/pull origin/direction-engine.
  --skip-tests    Skip the full pytest suite (syntax checks still run).
  --skip-freeze   Skip the P2.5 baseline freeze (not recommended).
  --help          Show this help.

Network hardening is intentionally NOT applied by this script. Run the dedicated
hardening script only after an alternative access path/AWS Security Group rule is
verified.
EOF
}

DO_PULL=1
RUN_TESTS=1
RUN_FREEZE=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-pull) DO_PULL=0 ;;
    --skip-tests) RUN_TESTS=0 ;;
    --skip-freeze) RUN_FREEZE=0 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "ERROR: unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

if [[ "$REPO_DIR" =~ [[:space:]] ]]; then
  echo "ERROR: repository path may not contain whitespace: $REPO_DIR" >&2
  exit 1
fi

if [[ ! -d .git ]]; then
  echo "ERROR: not a Git working tree: $REPO_DIR" >&2
  exit 1
fi

if [[ "$EUID" -eq 0 ]]; then
  SUDO=""
else
  if ! command -v sudo >/dev/null 2>&1; then
    echo "ERROR: sudo is required to install systemd units" >&2
    exit 1
  fi
  SUDO="sudo"
fi

fail() {
  echo "ERROR: $*" >&2
  echo "--- P26 oracle service ---" >&2
  $SUDO systemctl --no-pager --full status direction-engine-p26-oracle.service 2>/dev/null >&2 || true
  echo "--- P26 oracle log ---" >&2
  $SUDO tail -n 120 "$REPO_DIR/logs/p26-oracle.log" 2>/dev/null >&2 || true
  echo "--- P26 dataset service ---" >&2
  $SUDO systemctl --no-pager --full status direction-engine-p26-dataset.service 2>/dev/null >&2 || true
  echo "--- P26 dataset log ---" >&2
  $SUDO tail -n 120 "$REPO_DIR/logs/p26-dataset.log" 2>/dev/null >&2 || true
  exit 1
}

http_code() {
  curl --connect-timeout 4 --max-time 12 -sS -o "$2" -w '%{http_code}' "$1" || true
}

echo "=== P2.6 AWS DEPLOY PREFLIGHT ==="
if [[ "$DO_PULL" == "1" ]]; then
  dirty="$(git status --porcelain --untracked-files=all)"
  if [[ -n "$dirty" ]]; then
    echo "$dirty" >&2
    fail "working tree is dirty; preserve/stash local changes before deployment"
  fi
  git fetch origin direction-engine
  git checkout direction-engine
  git pull --ff-only origin direction-engine
fi

COMMIT="$(git rev-parse HEAD)"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
echo "branch=$BRANCH"
echo "commit=$COMMIT"
if [[ "$BRANCH" != "direction-engine" ]]; then
  fail "expected direction-engine branch, got $BRANCH"
fi

P25_HEALTH_CODE="$(http_code http://127.0.0.1:8091/health /tmp/p26-p25-health.json)"
P25_STATE_CODE="$(http_code http://127.0.0.1:8091/api/state /tmp/p26-p25-state.json)"
P25_PAPER_CODE="$(http_code http://127.0.0.1:8091/api/paper-summary /tmp/p26-p25-paper.json)"
echo "p25_health_http=$P25_HEALTH_CODE p25_state_http=$P25_STATE_CODE p25_paper_http=$P25_PAPER_CODE"
[[ "$P25_HEALTH_CODE" == "200" ]] || fail "P2.5 /health is not HTTP 200"
[[ "$P25_STATE_CODE" == "200" ]] || fail "P2.5 /api/state is not HTTP 200"
[[ "$P25_PAPER_CODE" == "200" ]] || fail "P2.5 /api/paper-summary is not HTTP 200"

if [[ ! -x ./.venv/bin/python ]]; then
  python3 -m venv .venv
fi
PY="$REPO_DIR/.venv/bin/python"
PIP="$REPO_DIR/.venv/bin/pip"

mkdir -p data logs models/p26 reports/p26 data/backups
if [[ ! -f .env.p26 ]]; then
  cp .env.p26.example .env.p26
  chmod 600 .env.p26
  echo "created .env.p26 from tracked example"
fi

# Ensure the runtime configuration stays explicitly separated from P2.5.
"$PY" - <<'PY'
from p26_config import get_p26_settings
settings = get_p26_settings()
settings.validate_research_safety()
settings.ensure_directories()
print("p25_db=", settings.p25_db_path)
print("p26_db=", settings.p26_db_path)
assert settings.p25_db_path != settings.p26_db_path
PY

echo "=== DEPENDENCIES ==="
"$PY" -m pip install -q --upgrade pip
"$PIP" install -q -r requirements.txt

echo "=== SYNTAX / STATIC SAFETY ==="
"$PY" -m py_compile ./*.py reference/*.py
"$PY" -m compileall -q .
bash -n deploy_p26.sh scripts/harden_port_8091.sh scripts/rollback_port_8091.sh scripts/status_p26.sh scripts/stop_p26.sh

if [[ "$RUN_TESTS" == "1" ]]; then
  echo "=== COMPLETE REGRESSION SUITE ==="
  PHASE=P1 \
  MODEL_TRAINING_ENABLED=false \
  CALIBRATION_ENABLED=false \
  PAPER_TRADING_ENABLED=false \
  "$PY" -m pytest -q
else
  echo "WARNING: full pytest suite skipped by explicit request"
fi

MANIFEST="SKIPPED"
if [[ "$RUN_FREEZE" == "1" ]]; then
  echo "=== P2.5 BASELINE FREEZE ==="
  MANIFEST="$("$PY" p26_baseline_freeze.py freeze)"
  [[ -f "$MANIFEST" ]] || fail "baseline manifest was not created: $MANIFEST"
  "$PY" p26_baseline_freeze.py verify "$MANIFEST"
  echo "baseline_manifest=$MANIFEST"
else
  echo "WARNING: P2.5 baseline freeze skipped by explicit request"
fi

# Use the repository owner by default. This supports both /home/ubuntu and /root
# deployments and avoids hard-coded service users in the template files.
RUN_USER="${P26_SERVICE_USER:-$(stat -c '%U' "$REPO_DIR")}"
if ! id "$RUN_USER" >/dev/null 2>&1; then
  fail "service user does not exist: $RUN_USER"
fi
RUN_GROUP="${P26_SERVICE_GROUP:-$(id -gn "$RUN_USER")}"

$SUDO chown -R "$RUN_USER:$RUN_GROUP" data logs models/p26 reports/p26

ORACLE_UNIT=/etc/systemd/system/direction-engine-p26-oracle.service
DATASET_UNIT=/etc/systemd/system/direction-engine-p26-dataset.service

ORACLE_TEMP="$(mktemp)"
DATASET_TEMP="$(mktemp)"
trap 'rm -f "$ORACLE_TEMP" "$DATASET_TEMP"' EXIT

cat > "$ORACLE_TEMP" <<EOF
[Unit]
Description=Direction Engine P2.6 Oracle Persistence Sidecar
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$REPO_DIR
EnvironmentFile=-$REPO_DIR/.env.p26
Environment=PYTHONUNBUFFERED=1
ExecStart=$PY $REPO_DIR/p26_oracle_daemon.py
Restart=always
RestartSec=3
TimeoutStopSec=30
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=$REPO_DIR/data $REPO_DIR/logs $REPO_DIR/models $REPO_DIR/reports
StandardOutput=append:$REPO_DIR/logs/p26-oracle.log
StandardError=append:$REPO_DIR/logs/p26-oracle.log

[Install]
WantedBy=multi-user.target
EOF

cat > "$DATASET_TEMP" <<EOF
[Unit]
Description=Direction Engine P2.6 Canonical Dataset Sidecar
After=network-online.target direction-engine-p26-oracle.service
Wants=network-online.target direction-engine-p26-oracle.service

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$REPO_DIR
EnvironmentFile=-$REPO_DIR/.env.p26
Environment=PYTHONUNBUFFERED=1
ExecStart=$PY $REPO_DIR/p26_dataset_daemon.py --interval-sec 10
Restart=always
RestartSec=5
TimeoutStopSec=30
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=$REPO_DIR/data $REPO_DIR/logs $REPO_DIR/models $REPO_DIR/reports
StandardOutput=append:$REPO_DIR/logs/p26-dataset.log
StandardError=append:$REPO_DIR/logs/p26-dataset.log

[Install]
WantedBy=multi-user.target
EOF

$SUDO install -m 0644 "$ORACLE_TEMP" "$ORACLE_UNIT"
$SUDO install -m 0644 "$DATASET_TEMP" "$DATASET_UNIT"
$SUDO systemctl daemon-reload
$SUDO systemctl enable direction-engine-p26-oracle.service
$SUDO systemctl enable direction-engine-p26-dataset.service

# Explicit restart is required on redeploy. `enable --now` leaves an already
# active process running old Python bytecode after git pull.
printf '\n=== P26 DEPLOY %s commit=%s ===\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$COMMIT" >> "$REPO_DIR/logs/p26-oracle.log"
printf '\n=== P26 DEPLOY %s commit=%s ===\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$COMMIT" >> "$REPO_DIR/logs/p26-dataset.log"
$SUDO systemctl restart direction-engine-p26-oracle.service
sleep 1
$SUDO systemctl restart direction-engine-p26-dataset.service

sleep 5
$SUDO systemctl is-active --quiet direction-engine-p26-oracle.service || fail "oracle sidecar is not active"
$SUDO systemctl is-active --quiet direction-engine-p26-dataset.service || fail "dataset sidecar is not active"

# Official RTDS traffic can be bursty. Wait up to 120 seconds for a persisted tick.
echo "=== WAIT FOR PERSISTED CHAINLINK RTDS TICK ==="
TICK_COUNT=0
for attempt in $(seq 1 40); do
  TICK_COUNT="$("$PY" - <<'PY'
from p26_config import get_p26_settings
from p26_schema import connect_p26, ensure_p26_schema
s = get_p26_settings()
conn = connect_p26(s.p26_db_path)
ensure_p26_schema(conn)
print(conn.execute("SELECT COUNT(*) FROM p26_oracle_ticks").fetchone()[0])
conn.close()
PY
)"
  echo "attempt=$attempt oracle_ticks=$TICK_COUNT"
  if [[ "$TICK_COUNT" -gt 0 ]]; then
    break
  fi
  if ! $SUDO systemctl is-active --quiet direction-engine-p26-oracle.service; then
    fail "oracle sidecar stopped while waiting for an RTDS tick"
  fi
  sleep 3
done
[[ "$TICK_COUNT" -gt 0 ]] || fail "no RTDS oracle tick persisted within 120 seconds"

P25_HEALTH_AFTER="$(http_code http://127.0.0.1:8091/health /tmp/p26-p25-health-after.json)"
[[ "$P25_HEALTH_AFTER" == "200" ]] || fail "P2.5 health failed after P2.6 deployment"

STATUS_JSON="$("$PY" - <<'PY'
import json, time
from p26_config import get_p26_settings
from p26_schema import connect_p26, ensure_p26_schema, integrity_check
s = get_p26_settings()
conn = connect_p26(s.p26_db_path)
ensure_p26_schema(conn)
counts = {
    "oracle_ticks": conn.execute("SELECT COUNT(*) FROM p26_oracle_ticks").fetchone()[0],
    "canonical_rows": conn.execute("SELECT COUNT(*) FROM p26_canonical_rows").fetchone()[0],
    "eligible_rows": conn.execute("SELECT COUNT(*) FROM p26_canonical_rows WHERE training_eligible=1").fetchone()[0],
    "labels": conn.execute("SELECT COUNT(*) FROM p26_labels WHERE official_label IS NOT NULL").fetchone()[0],
}
latest = conn.execute("SELECT MAX(source_ts_ms) FROM p26_oracle_ticks").fetchone()[0]
result = {
    "integrity": integrity_check(conn),
    **counts,
    "latest_oracle_age_ms": (int(time.time()*1000)-int(latest)) if latest else None,
    "execution_enabled": False,
    "private_key_loaded": False,
    "order_submission_enabled": False,
}
conn.close()
print(json.dumps(result, sort_keys=True))
PY
)"
echo "P26_RUNTIME_STATUS=$STATUS_JSON"

if command -v iptables >/dev/null 2>&1; then
  echo "=== PORT HARDENING DRY-RUN ONLY ==="
  $SUDO env P26_AUTHORIZED_CIDR="${P26_AUTHORIZED_CIDR:-}" \
    "$REPO_DIR/scripts/harden_port_8091.sh" --dry-run || true
else
  echo "NOTICE: iptables unavailable; use AWS Security Group as the primary perimeter control"
fi

cat <<EOF

P2.6 SIDECAR DEPLOY PASS
- commit: $COMMIT
- service_user: $RUN_USER
- P2.5 remained healthy: HTTP $P25_HEALTH_AFTER
- P2.6 oracle ticks: $TICK_COUNT
- baseline manifest: $MANIFEST
- P2.6 database: $REPO_DIR/data/p26_research.sqlite
- execution/signing/private-key/order submission: DISABLED

Status command:
  $REPO_DIR/scripts/status_p26.sh

Rollback only the P2.6 sidecars:
  $REPO_DIR/scripts/stop_p26.sh

Network hardening was NOT applied. First restrict AWS Security Group to your
trusted IP or establish Nginx/VPN/SSH-tunnel access; then use the dedicated
hardening script with its watchdog.
EOF
