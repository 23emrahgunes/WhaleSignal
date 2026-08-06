#!/bin/bash

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "=== PM-Edge System Health Diagnostics ==="

# 1. Process Status
if pgrep -f "pm-edge tv-direction" > /dev/null; then
    echo -e "Process Status: ${GREEN}PASS${NC} (pm-edge is running)"
else
    echo -e "Process Status: ${RED}FAIL${NC} (pm-edge is NOT running)"
fi

# 2. Port Listening Status
if ss -tlnp | grep -q ":8080 " > /dev/null; then
    echo -e "Port 8080: ${GREEN}PASS${NC} (Listening)"
else
    echo -e "Port 8080: ${RED}FAIL${NC} (NOT listening)"
fi

# 3. HTTP Health Ping
HEALTH_RESP=$(curl -s --max-time 2 http://127.0.0.1:8080/health || true)
if [ "$HEALTH_RESP" = "OK" ]; then
    echo -e "REST API Ping (/health): ${GREEN}PASS${NC} (Received OK)"
else
    echo -e "REST API Ping (/health): ${RED}FAIL${NC} (Connection failed or invalid response: '$HEALTH_RESP')"
fi

# 4. SQLite integrity check
DB_FILE="data/tv_direction.sqlite"
if [ -f "$DB_FILE" ]; then
    INTEGRITY=$(sqlite3 "$DB_FILE" "PRAGMA integrity_check;" 2>/dev/null || true)
    if [ "$INTEGRITY" = "ok" ]; then
        echo -e "SQLite DB Integrity: ${GREEN}PASS${NC} (Database is healthy)"
    else
        echo -e "SQLite DB Integrity: ${RED}FAIL${NC} (Database corrupt or verification failed: '$INTEGRITY')"
    fi
else
    echo -e "SQLite DB Integrity: ${YELLOW}WARN${NC} (Database file not found yet)"
fi

# 5. REST Payload Analysis (/api/live)
LIVE_PAYLOAD=$(curl -s --max-time 2 http://127.0.0.1:8080/api/live || true)
if [ -n "$LIVE_PAYLOAD" ] && [[ "$LIVE_PAYLOAD" != *"waiting_for_data"* ]]; then
    # WebSocket Status
    DATA_SOURCE=$(echo "$LIVE_PAYLOAD" | grep -o '"dataSource":"[^"]*' | grep -o '[^"]*$')
    if [ "$DATA_SOURCE" = "BINANCE_WS" ]; then
        echo -e "WebSocket Status: ${GREEN}PASS${NC} (Connected to Binance WS)"
    else
        echo -e "WebSocket Status: ${YELLOW}WARN${NC} (Not connected to WS; current source: $DATA_SOURCE)"
    fi

    # DataSource check
    echo -e "Current DataSource: ${GREEN}PASS${NC} ($DATA_SOURCE)"

    # Market Unavailable check
    if echo "$LIVE_PAYLOAD" | grep -q '"priceToBeat":100000'; then
        echo -e "Market Status: ${YELLOW}WARN${NC} (No active Polymarket match; using default fallback target)"
    else
        echo -e "Market Status: ${GREEN}PASS${NC} (Real-time Polymarket target active)"
    fi

    # Stale Price check
    MARKET_STALE=$(echo "$LIVE_PAYLOAD" | grep -o '"marketStale":[^,]*' | grep -o '[^:]*$')
    if [ "$MARKET_STALE" = "true" ]; then
         echo -e "Market Freshness: ${YELLOW}WARN${NC} (Stale flagged)"
    else
         echo -e "Market Freshness: ${GREEN}PASS${NC} (Fresh Polymarket polling)"
    fi
else
    echo -e "Telemetry Stream Checks: ${YELLOW}WARN${NC} (No live telemetry records arrived yet)"
fi

echo "=== Diagnostics Complete ==="
