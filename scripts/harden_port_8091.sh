#!/usr/bin/env bash
set -Eeuo pipefail

CHAIN="DIRECTION_ENGINE_8091"
PORT="${P26_PORT:-8091}"
STATE_DIR="${P26_HARDEN_STATE_DIR:-/var/lib/direction-engine-p26}"
CONFIRM_FILE="$STATE_DIR/port-${PORT}.confirmed"
WATCHDOG_PID_FILE="$STATE_DIR/port-${PORT}.watchdog.pid"
WATCHDOG_SEC="${P26_HARDEN_WATCHDOG_SEC:-60}"
AUTHORIZED_CIDR="${P26_AUTHORIZED_CIDR:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MODE="dry-run"
case "${1:-}" in
  ""|--dry-run) MODE="dry-run" ;;
  --apply) MODE="apply" ;;
  --confirm) MODE="confirm" ;;
  *) echo "Usage: $0 [--dry-run|--apply|--confirm]" >&2; exit 2 ;;
esac

if [[ "$MODE" == "confirm" ]]; then
  if [[ "$EUID" -ne 0 ]]; then
    echo "ERROR: --confirm requires root" >&2
    exit 1
  fi
  mkdir -p "$STATE_DIR"
  touch "$CONFIRM_FILE"
  echo "P26 port hardening confirmed; watchdog rollback cancelled"
  exit 0
fi

DRY_RUN=1
[[ "$MODE" == "apply" ]] && DRY_RUN=0
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
if ! command -v iptables >/dev/null 2>&1; then
  echo "ERROR: iptables is required; use AWS Security Group as the primary control" >&2
  exit 1
fi
if [[ -n "$AUTHORIZED_CIDR" ]] && ! [[ "$AUTHORIZED_CIDR" =~ ^[0-9a-fA-F:.]+(/[0-9]{1,3})?$ ]]; then
  echo "ERROR: invalid P26_AUTHORIZED_CIDR" >&2
  exit 1
fi

if [[ "$DRY_RUN" == "0" ]]; then
  mkdir -p "$STATE_DIR"
  rm -f "$CONFIRM_FILE"
fi

if ! iptables -L "$CHAIN" -n >/dev/null 2>&1; then
  run iptables -N "$CHAIN"
fi
run iptables -F "$CHAIN"
run iptables -A "$CHAIN" -i lo -j ACCEPT
if [[ -n "$AUTHORIZED_CIDR" ]]; then
  run iptables -A "$CHAIN" -s "$AUTHORIZED_CIDR" -j ACCEPT
fi
run iptables -A "$CHAIN" -j REJECT --reject-with tcp-reset
if ! iptables -C INPUT -p tcp --dport "$PORT" -j "$CHAIN" 2>/dev/null; then
  run iptables -I INPUT 1 -p tcp --dport "$PORT" -j "$CHAIN"
fi

if [[ "$DRY_RUN" == "0" ]]; then
  nohup bash -c "
    sleep '$WATCHDOG_SEC'
    if [[ ! -f '$CONFIRM_FILE' ]]; then
      '$SCRIPT_DIR/rollback_port_8091.sh' --apply >> '$STATE_DIR/watchdog.log' 2>&1
    fi
  " >/dev/null 2>&1 &
  echo "$!" > "$WATCHDOG_PID_FILE"
fi

cat <<EOF
P26 port hardening prepared (dry_run=$DRY_RUN)
- localhost remains allowed
- authorized_cidr=${AUTHORIZED_CIDR:-NONE}
- all other TCP/$PORT traffic is rejected
- AWS Security Group remains the primary perimeter control
- watchdog_seconds=$WATCHDOG_SEC
EOF
if [[ "$DRY_RUN" == "0" ]]; then
  echo "Verify access, then run: sudo $0 --confirm"
fi
