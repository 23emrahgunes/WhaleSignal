# Direction Engine P2.6 — Isolated Quant Research Challenger

P2.6 does **not** replace P2.5. It reads P2.5 data in read-only mode, writes a
separate `data/p26_research.sqlite` database and stays SHADOW/PAPER-only.

## Data and decision architecture

```text
P2.5 SQLite (read-only)
  → incremental canonical T-60 / T-240 / T-600 rows
Chainlink RTDS
  → persistent oracle lineage
Public CLOB V2 market info + market WebSocket
  → UP/DOWN full-depth books + dynamic fee lineage
Frozen external-only model
  → chronological OOS calibration + Wilson side bounds
Frozen past-OOS alpha artifact
  → latency/liquidity/fee/portfolio gates
  → RESEARCH_PAPER_V2 OPEN or deterministic SKIPPED
```

The fair-value model contains no Polymarket price or CLOB feature. CLOB data is
used only for executable depth/VWAP, fee, freshness, liquidity risk and vetoes.

## P2.6.5 safeguards

- Current-market future books never influence entry.
- Fill-after book replay is ex-post analytics only.
- Empty alpha history returns `ALPHA_PROFILE_MISSING`, not an exception.
- Exactly one of UP/DOWN may pass; dual-positive edge is an integrity failure.
- Fee-enabled markets require public fee metadata; missing metadata fails closed.
- `PER_COMBO` is the only initially approved calibration/alpha scope for OPEN.
- Fixed stake only; bankroll, exposure, overlap, daily loss, drawdown, cooldown
  and global kill-switch gates are enforced.
- Paper V2 is disabled by default.

## Sidecars

```bash
python p26_oracle_daemon.py
python p26_dataset_daemon.py
python p26_book_daemon.py
python p26_paper_v2_daemon.py   # idle while P26_PAPER_V2_ENABLED=false
```

## Local verification

```bash
python -m py_compile *.py reference/*.py
python -m compileall -q .
bash -n deploy_p26.sh scripts/*.sh
pytest -q
```

## Training / OOS / alpha artifacts

```bash
python p26_train.py
python p26_eval.py
python p26_alpha_train.py --cutoff-ms <UTC_MS>
python p26_report.py
```

No edge is claimed merely because an artifact can be trained. Promotion remains
`NOT_READY` until pre-registered independent OOS and Paper V2 evidence passes.
