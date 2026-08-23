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
from p3_live_ledger import ensure_live_ledger_schema, live_ledger_summary
from p3_schema import connect_p3, ensure_p3_schema, integrity_check

s=get_p3_settings(); p=Path(s.p3_db_path)
print("live_config=", {
    "feature_enabled": s.live_feature_enabled,
    "auto_execute_enabled": s.live_auto_execute_enabled,
    "require_dry_validated": s.live_require_dry_validated,
    "sizing_mode": "EQUAL_SHARES_FRESH_DEPTH",
    "target_shares_each_leg": s.live_target_quantity_shares,
    "hard_max_shares_each_leg": s.live_max_quantity_shares,
    "legacy_dollar_scaler_enabled": False,
    "max_single_leg_notional_usdc": s.live_max_single_leg_notional_usdc,
    "max_projected_unwind_loss_usdc": s.live_max_projected_unwind_loss_usdc,
    "emergency_unwind_loss_usdc": s.live_emergency_unwind_loss_usdc,
    "halt_after_one_leg": s.live_halt_after_one_leg,
    "rolling_24h_gross_loss_limit_usdc": s.live_rolling_24h_gross_loss_limit_usdc,
    "control": f"authenticated_web:{s.web_host}:{s.web_port}",
    "web_auth_required": s.web_auth_required,
    "web_cookie_secure": s.web_cookie_secure,
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
conn=connect_p3(s.p3_db_path); ensure_p3_schema(conn); ensure_live_ledger_schema(conn)
print("p3_database=", p.resolve())
print("integrity=", integrity_check(conn))
for table in (
    "p3_opportunities","p3_windows","p3_window_observations","p3_replays",
    "p3_entry_replays","p3_live_cycles","p3_live_ledger","p3_health_events",
):
    print(f"{table}=", conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
ledger=live_ledger_summary(conn)
print("live_realized=", {
    "cycles": ledger["cycles"],
    "realized_cycles": ledger["realized_cycles"],
    "realized_pnl_usdc": ledger["realized_pnl_usdc"],
    "avg_realized_pnl_usdc": ledger["average_realized_pnl_usdc"],
    "one_leg_events": ledger["one_leg_events"],
    "one_leg_rate": ledger["one_leg_rate"],
    "rolling_24h_gross_loss_usdc": ledger["rolling_24h_gross_loss_usdc"],
})
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
recent=conn.execute(
    "SELECT id,combo_key,status,quantity_shares,capital_usdc,error_code FROM p3_live_cycles ORDER BY id DESC LIMIT 5"
).fetchall()
print("recent_live_cycles=", [dict(row) for row in recent])
conn.close()
PY

AUTH_REQUIRED="$("$PY" - <<'PY'
from p3_config import get_p3_settings
print('1' if get_p3_settings().web_auth_required else '0')
PY
)"
if [[ "$AUTH_REQUIRED" == "1" ]]; then
  CODE="$(curl -sS --connect-timeout 1 --max-time 2 -o /tmp/p3-status-unauth.json -w '%{http_code}' http://127.0.0.1:8093/api/session || true)"
  echo "unauthenticated_8093_session_http=$CODE expected=401"
fi
