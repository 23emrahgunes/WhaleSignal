#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PORT="${PORT:-8080}"
FORCE_BUILD="${FORCE_BUILD:-0}"
BINARY="$SCRIPT_DIR/pm-edge"
LOG_FILE="$SCRIPT_DIR/logs/run_output.log"

echo "========================================================="
echo "   PM-Edge TV-Direction Research Engine Auto-Deployer"
echo "========================================================="
echo "Working directory: $SCRIPT_DIR"

mkdir -p logs data reports backup

if [ ! -f .env ] && [ -f .env.example ]; then
    cp .env.example .env
    echo "Created .env from .env.example"
fi

# CI publishes a verified Linux amd64 binary into the repository. This is the
# default deployment path so small VPS instances do not have to compile
# modernc/sqlite locally. Set FORCE_BUILD=1 only when a local rebuild is wanted.
if [ "$FORCE_BUILD" = "1" ] || [ ! -f "$BINARY" ]; then
    echo "Local build requested/required."
    if ! command -v go >/dev/null 2>&1; then
        echo "ERROR: Go is required because no usable prebuilt binary was selected."
        exit 1
    fi

    echo "Using $(go version)"
    echo "Building with low-memory settings..."
    GOMAXPROCS="${GOMAXPROCS:-1}" GOGC="${GOGC:-20}" go build -p=1 -trimpath -o "$BINARY" ./cmd/pm-edge
else
    echo "Using CI-verified prebuilt binary: $BINARY"
fi

chmod +x "$BINARY"

ARCH="$(uname -m)"
case "$ARCH" in
    x86_64|amd64)
        ;;
    *)
        echo "ERROR: tracked pm-edge binary targets Linux amd64, VPS reports architecture: $ARCH"
        echo "Use a matching CI artifact or build locally with FORCE_BUILD=1."
        exit 1
        ;;
esac

# ELF magic check without depending on the optional `file` package.
ELF_MAGIC="$(od -An -tx1 -N4 "$BINARY" 2>/dev/null | tr -d ' \n')"
if [ "$ELF_MAGIC" != "7f454c46" ]; then
    echo "ERROR: $BINARY is not a Linux ELF executable."
    exit 1
fi

# Stop only this application's user-owned processes. No sudo/systemd required.
OLD_PIDS="$(pgrep -f "${BINARY} tv-direction" || true)"
if [ -z "$OLD_PIDS" ]; then
    OLD_PIDS="$(pgrep -f "pm-edge tv-direction" || true)"
fi
if [ -n "$OLD_PIDS" ]; then
    echo "Stopping old pm-edge process(es): $OLD_PIDS"
    kill $OLD_PIDS 2>/dev/null || true
    sleep 2
    REMAINING="$(pgrep -f "pm-edge tv-direction" || true)"
    if [ -n "$REMAINING" ]; then
        kill -9 $REMAINING 2>/dev/null || true
    fi
fi

# Generate optional local service/logrotate templates but do not require sudo.
if [ -f pm-edge.service ]; then
    sed "s|/app|$SCRIPT_DIR|g" pm-edge.service > pm-edge.service.local
fi
if [ -f pm-edge.logrotate ]; then
    sed "s|/app|$SCRIPT_DIR|g" pm-edge.logrotate > pm-edge.logrotate.local
fi

: > "$LOG_FILE"
echo "Starting paper-only engine..."
nohup "$BINARY" tv-direction >> "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$SCRIPT_DIR/pm-edge.pid"

# Process + HTTP startup smoke check. Mid-window NO_SIGNAL is valid; the process
# and health endpoint must still become available.
for _ in $(seq 1 15); do
    if ! kill -0 "$NEW_PID" 2>/dev/null; then
        echo "ERROR: pm-edge exited during startup."
        tail -n 40 "$LOG_FILE" || true
        exit 1
    fi

    if command -v curl >/dev/null 2>&1; then
        if curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
            echo "========================================================="
            echo "DEPLOYMENT SUCCESSFUL"
            echo "PID: $NEW_PID"
            echo "Health: http://127.0.0.1:${PORT}/health"
            echo "Dashboard/API: http://YOUR_VPS_IP:${PORT}"
            echo "Logs: tail -f $LOG_FILE"
            echo "========================================================="
            exit 0
        fi
    else
        # If curl is unavailable, surviving the startup window is the fallback check.
        sleep 1
        if kill -0 "$NEW_PID" 2>/dev/null; then
            echo "DEPLOYMENT SUCCESSFUL (process check only; curl is not installed). PID: $NEW_PID"
            exit 0
        fi
    fi
    sleep 1
done

echo "ERROR: pm-edge stayed alive but /health did not become ready on port $PORT."
tail -n 40 "$LOG_FILE" || true
exit 1
