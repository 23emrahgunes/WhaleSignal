#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE="direction-engine-p26-book.service"
OVERRIDE_DIR="/etc/systemd/system/${SERVICE}.d"
OVERRIDE_FILE="${OVERRIDE_DIR}/10-resilient-book.conf"
PYTHON="${ROOT}/.venv/bin/python"
ENTRY="${ROOT}/p26_book_daemon_resilient.py"

cd "$ROOT"

[[ -x "$PYTHON" ]] || { echo "FAIL missing python: $PYTHON"; exit 1; }
[[ -f "$ENTRY" ]] || { echo "FAIL missing entrypoint: $ENTRY"; exit 1; }

"$PYTHON" -m py_compile p26_book_daemon_resilient.py

sudo mkdir -p "$OVERRIDE_DIR"
sudo tee "$OVERRIDE_FILE" >/dev/null <<EOF
[Service]
ExecStart=
ExecStart=${PYTHON} ${ENTRY}
EOF

sudo systemctl daemon-reload
sudo systemctl restart "$SERVICE"

for i in $(seq 1 20); do
  if sudo systemctl is-active --quiet "$SERVICE"; then
    break
  fi
  sleep 0.5
done

sudo systemctl is-active "$SERVICE"

sleep 3

"$PYTHON" - <<'PY'
import json
import sqlite3
import time

conn = sqlite3.connect("data/p26_research.sqlite")
row = conn.execute(
    "SELECT value FROM p26_meta WHERE key='book_collector_health_json'"
).fetchone()
conn.close()
if row is None:
    raise SystemExit("FAIL no book collector health")
health = json.loads(row[0])
age = int(time.time() * 1000) - int(health.get("heartbeat_ts_ms") or 0)
print("book_health=", health)
print("heartbeat_age_ms=", age)
if not health.get("connected"):
    raise SystemExit("FAIL book socket not connected")
if age > 5000:
    raise SystemExit("FAIL book heartbeat stale")
print("P26_BOOK_UPTIME_OVERRIDE_PASS")
PY

echo "override=${OVERRIDE_FILE}"
echo "rollback: sudo rm -f '${OVERRIDE_FILE}' && sudo systemctl daemon-reload && sudo systemctl restart '${SERVICE}'"
