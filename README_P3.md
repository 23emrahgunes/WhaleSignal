# P3 Arbitrage Lab — Structural Complete-Set Research

P3 is an isolated, model-free structural-arbitrage research layer built on top of
P2.6 public CLOB book and fee persistence. It is **SHADOW/PAPER ONLY**.

## Scope

- **P3.0** — isolated `data/p3_arbitrage.sqlite`, opportunity/window/replay schema.
- **P3.1** — equal-share `UP + DOWN` BUY→MERGE scanner using full-depth VWAP and dynamic fees.
- **P3.2** — collateral SPLIT→SELL both outcomes reverse-parity scanner.
- **P3.3** — contiguous opportunity lifetime windows and peak profitability.
- **P3.4** — delayed 10/25/50/100/200/500ms two-leg FOK replay, one-leg exposure and unwind loss.
- **P3.5** — read-only dashboard/API on `127.0.0.1:8093` and systemd deployment.
- **P3.6** — independent-window DRY bankroll, first-print survival confirmation and readiness gates.

## Structural formulas

### BUY both outcomes and MERGE

```text
net_profit(q) = q
              - buy_cost_UP(q)
              - buy_cost_DOWN(q)
              - fee_UP(q)
              - fee_DOWN(q)
              - execution_buffer(q)
```

### SPLIT collateral and SELL both outcomes

```text
net_profit(q) = sell_proceeds_UP(q)
              + sell_proceeds_DOWN(q)
              - fee_UP(q)
              - fee_DOWN(q)
              - q
              - execution_buffer(q)
```

The scanner optimizes over equal-share depth breakpoints. It never uses P2.6 fair
value or direction predictions to decide whether structural parity exists.

## Non-atomic execution research

Two separate FOK legs are **not** assumed atomic. P3.4 replays historical books at
configurable submission delays and records:

- both-leg completion,
- one-leg exposure,
- immediate unwind price/fee/loss,
- cycle PnL,
- pair completion rate by delay.

Current-market future books are used only ex-post in replay and never feed back
into opportunity detection.

## P3.6 entry-survival policy

Observation count is not trade count. A single structural-arbitrage window can be
observed many times while it remains open, so observation-level pair-fill statistics
can be highly optimistic.

The DRY policy therefore permits at most one simulated attempt per independent
window and no longer assumes the first positive print is tradable. With the default:

```text
P3_DRY_ENTRY_CONFIRM_MS=250
```

the window must remain structurally positive for at least one default scanner cycle.
The selected DRY entry is the first still-positive opportunity at or after:

```text
window.opened_ts_ms + P3_DRY_ENTRY_CONFIRM_MS
```

If a closed window dies before that point it is recorded as
`SKIPPED_CONFIRMATION`. An open window that has not yet produced a surviving print
is `PENDING_CONFIRMATION`.

The dashboard also evaluates a same-data comparison grid:

```text
P3_DRY_SURVIVAL_DELAYS_MS=0,50,100,200,250,500
```

`0ms` is retained as the old first-print baseline. Each row reports independent
attempt count, confirmation survival, pair completion, one-leg rate, cumulative
PnL and maximum drawdown. This grid is research evidence only and never changes
live execution because P3 has no live execution path.

## Deployment

```bash
cd ~/direction-engine
git pull --ff-only origin direction-engine
bash deploy_p3.sh --no-pull
bash scripts/status_p3.sh
```

Dashboard/API locally:

```text
http://127.0.0.1:8093/
http://127.0.0.1:8093/health
http://127.0.0.1:8093/api/summary
```

## Safety

P3 has no private-key setting, signing dependency, order constructor, order
submission or live-execution route. `scripts/stop_p3.sh` removes only the P3
service and preserves P2.5, P2.6 and all databases.
