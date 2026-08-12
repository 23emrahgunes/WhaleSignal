# Maker Arb Paper live verification — 2026-08-12 00:07 UTC

Verification source: GitHub Actions run `31549043672`, querying public VPS `http://34.107.85.209`.

## Service/UI
- `/health` => `OK`
- Dashboard contains `Arbitraj Paper Bakiyesi`, confirming the new paper UI is deployed.

## 5m live arb
- market: `btc-updown-5m-1786493100`
- status: `BLOCKED`
- reason: `PAIR_EDGE_BELOW_TARGET`
- UP best bid/ask: `0.12 / 0.13`
- DOWN best bid/ask: `0.87 / 0.88`
- maker prices: `0.12 + 0.87 = 0.99`
- gross edge: `0.01`
- operational buffer: `0.002`
- net edge: `0.008` (0.8%)
- target edge: `0.02` (2%)
- min order size: `5 / 5`
- paper order size: `5`
- max stranded shares: `5`
- PTB ready: `true`
- PTB decision: `DOWN`
- PTB P(UP)/P(DOWN): `0.2719655542 / 0.7280344458`
- preferred first leg: `UP`
- pair book fetch: `26 ms`

5m arb-paper stats immediately after deployment:
- cash balance: `1000`
- total cycles: `0`
- open cycles: `0`
- completed cycles: `0`
- net paper PnL: `0`

No cycle was opened because the current net edge (0.8%) is below the configured 2% target.

## 15m live arb
- market: `btc-updown-15m-1786492800`
- status: `BLOCKED`
- reason: `PAIR_EDGE_BELOW_TARGET`
- UP best bid/ask: `0.10 / 0.11`
- DOWN best bid/ask: `0.89 / 0.90`
- maker prices: `0.10 + 0.89 = 0.99`
- gross edge: `0.01`
- operational buffer: `0.002`
- net edge: `0.008` (0.8%)
- target edge: `0.02` (2%)
- min order size: `5 / 5`
- paper order size: `5`
- max stranded shares: `5`
- PTB ready: `true`
- PTB decision: `DOWN`
- PTB P(UP)/P(DOWN): `0.0305375383 / 0.9694624617`
- preferred first leg: `DOWN`
- pair book fetch: `24 ms`

15m arb-paper stats immediately after deployment:
- cash balance: `1000`
- total cycles: `0`
- open cycles: `0`
- completed cycles: `0`
- net paper PnL: `0`

No cycle was opened because the current net edge (0.8%) is below the configured 2% target.

## Conclusion
The deployed Maker Arb Paper Executor endpoints/UI are live and the scanner is consuming real CLOB metadata (including dynamic `min_order_size=5`). At this verification instant there was no qualifying >=2% net maker pair edge, so the correct behavior was to leave the paper portfolio untouched rather than fabricate a cycle.
