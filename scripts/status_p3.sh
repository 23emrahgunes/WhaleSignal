#!/usr/bin/env bash
set -Eeuo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
PY="$REPO_DIR/.venv/bin/python"
[[ -x "$PY" ]] || { echo "ERROR: missing venv" >&2; exit 1; }
STATE="$(systemctl is-active direction-engine-p3-arbitrage.service 2>/dev/null || true)"
HTTP="$(curl -sS --connect-timeout 2 --max-time 5 -o /tmp/p3-health.json -w '%{http_code}' http://127.0.0.1:8093/health || true)"
echo "=== P3 STATUS ==="
echo "p3_service=$STATE"
echo "p3_health_http=$HTTP"
"$PY" - <<'PY'
from pathlib import Path
from p3_config import get_p3_settings
from p3_schema import connect_p3, ensure_p3_schema, integrity_check
s=get_p3_settings(); p=Path(s.p3_db_path)
if not p.exists():
    print("p3_database=MISSING", p); raise SystemExit(0)
conn=connect_p3(s.p3_db_path); ensure_p3_schema(conn)
print("p3_database=", p.resolve())
print("integrity=", integrity_check(conn))
for table in ("p3_opportunities","p3_windows","p3_replays","p3_health_events"):
    print(f"{table}=", conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
for row in conn.execute("SELECT strategy,COUNT(*),MAX(net_profit_usdc) FROM p3_opportunities GROUP BY strategy ORDER BY strategy"):
    print("strategy=", row[0], "opportunities=", row[1], "peak_net_profit=", row[2])
print("safety=", {"mode":"SHADOW_PAPER_ONLY","execution":False,"private_key":False,"signing":False,"orders":False})
conn.close()
PY
