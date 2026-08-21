#!/usr/bin/env bash
set -Eeuo pipefail
SUDO=""; [[ "$EUID" -eq 0 ]] || SUDO=sudo
$SUDO systemctl stop direction-engine-p3-arbitrage.service 2>/dev/null || true
$SUDO systemctl disable direction-engine-p3-arbitrage.service 2>/dev/null || true
$SUDO rm -f /etc/systemd/system/direction-engine-p3-arbitrage.service
$SUDO systemctl daemon-reload
$SUDO systemctl reset-failed 2>/dev/null || true
echo "P3 stopped. P2.5/P2.6 runtimes and all databases were preserved."
