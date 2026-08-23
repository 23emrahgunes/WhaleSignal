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
import json
from pathlib import Path
from p3_config import get_p3_settings
from p3_schema import connect_p3, ensure_p3_schema, integrity_check

s=get_p3_settings(); p=Path(s.p3_db_path)
print("live_config=", {
    "feature_enabled": s.live_feature_enabled,
    "auto_execute_enabled": s.live_auto_execute_enabled,
    "require_dry_validated": s.live_require_dry_validated,
    "max_cycle_usdc": s.live_max_capital_per_cycle_usdc,
    "control": f"{s.live_control_host}:{s.live_control_port}",
})
health_path=Path('/tmp/p3-health.json')
if health_path.exists():
    try:
        h=json.loads(health_path.read_text(encoding='utf-8'))
    except Exception:
        h={}
    print("runtime=", {
        "mode": h.get("mode"),
        "execution_enabled": h.get("execution_enabled"),
        "order_submission_enabled": h.get("order_submission_enabled"),
        "live_feature_enabled": h.get("live_feature_enabled"),
    })
if not p.exists():
    print("p3_database=MISSING", p); raise SystemExit(0)
conn=connect_p3(s.p3_db_path); ensure_p3_schema(conn)
print("p3_database=", p.resolve())
print("integrity=", integrity_check(conn))
for table in (
    "p3_opportunities","p3_windows","p3_window_observations","p3_replays",
    "p3_entry_replays","p3_live_cycles","p3_health_events",
):
    print(f"{table}=", conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
row=conn.execute("SELECT value FROM p3_meta WHERE key='latest_scan_stats_json'").fetchone()
if row is not None:
    try: scan=json.loads(str(row[0]))
    except Exception: scan={}
    keys=(
        "conditions","valid_pairs","missing_book","stale_book","source_skew",
        "transport_stale","session_incomplete","high_source_skew","missing_fee",
        "positive_buy_merge","positive_split_sell","inserted","windows_closed",
    )
    print("scanner=", {key: scan.get(key,0) for key in keys})
    print("book_transport=", scan.get("book_transport") or {})
for row in conn.execute(
    "SELECT strategy,COUNT(*),MAX(net_profit_usdc) FROM p3_opportunities GROUP BY strategy ORDER BY strategy"
):
    print("strategy=", row[0], "opportunities=", row[1], "peak_net_profit=", row[2])
recent=conn.execute(
    "SELECT id,combo_key,status,capital_usdc,error_code FROM p3_live_cycles ORDER BY id DESC LIMIT 5"
).fetchall()
print("recent_live_cycles=", [dict(row) for row in recent])
conn.close()
PY

CONTROL_ENABLED="$("$PY" - <<'PY'
from p3_config import get_p3_settings
print('1' if get_p3_settings().live_control_enabled else '0')
PY
)"
if [[ "$CONTROL_ENABLED" == "1" ]]; then
  CONTROL_PORT="$("$PY" - <<'PY'
from p3_config import get_p3_settings
print(get_p3_settings().live_control_port)
PY
)"
  echo "live_control_status:"
  curl -sS --connect-timeout 1 --max-time 2 "http://127.0.0.1:${CONTROL_PORT}/api/status" || true
  echo
fi
