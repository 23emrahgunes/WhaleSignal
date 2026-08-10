#!/bin/bash
set -e

echo "=== PM-Edge TV-Direction Production Installer ==="

# 1. Update APT
echo "Updating apt repositories..."
sudo apt-get update -y

# 2. Install basic packages
echo "Installing base system packages..."
sudo apt-get install -y git sqlite3 build-essential curl wget logrotate

# 3. Install Go 1.25.0 if not already present or if version is old
if command -v go >/dev/null 2>&1; then
    GO_VERSION=$(go version | awk '{print $3}' | sed 's/go//')
    echo "Found Go version: $GO_VERSION"
else
    echo "Go is missing. Installing Go 1.25.0..."
    wget -q https://go.dev/dl/go1.25.0.linux-amd64.tar.gz
    sudo rm -rf /usr/local/go
    sudo tar -C /usr/local -xzf go1.25.0.linux-amd64.tar.gz
    rm go1.25.0.linux-amd64.tar.gz

    # Export path
    export PATH=$PATH:/usr/local/go
    if ! grep -q "/usr/local/go/bin" ~/.profile; then
        echo 'export PATH=$PATH:/usr/local/go/bin' >> ~/.profile
    fi
    if ! grep -q "/usr/local/go/bin" ~/.bashrc; then
        echo 'export PATH=$PATH:/usr/local/go/bin' >> ~/.bashrc
    fi
    echo "Go 1.25.0 installed successfully"
fi

echo "=== Installation steps complete! ==="
