# P2.6 AWS Deployment Runbook

P2.6 is an isolated SHADOW/PAPER challenger. The deployment does not stop P2.5,
write to the P2.5 database, load credentials, sign or submit orders.

## One-shot deployment

Keep Paper V2 disabled for the first post-merge smoke:

```bash
cd ~/direction-engine
git checkout direction-engine
git pull --ff-only origin direction-engine

touch .env.p26
if grep -q '^P26_PAPER_V2_ENABLED=' .env.p26; then
  sed -i 's/^P26_PAPER_V2_ENABLED=.*/P26_PAPER_V2_ENABLED=false/' .env.p26
else
  echo 'P26_PAPER_V2_ENABLED=false' >> .env.p26
fi
chmod 600 .env.p26

chmod +x deploy_p26.sh scripts/*.sh
./deploy_p26.sh --skip-freeze
./scripts/smoke_p26.sh
```

The deployment installs/restarts four services:

```text
direction-engine-p26-oracle.service
direction-engine-p26-dataset.service
direction-engine-p26-book.service
direction-engine-p26-paper-v2.service
```

The Paper V2 service remains active but idle/fail-closed while the enable flag is
false. This allows deployment and schema health to be tested without creating
Paper V2 attempts.

## Expected smoke result

```text
P2.5 /health = 200
all four P2.6 services = active
P2.6 integrity_check = ok
oracle_ticks > 0
P26_PAPER_V2_ENABLED=false
execution/signing/order submission = false
```

The incremental dataset cursor processes only new canonical snapshot IDs. Label
synchronization is separate, periodic and writes only actual changes; it must not
repeat the old full-history scan every ten seconds.

## Status and rollback

```bash
scripts/status_p26.sh
scripts/status_p26.sh --json
scripts/stop_p26.sh
```

Rollback stops only P2.6 services and preserves both databases. Port 8091 network
hardening remains an explicit, separately confirmed operation.
