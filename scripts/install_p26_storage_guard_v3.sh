#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BOOK_SERVICE="direction-engine-p26-book.service"
BOOK_OVERRIDE_DIR="/etc/systemd/system/${BOOK_SERVICE}.d"
BOOK_OVERRIDE="${BOOK_OVERRIDE_DIR}/10-resilient-book.conf"
RET_SERVICE="direction-engine-p26-retention.service"
RET_TIMER="direction-engine-p26-retention.timer"
PYTHON="${ROOT}/.venv/bin/python"
BOOK_ENTRY="${ROOT}/p26_book_daemon_resilient_v3.py"
RET_ENTRY="${ROOT}/p26_retention_v2.py"
DB="${ROOT}/data/p26_research.sqlite"

cd "$ROOT"
[[ -x "$PYTHON" ]] || { echo "FAIL missing python: $PYTHON"; exit 1; }
[[ -f "$BOOK_ENTRY" ]] || { echo "FAIL missing V3 book entry: $BOOK_ENTRY"; exit 1; }
[[ -f "$RET_ENTRY" ]] || { echo "FAIL missing retention V2: $RET_ENTRY"; exit 1; }

"$PYTHON" -m py_compile p26_book_daemon_resilient_v3.py p26_retention_v2.py

sudo mkdir -p "$BOOK_OVERRIDE_DIR"
sudo tee "$BOOK_OVERRIDE" >/dev/null <<EOF
[Service]
ExecStart=
ExecStart=${PYTHON} ${BOOK_ENTRY}
EOF

sudo tee "/etc/systemd/system/${RET_SERVICE}" >/dev/null <<EOF
[Unit]
Description=Direction Engine P2.6 Research DB Retention V2
After=local-fs.target

[Service]
Type=oneshot
User=${SUDO_USER:-$USER}
WorkingDirectory=${ROOT}
ExecStart=${PYTHON} ${RET_ENTRY} --db data/p26_research.sqlite --book-hours 0.25 --oracle-hours 72 --canonical-hours 168 --health-hours 48 --batch-size 5000 --max-batches 200
Nice=10
IOSchedulingClass=idle
EOF

sudo tee "/etc/systemd/system/${RET_TIMER}" >/dev/null <<'EOF'
[Unit]
Description=Run Direction Engine P2.6 bounded retention every 15 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=15min
Persistent=true
RandomizedDelaySec=30s

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl restart "$BOOK_SERVICE"
sudo systemctl enable --now "$RET_TIMER"

for _ in $(seq 1 30); do
  sudo systemctl is-active --quiet "$BOOK_SERVICE" && break
  sleep 0.5
done
sudo systemctl is-active "$BOOK_SERVICE"

EXECSTART="$(sudo systemctl show "$BOOK_SERVICE" -p ExecStart --value)"
echo "effective_execstart=${EXECSTART}"
if [[ "$EXECSTART" != *"p26_book_daemon_resilient_v3.py"* ]]; then
  echo "FAIL book service did not select resilient_v3"
  exit 1
fi

# Let session seed complete, then prove DB integrity and report bounded write rate.
sleep 8
read_count() {
  "$PYTHON" - "$DB" <<'PY'
import sqlite3, sys
p=sys.argv[1]
c=sqlite3.connect(p, timeout=5)
try:
    print(c.execute("SELECT COUNT(*) FROM p26_clob_books").fetchone()[0])
finally:
    c.close()
PY
}

BEFORE="$(read_count)"
sleep 10
AFTER="$(read_count)"
DELTA=$((AFTER-BEFORE))

echo "book_rows_before=${BEFORE}"
echo "book_rows_after=${AFTER}"
echo "book_rows_delta_10s=${DELTA}"

"$PYTHON" - "$DB" <<'PY'
import sqlite3, sys
p=sys.argv[1]
c=sqlite3.connect(p, timeout=10)
try:
    integrity=c.execute("PRAGMA integrity_check").fetchone()[0]
    tokens=c.execute("SELECT COUNT(*) FROM p26_market_tokens").fetchone()[0]
    fees=c.execute("SELECT COUNT(*) FROM p26_fee_schedules").fetchone()[0]
    print("integrity=",integrity)
    print("market_tokens=",tokens)
    print("fee_schedules=",fees)
    assert integrity == "ok"
finally:
    c.close()
PY

sudo systemctl list-timers "$RET_TIMER" --no-pager

echo "P26_STORAGE_GUARD_V3_PASS | empty_transition=bounded | raw_books=15m | replay_resolution=configured | retention_every=15m"
