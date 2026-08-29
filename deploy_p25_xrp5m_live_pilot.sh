#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")"

nonce="${P25_LIVE_ARM_NONCE:-}"
if [[ ${#nonce} -lt 8 ]]; then
  echo "ERROR: benzersiz P25_LIVE_ARM_NONCE ver (en az 8 karakter)." >&2
  echo "Ornek: P25_LIVE_ARM_NONCE=xrp-$(date +%Y%m%d-%H%M%S) ./deploy_p25_xrp5m_live_pilot.sh" >&2
  exit 1
fi

if [[ ! -x ./.venv/bin/python ]]; then
  echo "ERROR: .venv bulunamadi." >&2
  exit 1
fi

./deploy_p25.sh

echo "=== ARM XRP:5m ONE-CYCLE LIVE PILOT (CLI compatibility path) ==="
P25_LIVE_ARM_NONCE_VALUE="$nonce" ./.venv/bin/python - <<'PY'
import os
from pathlib import Path
p=Path('.env')
text=p.read_text(encoding='utf-8') if p.exists() else ''
wanted={
 'P25_LIVE_FEATURE_ENABLED':'true',
 'P25_LIVE_ARMED':'true',
 'P25_LIVE_ARM_NONCE':os.environ['P25_LIVE_ARM_NONCE_VALUE'],
 'P25_LIVE_ASSET':'XRP',
 'P25_LIVE_HORIZON':'5m',
 'P25_LIVE_STRATEGY_VERSION':'DEEP_VALUE_25C_5M_DUAL_V1',
 'P25_LIVE_MAX_STAKE_USDC':'1.10',
 'P25_LIVE_MAX_PRICE_DRIFT_PCT':'0.10',
 'P25_LIVE_MAX_LIMIT_PRICE':'0.255',
 'P25_LIVE_LEDGER_PATH':'data/p25_live_direction.sqlite',
 'P25_LIVE_CLOB_HOST':'https://clob.polymarket.com',
 'P25_LIVE_CHAIN_ID':'137',
 'P25_LIVE_GEOBLOCK_URL':'https://polymarket.com/api/geoblock',
 'P25_LIVE_REQUIRE_GEOBLOCK_CLEAR':'true',
 'P25_LIVE_SETTLEMENT_WAIT_SEC':'15',
 'P25_LIVE_SETTLEMENT_POLL_SEC':'0.5',
}
lines=text.splitlines(); seen=set(); out=[]
for line in lines:
    s=line.strip(); replaced=False
    for k,v in wanted.items():
        if s.startswith(k+'='):
            out.append(f'{k}={v}'); seen.add(k); replaced=True; break
    if not replaced: out.append(line)
for k,v in wanted.items():
    if k not in seen: out.append(f'{k}={v}')
p.write_text('\n'.join(out).rstrip()+'\n', encoding='utf-8')
PY
chmod 600 .env

# Reuse the already-provisioned arbitrage secret file first; P2.5 settings are
# sourced second. Secret values are never printed.
set -a
if [[ -f .env.p3 ]]; then
  # shellcheck disable=SC1091
  source ./.env.p3
fi
# shellcheck disable=SC1091
source ./.env
set +a

if [[ -z "${POLYMARKET_PRIVATE_KEY:-${PK:-}}" ]]; then
  echo "ERROR: POLYMARKET_PRIVATE_KEY/PK yok; .env.p3 kontrol et." >&2
  exit 1
fi
sig="${POLYMARKET_SIGNATURE_TYPE:-3}"
if [[ "$sig" != "0" && -z "${POLYMARKET_FUNDER:-${POLYMARKET_DEPOSIT_WALLET:-${POLYMARKET_WALLET:-}}}" ]]; then
  echo "ERROR: signatureType=$sig icin POLYMARKET_FUNDER/deposit wallet gerekli." >&2
  exit 1
fi

./.venv/bin/python - <<'PY'
import json, os, urllib.request
url=os.environ.get('P25_LIVE_GEOBLOCK_URL','https://polymarket.com/api/geoblock')
req=urllib.request.Request(url,headers={'User-Agent':'WhaleSignal-P25-XRP5m-Deploy/2.0'})
with urllib.request.urlopen(req,timeout=5) as r:
    p=json.loads(r.read().decode('utf-8'))
print('geoblock_country=',p.get('country'),'blocked=',p.get('blocked'))
if not isinstance(p,dict) or p.get('blocked'):
    raise SystemExit('ERROR: jurisdiction blocked / invalid geoblock response')
PY

pkill -f 'python.*p25_main.py' 2>/dev/null || true
sleep 2
nohup ./.venv/bin/python p25_main.py > engine.log 2>&1 &
pid=$!
echo "$pid" > direction-engine.pid
sleep 3
if ! kill -0 "$pid" 2>/dev/null; then
  echo "ERROR: LIVE pilot process baslatilamadi" >&2
  tail -n 150 engine.log >&2 || true
  exit 1
fi

curl -fsS --connect-timeout 1 --max-time 45 http://127.0.0.1:8091/api/state > /tmp/p25-xrp-live-state.json
./.venv/bin/python - <<'PY'
import json
p=json.load(open('/tmp/p25-xrp-live-state.json',encoding='utf-8'))
live=p.get('xrp5m_live_pilot') or {}
print('xrp5m_live_scope=',live.get('scope'))
print('armed=',live.get('armed'),'consumed=',live.get('arm_consumed'))
print('max_stake=',live.get('max_stake_usdc'),'max_drift=',live.get('max_price_drift_pct'))
assert live.get('feature_enabled') is True
assert live.get('armed') is True
assert live.get('scope') == 'XRP:5m'
assert live.get('one_cycle_per_arm') is True
assert abs(float(live.get('max_stake_usdc') or 0)-1.10) < 1e-9
assert abs(float(live.get('max_price_drift_pct') or 0)-0.10) < 1e-9
PY

echo "XRP:5m LIVE PILOT ARMED | notional<=1.10 USDC | price_drift<=10% | FOK | one network cycle per arm nonce=$nonce"
