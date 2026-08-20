#!/usr/bin/env bash
set -Eeuo pipefail

CHAIN="DIRECTION_ENGINE_8091"
PORT="${P26_PORT:-8091}"
STATE_DIR="${P26_HARDEN_STATE_DIR:-/var/lib/direction-engine-p26}"
CONFIRM_FILE="$STATE_DIR/port-${PORT}.confirmed"
WATCHDOG_PID_FILE="$STATE_DIR/port-${PORT}.watchdog.pid"

DRY_RUN=1
if [[ "${1:-}" == "--apply" ]]; then
  DRY_RUN=0
elif [[ "${1:-}" != "" && "${1:-}" != "--dry-run" ]]; then
  echo "Usage: $0 [--dry-run|--apply]" >&2
  exit 2
fi

run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    printf 'DRY-RUN:'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

if [[ "$DRY_RUN" == "0" && "$EUID" -ne 0 ]]; then
  echo "ERROR: --apply requires root" >&2
  exit 1
fi

if [[ "$DRY_RUN" == "0" ]]; then
  mkdir -p "$STATE_DIR"
fi

# Remove only the dedicated P2.6 chain/jump; never flush unrelated firewall rules.
while iptables -C INPUT -p tcp --dport "$PORT" -j "$CHAIN" 2>/dev/null; do
  run iptables -D INPUT -p tcp --dport "$PORT" -j "$CHAIN"
  [[ "$DRY_RUN" == "1" ]] && break
done
if iptables -L "$CHAIN" -n >/dev/null 2>&1; then
  run iptables -F "$CHAIN"
  run iptables -X "$CHAIN"
fi

if [[ "$DRY_RUN" == "0" ]]; then
  rm -f "$CONFIRM_FILE"
  if [[ -f "$WATCHDOG_PID_FILE" ]]; then
    pid="$(cat "$WATCHDOG_PID_FILE" 2>/dev/null || true)"
    if [[ "$pid" =~ ^[0-9]+$ ]]; then
      kill "$pid" 2>/dev/null || true
    fi
    rm -f "$WATCHDOG_PID_FILE"
  fi
fi

echo "P26 port $PORT hardening rolled back (dry_run=$DRY_RUN)"
