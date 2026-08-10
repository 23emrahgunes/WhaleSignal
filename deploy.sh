#!/bin/bash
set -e

echo "=== Deploying PM-Edge TV-Direction Research Engine ==="

# 1. Setup Directories
mkdir -p logs data reports backup

# 2. Setup Environment Configuration
if [ ! -f .env ]; then
    echo ".env not found. Copying from .env.example..."
    cp .env.example .env
fi

# 3. Clean and verify dependencies
echo "Running Go Mod Tidy..."
go mod tidy

# 4. Standardise syntax format
echo "Running Go Fmt..."
gofmt -w -s .

# 5. Lint
echo "Running Go Vet..."
go vet ./...

# 6. Test Suite Execution
echo "Running Suite Unit Tests..."
go test ./...

# 7. Production Compilation
echo "Compiling single static binary..."
go build -o pm-edge ./cmd/pm-edge

echo "=== Deployment build complete! pm-edge binary created ==="
