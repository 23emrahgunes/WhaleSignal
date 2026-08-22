#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE="direction-engine-p26-book.service"
OVERRIDE_DIR="/etc/systemd/system/${SERVICE}.d"
OVERRIDE_FILE="${OVERRIDE_DIR}/10-resilient-book.conf"
WATCHDOG_SERVICE="direction-engine-p26-book-watchdog.service"
WATCHDOG_TIMER="direction-engine-p26-book-watchdog.timer"
WATCHDOG_SERVICE_FILE="/etc/systemd/system/${WATCHDOG_SERVICE}"
WATCHDOG_TIMER_FILE="/etc/systemd/system/${WATCHDOG_TIMER}"
PYTHON="${ROOT}/.venv/bin/python"
ENTRY="${ROOT}/p26_book_daemon_resilient_v2.py"
WATCHDOG="${ROOT}/scripts/p26_book_watchdog.py"

cd "$ROOT"

[[ -x "$PYTHON" ]] || { echo "FAIL missing python: $PYTHON"; exit 1; }
[[ -f "$ENTRY" ]] || { echo "FAIL missing entrypoint: $ENTRY"; exit 1; }
[[ -f "$WATCHDOG" ]] || { echo "FAIL missing watchdog: $WATCHDOG"; exit 1; }

"$PYTHON" -m py_compile p26_book_daemon_resilient.py p26_book_daemon_resilient_v2.py scripts/p26_book_watchdog.py

sudo mkdir -p "$OVERRIDE_DIR"
sudo tee "$OVERRIDE_FILE" >/dev/null <<EOF
[Service]
ExecStart=
ExecStart=${PYTHON} ${ENTRY}
EOF

sudo tee "$WATCHDOG_SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=Direction Engine P2.6 Book Heartbeat Watchdog
After=${SERVICE}

[Service]
Type=oneshot
WorkingDirectory=${ROOT}
ExecStart=${PYTHON} ${WATCHDOG} --db ${ROOT}/data/p26_research.sqlite --service ${SERVICE}
EOF

sudo tee "$WATCHDOG_TIMER_FILE" >/dev/null <<EOF
[Unit]
Description=Run Direction Engine P2.6 Book Heartbeat Watchdog

[Timer]
OnBootSec=20s
OnUnitActiveSec=10s
AccuracySec=1s
Unit=${WATCHDOG_SERVICE}

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl restart "$SERVICE"
sudo systemctl enable --now "$WATCHDOG_TIMER"

for i in $(seq 1 30); do
  if sudo systemctl is-active --quiet "$SERVICE"; then
    break
  fi
  sleep 0.5
done

sudo systemctl is-active "$SERVICE"
sudo systemctl is-active "$WATCHDOG_TIMER"

EXECSTART="$(sudo systemctl show "$SERVICE" -p ExecStart --value)"
echo "effective_execstart=${EXECSTART}"
if [[ "$EXECSTART" != *"p26_book_daemon_resilient_v2.py"* ]]; then
  echo "FAIL systemd override did not select resilient_v2 entrypoint"
  exit 1
fi

PASS=0
for i in $(seq 1 20); do
  if "$PYTHON" "$WATCHDOG" --db "${ROOT}/data/p26_research.sqlite" --no-restart >/tmp/p26-book-watchdog-check.txt 2>&1; then
    cat /tmp/p26-book-watchdog-check.txt
    PASS=1
    break
  fi
  cat /tmp/p26-book-watchdog-check.txt || true
  sleep 1
 done

if [[ "$PASS" != "1" ]]; then
  echo "FAIL book collector did not become healthy"
  sudo systemctl status "$SERVICE" --no-pager --full || true
  exit 1
fi

echo "P26_BOOK_UPTIME_V2_PASS"
echo "override=${OVERRIDE_FILE}"
echo "watchdog_timer=${WATCHDOG_TIMER_FILE}"
echo "rollback: sudo systemctl disable --now '${WATCHDOG_TIMER}' || true; sudo rm -f '${WATCHDOG_SERVICE_FILE}' '${WATCHDOG_TIMER_FILE}' '${OVERRIDE_FILE}'; sudo systemctl daemon-reload; sudo systemctl restart '${SERVICE}'"
