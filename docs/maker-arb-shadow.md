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


## Paper executor
`ARB_PAPER_ENABLED=true` adds a separate maker-arbitrage paper portfolio. It never signs or submits an order. A resting maker BUY is counted as filled only when a later public CLOB snapshot moves strictly through its limit and exposes at least the full configured order size in ask liquidity below that limit. A mere touch is not a fill. After one leg fills, only the opposite leg may be repriced, never above the economic completion ceiling and never through the current ask. If the second leg is not completed before the stranded timeout/market-end guard, the cycle is closed using the filled leg's current best bid as a conservative mark-to-market exit estimate.

Endpoints: `/api/arb/paper/stats?tf=5m|15m` and `/api/arb/paper/cycles?tf=5m|15m`.
\n\n## Safe-first queue-aware paper model\nThe paper executor no longer pretends that a resting BUY fills when a REST ask snapshot crosses its limit. It posts only the PTB/risk-selected first leg, observes public Polymarket `last_trade_price` SELL executions from the market WebSocket, debits displayed price-time queue ahead, supports partial fills, and only activates the opposite completion order after the first leg is fully filled. Trades from the batch that completed the first leg cannot retroactively fill the second leg. A WebSocket data gap invalidates the cycle instead of inventing PnL. `ARB_PAPER_MIN_EDGE` controls research sampling separately from the future-live `ARB_TARGET_EDGE`.\n