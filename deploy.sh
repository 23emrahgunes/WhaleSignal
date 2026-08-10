#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ -z "${PORT+x}" ] && [ -f .env ]; then
    ENV_PORT="$(sed -n 's/^PORT=//p' .env | tail -n 1 | tr -d '\r' || true)"
    PORT="${ENV_PORT:-8080}"
else
    PORT="${PORT:-8080}"
fi

PREBUILT="${PM_EDGE_PREBUILT:-$SCRIPT_DIR/pm-edge-linux-amd64}"
TARGET="$SCRIPT_DIR/pm-edge"
TMP_TARGET="$SCRIPT_DIR/.pm-edge.new"
TMP_SHA="$SCRIPT_DIR/.pm-edge.new.sha256"
RELEASE_URL="${PM_EDGE_RELEASE_URL:-https://github.com/23emrahgunes/WhaleSignal/releases/download/vps-latest/pm-edge-linux-amd64}"
RELEASE_SHA_URL="${PM_EDGE_RELEASE_SHA256_URL:-${RELEASE_URL}.sha256}"

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

download_file() {
    local url="$1"
    local output="$2"

    if command -v curl >/dev/null 2>&1; then
        curl --fail --location --silent --show-error --retry 3 --connect-timeout 10 \
            --output "$output" "$url"
        return
    fi
    if command -v wget >/dev/null 2>&1; then
        wget -q --tries=3 --timeout=15 -O "$output" "$url"
        return
    fi
    return 1
}

verify_linux_amd64_binary() {
    local binary="$1"
    local magic

    if [ ! -s "$binary" ]; then
        return 1
    fi
    magic="$(od -An -tx1 -N4 "$binary" 2>/dev/null | tr -d ' \n')"
    [ "$magic" = "7f454c46" ]
}

download_release_binary() {
    local arch expected actual
    arch="$(uname -m)"
    case "$arch" in
        x86_64|amd64) ;;
        *)
            echo "GitHub VPS release is linux/amd64; host architecture is $arch."
            return 1
            ;;
    esac

    echo "Downloading CI-verified VPS binary from GitHub release..."
    rm -f "$TMP_TARGET" "$TMP_SHA"
    if ! download_file "$RELEASE_URL" "$TMP_TARGET"; then
        echo "GitHub release binary download failed; will try local build fallback." >&2
        rm -f "$TMP_TARGET" "$TMP_SHA"
        return 1
    fi
    if ! download_file "$RELEASE_SHA_URL" "$TMP_SHA"; then
        echo "GitHub release checksum download failed; refusing unverified binary." >&2
        rm -f "$TMP_TARGET" "$TMP_SHA"
        return 1
    fi

    if ! command -v sha256sum >/dev/null 2>&1; then
        echo "sha256sum is required to verify the GitHub release binary." >&2
        rm -f "$TMP_TARGET" "$TMP_SHA"
        return 1
    fi

    expected="$(awk 'NR==1 {print $1}' "$TMP_SHA")"
    actual="$(sha256sum "$TMP_TARGET" | awk '{print $1}')"
    if [ -z "$expected" ] || [ "$expected" != "$actual" ]; then
        echo "GitHub release checksum verification FAILED." >&2
        rm -f "$TMP_TARGET" "$TMP_SHA"
        return 1
    fi
    if ! verify_linux_amd64_binary "$TMP_TARGET"; then
        echo "Downloaded file is not a valid ELF executable." >&2
        rm -f "$TMP_TARGET" "$TMP_SHA"
        return 1
    fi

    chmod +x "$TMP_TARGET"
    echo "GitHub release verified: sha256=$actual"
    rm -f "$TMP_SHA"
    return 0
}

build_or_install_binary() {
    rm -f "$TMP_TARGET" "$TMP_SHA"

    if [ -f "$PREBUILT" ]; then
        echo "Using local prebuilt Linux amd64 binary: $PREBUILT"
        cp "$PREBUILT" "$TMP_TARGET"
        chmod +x "$TMP_TARGET"
        if ! verify_linux_amd64_binary "$TMP_TARGET"; then
            echo "ERROR: local prebuilt is not a valid ELF executable." >&2
            rm -f "$TMP_TARGET"
            exit 1
        fi
    elif download_release_binary; then
        :
    else
        if ! command -v go >/dev/null 2>&1; then
            cat >&2 <<'EOF'
ERROR: GitHub release download failed, no local prebuilt binary exists, and Go is unavailable.
Check internet access to github.com and run ./deploy.sh again.
EOF
            exit 1
        fi

        echo "No downloadable/prebuilt binary available; building in low-memory mode with $(go version)."
        if [ "$(go env GOROOT)" = "$(go env GOPATH)" ]; then
            echo "ERROR: GOROOT and GOPATH must not point to the same directory." >&2
            exit 1
        fi

        if ! GOMAXPROCS="${GOMAXPROCS:-1}" GOGC="${GOGC:-20}" \
            go build -p=1 -trimpath -o "$TMP_TARGET" ./cmd/pm-edge; then
            cat >&2 <<'EOF'
ERROR: Local compilation failed. On ~1 GB VPS hosts this is commonly an OOM.
The preferred path is the CI-built public 'vps-latest' GitHub release binary.
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
for _ in $(seq 1 15); do
    if ! kill -0 "$NEW_PID" 2>/dev/null; then
        break
    fi
    if command -v curl >/dev/null 2>&1; then
        if curl --fail --silent --max-time 2 "http://127.0.0.1:${PORT}/health" >/dev/null; then
            healthy=1
            break
        fi
    else
        healthy=1
        break
    fi
    sleep 1
done

if [ "$healthy" -ne 1 ]; then
    echo "ERROR: pm-edge did not become healthy. Last log lines:" >&2
    tail -n 60 logs/run_output.log >&2 || true
    kill "$NEW_PID" 2>/dev/null || true
    exit 1
fi

echo "Deployment successful. PID: $NEW_PID"
echo "Health: http://127.0.0.1:${PORT}/health"
echo "Paper:  http://127.0.0.1:${PORT}/api/paper/stats"
echo "Logs:   tail -f $SCRIPT_DIR/logs/run_output.log"
echo "Stop:   kill $NEW_PID"
