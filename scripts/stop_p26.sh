#!/usr/bin/env bash
set -Eeuo pipefail

REMOVE_UNITS=0
case "${1:-}" in
  "") ;;
  --remove-units) REMOVE_UNITS=1 ;;
  --help|-h)
    cat <<'EOF'
Usage: scripts/stop_p26.sh [--remove-units]

Stops and disables only the P2.6 oracle/dataset sidecars. It never stops P2.5,
never restores a database, never deletes research data and never changes port 8091
firewall rules. Use --remove-units to remove the installed systemd unit files after
stopping the services.
EOF
    exit 0
    ;;
  *) echo "ERROR: unknown option: ${1:-}" >&2; exit 2 ;;
esac

if [[ "$EUID" -eq 0 ]]; then
  SUDO=""
else
  command -v sudo >/dev/null 2>&1 || { echo "ERROR: sudo required" >&2; exit 1; }
  SUDO="sudo"
fi

for service in direction-engine-p26-dataset.service direction-engine-p26-oracle.service; do
  $SUDO systemctl disable --now "$service" 2>/dev/null || true
done

if [[ "$REMOVE_UNITS" == "1" ]]; then
  $SUDO rm -f \
    /etc/systemd/system/direction-engine-p26-oracle.service \
    /etc/systemd/system/direction-engine-p26-dataset.service
  $SUDO systemctl daemon-reload
fi

cat <<EOF
P2.6 SIDECARS STOPPED
- P2.5 was not stopped or modified
- P2.5 SQLite was not restored or deleted
- data/p26_research.sqlite was preserved
- port 8091 rules were not changed
- unit_files_removed=$REMOVE_UNITS
EOF
