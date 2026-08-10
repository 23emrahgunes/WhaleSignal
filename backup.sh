#!/bin/bash
set -e

echo "=== Creating Timestamped Backup ==="

mkdir -p backup

TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_FILE="backup/pm_edge_backup_${TIMESTAMP}.tar.gz"

echo "Compressing SQLite database, reports, and logs..."
tar -czf "$BACKUP_FILE" data/ logs/ reports/

echo "=== Backup created successfully: $BACKUP_FILE ==="
