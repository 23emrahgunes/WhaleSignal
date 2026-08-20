# P2.6 AWS Deployment Runbook

P2.6 is an isolated SHADOW/PAPER research challenger. It does not replace or stop
P2.5, does not write to the P2.5 SQLite database, and contains no private key,
signing, order submission or live execution path.

## One-shot deployment

Run from the existing VPS repository:

```bash
cd ~/direction-engine
git checkout direction-engine
git pull --ff-only origin direction-engine
chmod +x deploy_p26.sh scripts/*.sh
./deploy_p26.sh
```

The deployment performs the following sequence:

1. Requires a clean Git worktree and updates `direction-engine`.
2. Requires P2.5 `/health`, `/api/state` and `/api/paper-summary` to return HTTP 200.
3. Creates `.env.p26` from `.env.p26.example` when absent.
4. Validates P2.5/P2.6 database separation.
5. Runs Python syntax checks and the complete regression suite.
6. Creates and verifies a WAL-aware P2.5 baseline freeze.
7. Generates systemd units using the actual repository path and owner.
8. Starts the independent RTDS oracle and canonical dataset sidecars.
9. Waits for a real Chainlink RTDS tick to be persisted in
   `data/p26_research.sqlite`.
10. Rechecks that P2.5 remains healthy.

The script never applies firewall changes automatically.

## Status

```bash
scripts/status_p26.sh
scripts/status_p26.sh --json
```

Important early-stage values:

- `oracle_ticks` should rise shortly after startup.
- `canonical_rows` can initially be zero; rows appear only as new markets cross
  T-60/T-240/T-600 with acceptable lineage.
- `eligible_rows` requires complete no-future source lineage.
- `official_labels` rise only after official market resolution.

## Rollback

```bash
scripts/stop_p26.sh
```

This stops only P2.6 sidecars. It does not stop P2.5, restore/delete a database or
change firewall rules. To also remove the P2.6 unit files:

```bash
scripts/stop_p26.sh --remove-units
```

## Port 8091 security

Use the AWS Security Group as the primary perimeter control. Restrict TCP/8091 to
your trusted public IP before applying host firewall rules. The repository script
is dry-run-first:

```bash
P26_AUTHORIZED_CIDR='YOUR.PUBLIC.IP/32' sudo scripts/harden_port_8091.sh --dry-run
P26_AUTHORIZED_CIDR='YOUR.PUBLIC.IP/32' sudo scripts/harden_port_8091.sh --apply
```

The apply mode starts a watchdog. Verify authorized access immediately, then:

```bash
sudo scripts/harden_port_8091.sh --confirm
```

Without confirmation, the watchdog invokes the rollback script automatically.

## Data/training sequence

Deployment does not claim edge or train a model immediately. The empirical order is:

```text
Oracle ticks
→ canonical rows
→ official labels
→ frozen external-only model
→ purged nested OOS evaluation
→ OOS calibration buckets
→ latency/depth/alpha-aware Paper V2
→ promotion or rejection report
```

Until sufficient canonical/OOS/Paper V2 evidence exists, promotion remains
`NOT_READY`.
