#!/bin/bash
set -e

echo "========================================================="
echo "   PM-Edge TV-Direction Research Engine Auto-Deployer    "
echo "========================================================="

# Get active script's physical path
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# --- 1. Automatic Go Environment Optimization & Fix ---
echo "Checking and configuring Go environment..."

# If ~/go exists and contains bin/go (extracted as root distribution directly in ~)
if [ -d "$HOME/go" ] && [ -f "$HOME/go/bin/go" ]; then
    echo "Relocating Go installation to ~/.go to avoid GOPATH/GOROOT conflicts..."
    # Clean up ~/.go if it already exists to avoid conflict
    rm -rf "$HOME/.go"
    mv "$HOME/go" "$HOME/.go"
fi

# Define our preferred paths
export GOROOT="$HOME/.go"
export GOPATH="$HOME/go"
export PATH="$GOROOT/bin:$GOPATH/bin:$PATH"

# Ensure GOPATH workspace directories exist
mkdir -p "$GOPATH/src" "$GOPATH/bin" "$GOPATH/pkg"

# Programmatically fix ~/.bashrc without needing manual text editors (nano/vim)
BASHRC="$HOME/.bashrc"
if [ -f "$BASHRC" ]; then
    echo "Optimizing ~/.bashrc PATH variables..."
    # Create clean backup of bashrc
    cp "$BASHRC" "$BASHRC.bak"

    # 1. Strip out the warning-prone simple path line if exists
    grep -v 'export PATH=\$PATH:\$HOME/go/bin' "$BASHRC" > "$BASHRC.tmp" || true
    # 2. Strip any previously added GOROOT/GOPATH lines from this script to avoid duplication
    grep -v 'export GOROOT=' "$BASHRC.tmp" > "$BASHRC.tmp2" || true
    grep -v 'export GOPATH=' "$BASHRC.tmp2" > "$BASHRC.tmp3" || true
    # Move back
    mv "$BASHRC.tmp3" "$BASHRC"
    rm -f "$BASHRC.tmp" "$BASHRC.tmp2"

    # 3. Append the correct path configuration clean and safe
    echo 'export GOROOT=$HOME/.go' >> "$BASHRC"
    echo 'export GOPATH=$HOME/go' >> "$BASHRC"
    echo 'export PATH=$GOROOT/bin:$GOPATH/bin:$PATH' >> "$BASHRC"
    echo "Updated ~/.bashrc automatically with conflict-free paths."
fi

# Check if Go is accessible now
if ! command -v go >/dev/null 2>&1; then
    echo "Error: Go is still not found in current path."
    echo "Expected location: $GOROOT/bin/go"
    exit 1
else
    echo "Success: $(go version) found and configured!"
fi

# --- 2. Terminate Old Instance ---
echo "Checking for any running pm-edge instance..."
PID=$(pgrep -f "pm-edge tv-direction" || true)
if [ -n "$PID" ]; then
    echo "Stopping existing pm-edge process (PID: $PID)..."
    kill "$PID" 2>/dev/null || true
    sleep 2
    # Force kill if still alive
    kill -9 "$PID" 2>/dev/null || true
fi

# Also check if something is listening on port 8080 (the default port for our web UI)
PORT_PID=$(lsof -t -i :8080 2>/dev/null || true)
if [ -n "$PORT_PID" ]; then
    echo "Stopping process listening on port 8080 (PID: $PORT_PID)..."
    kill -9 "$PORT_PID" 2>/dev/null || true
fi

# --- 3. Directory Setup ---
echo "Setting up workspace directories..."
mkdir -p logs data reports backup

# --- 4. Environment Configuration Setup ---
if [ ! -f .env ]; then
    echo ".env not found. Copying default configuration from .env.example..."
    cp .env.example .env
fi

# --- 5. Compile Project ---
echo "Tidying Go module dependencies..."
go mod tidy

echo "Formatting source files..."
gofmt -w -s .

echo "Compiling the PM-Edge binary..."
go build -o pm-edge ./cmd/pm-edge

echo "Compilation successful!"

# --- 6. Update Systemd/Logrotate with Absolute Paths ---
# If the user has sudo we can update system services, otherwise we advise running as userland nohup
echo "Generating customized service configurations for user..."
sed "s|/app|$SCRIPT_DIR|g" pm-edge.service > pm-edge.service.local
sed "s|/app|$SCRIPT_DIR|g" pm-edge.logrotate > pm-edge.logrotate.local

# --- 7. Launch in Background (Paper-Only Engine) ---
echo "Starting PM-Edge Research Engine in paper-trading background mode..."
nohup ./pm-edge tv-direction > logs/run_output.log 2>&1 &

# Wait 3 seconds to check if it crashed on boot
sleep 3
NEW_PID=$(pgrep -f "pm-edge tv-direction" || true)

if [ -z "$NEW_PID" ]; then
    echo "❌ Error: pm-edge failed to start. Last 15 lines of log output:"
    tail -n 15 logs/run_output.log
    exit 1
else
    echo "✅ PM-Edge successfully started with PID: $NEW_PID!"
    echo "========================================================="
    echo "🎉 DEPLOYMENT SUCCESSFUL!"
    echo "========================================================="
    echo "Web Dashboard: http://YOUR_VPS_IP:8080"
    echo ""
    echo "Useful Commands:"
    echo "👉 View Live Logs:      tail -f logs/run_output.log"
    echo "👉 Check Status:        ./healthcheck.sh"
    echo "👉 Stop Application:    pkill -f \"pm-edge tv-direction\""
    echo "========================================================="
    echo "💡 Note: If you want to use systemd instead of background nohup,"
    echo "   we have customized a config for you here:"
    echo "   $SCRIPT_DIR/pm-edge.service.local"
    echo "========================================================="
fi
