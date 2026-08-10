#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PORT="${PORT:-8080}"
PREBUILT="${PM_EDGE_PREBUILT:-$SCRIPT_DIR/pm-edge-linux-amd64}"
TARGET="$SCRIPT_DIR/pm-edge"
TMP_TARGET="$SCRIPT_DIR/.pm-edge.new"

echo "========================================================="
echo " PM-Edge TV-Direction Paper Research Engine Deployment"
echo "========================================================="

mkdir -p logs data reports backup
if [ ! -f .env ] && [ -f .env.example ]; then
    cp .env.example .env
fi

stop_old_instance() {
    local pids
    pids="$(pgrep -f '(^|/)pm-edge tv-direction' || true)"
    if [ -n "$pids" ]; then
        echo "Stopping previous pm-edge process(es): $pids"
        kill $pids 2>/dev/null || true
        sleep 2
        pids="$(pgrep -f '(^|/)pm-edge tv-direction' || true)"
        if [ -n "$pids" ]; then
            kill -9 $pids 2>/dev/null || true
        fi
    fi
}

build_or_install_binary() {
    rm -f "$TMP_TARGET"

    if [ -f "$PREBUILT" ]; then
        echo "Using prebuilt Linux amd64 binary: $PREBUILT"
        cp "$PREBUILT" "$TMP_TARGET"
        chmod +x "$TMP_TARGET"
    else
        if ! command -v go >/dev/null 2>&1; then
            cat >&2 <<'EOF'
ERROR: No prebuilt binary was found and Go is not available.
Download the GitHub Actions artifact named 'pm-edge-linux-amd64', extract it
into this repository directory, then run ./deploy.sh again.
EOF
            exit 1
        fi

        echo "No prebuilt binary found; building in low-memory mode with $(go version)."
        if [ "$(go env GOROOT)" = "$(go env GOPATH)" ]; then
            echo "ERROR: GOROOT and GOPATH must not point to the same directory." >&2
            exit 1
        fi

        # modernc.org/sqlite is memory-heavy to compile. Serial compilation and
        # aggressive GC make this the safest fallback on small VPS instances.
        if ! GOMAXPROCS="${GOMAXPROCS:-1}" GOGC="${GOGC:-20}" \
            go build -p=1 -trimpath -o "$TMP_TARGET" ./cmd/pm-edge; then
            cat >&2 <<'EOF'
ERROR: Local compilation failed. On ~1 GB VPS hosts this is commonly an OOM.
Use the CI-built 'pm-edge-linux-amd64' artifact instead of compiling locally.
EOF
            rm -f "$TMP_TARGET"
            exit 1
        fi
    fi

    mv -f "$TMP_TARGET" "$TARGET"
    chmod +x "$TARGET"
}

stop_old_instance
build_or_install_binary

# Generate user-local service/logrotate templates without requiring sudo.
if [ -f pm-edge.service ]; then
    sed "s|/app|$SCRIPT_DIR|g" pm-edge.service > pm-edge.service.local
fi
if [ -f pm-edge.logrotate ]; then
    sed "s|/app|$SCRIPT_DIR|g" pm-edge.logrotate > pm-edge.logrotate.local
fi

: > logs/run_output.log
nohup "$TARGET" tv-direction >> logs/run_output.log 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > logs/pm-edge.pid

healthy=0
for _ in $(seq 1 10); do
    if ! kill -0 "$NEW_PID" 2>/dev/null; then
        break
    fi
    if command -v curl >/dev/null 2>&1; then
        if curl --fail --silent --max-time 2 "http://127.0.0.1:${PORT}/health" >/dev/null; then
            healthy=1
            break
        fi
    else
        # Without curl, staying alive for the grace period is the best local check.
        healthy=1
        break
    fi
    sleep 1
done

if [ "$healthy" -ne 1 ]; then
    echo "ERROR: pm-edge did not become healthy. Last log lines:" >&2
    tail -n 40 logs/run_output.log >&2 || true
    kill "$NEW_PID" 2>/dev/null || true
    exit 1
fi

echo "Deployment successful. PID: $NEW_PID"
echo "Health: http://127.0.0.1:${PORT}/health"
echo "Logs:   tail -f $SCRIPT_DIR/logs/run_output.log"
echo "Stop:   kill $NEW_PID"
