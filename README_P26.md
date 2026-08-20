# Direction Engine P2.6 — Isolated Quant Research Challenger

P2.6 does **not** replace P2.5. It keeps the current `p25_*` SHADOW/PAPER
runtime as a rollback baseline and adds only `p26_*` research modules plus a
separate SQLite database (`data/p26_research.sqlite`). No private key, signing,
order submission or live execution exists.

## Phase map

1. **P2.6.0** — consistent P2.5 baseline freeze, SHA-256 manifest, dry-run-first
   port hardening and rollback scripts.
2. **P2.6.1** — independent Chainlink RTDS sidecar, persistent oracle ticks,
   restart recovery and one canonical row per market (5m T-60, 15m T-240,
   1h T-600) with explicit source lineage.
3. **P2.6.2** — external-only fair value champion: median imputation + frozen
   `RobustScaler` + L2 `LogisticRegression`; Polymarket/CLOB features are
   forbidden from the feature contract.
4. **P2.6.3** — overlapping-horizon temporal clusters, purged/embargoed nested
   walk-forward, latency mismatch metrics and alpha-decay replay.
5. **P2.6.4** — past-only OOS Platt calibration, fixed reliability buckets and
   full Wilson lower/upper bounds. DOWN lower bound is `1 - UP upper`.
6. **P2.6.5** — depth-VWAP execution, fee in cost-per-share units,
   ghost/transient-liquidity risk vetoes, sequence-gap/freshness gates,
   forecast-to-fill latency and alpha TTL, isolated `RESEARCH_PAPER_V2` records.
7. **P2.6.6** — strict paper-only promotion/rejection using paired market
   baselines, temporal-block bootstrap, drawdown, fold stability and
   asset/horizon concentration. Highest state: `VALIDATED_PAPER_MODEL`.

## Local verification

```bash
python -m py_compile *.py reference/*.py
pytest -q
```

## Baseline freeze

```bash
python p26_baseline_freeze.py freeze
python p26_baseline_freeze.py verify data/backups/<baseline>/p26_freeze_manifest.json
```

The freeze command fails closed on a dirty worktree unless explicitly overridden.
It uses SQLite's online backup API and never restores or mutates the P2.5 DB.

## Sidecars

```bash
python p26_oracle_daemon.py
python p26_dataset_daemon.py
```

Systemd templates live under `deploy/`. Live AWS acceptance is separate from CI:
CI can prove syntax, deterministic algorithms and dry-run safety, but cannot claim
that an AWS Security Group blocks unauthorized public traffic or that RTDS/CLOB
feeds are healthy on the target VPS.

## AWS one-shot deployment

The rollback-safe AWS runbook and one-shot deployment command are documented in
[`README_P26_AWS.md`](README_P26_AWS.md):

```bash
cd ~/direction-engine
git checkout direction-engine
git pull --ff-only origin direction-engine
chmod +x deploy_p26.sh scripts/*.sh
./deploy_p26.sh
```

The deployment keeps P2.5 running, creates a verified baseline freeze, installs
dynamic systemd units and fails unless a real RTDS tick is persisted. Port 8091
hardening remains an explicit post-deployment operation and is never applied
implicitly.

## Training and evaluation

```bash
python p26_train.py
python p26_eval.py
python p26_report.py
```

No edge is claimed unless the report state reaches `VALIDATED_PAPER_MODEL`. Even
that state remains PAPER-only and does not enable execution.
