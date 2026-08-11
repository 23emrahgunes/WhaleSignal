# Maker Arb Shadow Engine

This engine is research/shadow-only. It never signs or submits a Polymarket order.

For each active BTC 5m/15m market it reads both outcome CLOB books and uses the market-provided `min_order_size` and `tick_size`.

- Order mode modeled: GTC/GTD + post-only maker.
- Maker fee assumption: 0.
- Base order size: `max(UP.min_order_size, DOWN.min_order_size)`.
- Max stranded inventory: one base unit by default.
- Net pair edge: `1 - upMaker - downMaker - operationalBuffer`.
- Candidate gate: net pair edge >= target edge AND PTB terminal estimate is ready.
- Safe first leg: max PTB stranded EV after exit-risk and model-uncertainty penalty.
- Safer leg may improve one tick above best bid; risky leg remains passive. The engine falls back to both-passive quotes if the queue jump consumes target edge.
- Completion ceiling preserves both target edge and post-only status.

SQLite table `arb_snapshots` stores every evaluated shadow snapshot. APIs:

- `/api/arb?tf=5m|15m`
- `/api/arb/history?tf=...&limit=...`
- `/api/arb/stats?tf=...`
