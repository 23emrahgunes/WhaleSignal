#!/bin/bash
set -e

echo "=== Pulling Code Updates & Re-deploying pm-edge ==="

# 1. Fetch latest changes
echo "Pulling latest git revision..."
git pull

# 2. Re-compile and deploy code
./deploy.sh

# 3. Restart Systemd Service daemon
if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files | grep -q pm-edge.service; then
    echo "Restarting pm-edge systemd service..."
    sudo systemctl restart pm-edge
    echo "Service successfully restarted."
else
    echo "Systemd service pm-edge is not installed. Restart the process manually."
fi

echo "=== System update and restart complete! ==="
