#!/usr/bin/env bash
set -Eeuo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
run_user="${SUDO_USER:-$USER}"
python_bin="$repo_dir/.venv/bin/python"
retention_py="$repo_dir/p26_retention.py"

if [[ ! -x "$python_bin" ]]; then
  echo "ERROR: $python_bin bulunamadi" >&2
  exit 1
fi

sudo tee /etc/systemd/system/direction-engine-p26-retention.service >/dev/null <<EOF
[Unit]
Description=Direction Engine P2.6 Research DB Retention
After=local-fs.target

[Service]
Type=oneshot
User=$run_user
WorkingDirectory=$repo_dir
ExecStart=$python_bin $retention_py --db data/p26_research.sqlite --book-hours 24 --oracle-hours 72 --canonical-hours 168 --health-hours 48 --batch-size 5000 --max-batches 200
Nice=10
IOSchedulingClass=idle
EOF

sudo tee /etc/systemd/system/direction-engine-p26-retention.timer >/dev/null <<'EOF'
[Unit]
Description=Run Direction Engine P2.6 retention every 6 hours

[Timer]
OnBootSec=15min
OnUnitActiveSec=6h
Persistent=true
RandomizedDelaySec=5min

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now direction-engine-p26-retention.timer

echo "=== P26 RETENTION TIMER ==="
sudo systemctl status direction-engine-p26-retention.timer --no-pager -l
sudo systemctl list-timers direction-engine-p26-retention.timer --no-pager

echo "P26_RETENTION_INSTALL_PASS | books=24h | oracle=72h | canonical=168h | health=48h | every=6h"
