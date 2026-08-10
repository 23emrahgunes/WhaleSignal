# Paper Shadow Hedge Model

This branch uses a research-only full-share reverse hedge. It never sends live orders.

A hedge is not triggered by one opposite signal. The default gate requires 6 of the last 8 evaluations in the reverse direction, the last 3 consecutive reverse, EWMA score magnitude at least 0.35, reverse terminal probability at least 65%, aligned PTB z-score of at least 0.50 sigma, 20-120 seconds remaining, and at least 3 percentage points of positive edge after CLOB VWAP, taker-fee and latency assumptions.

Original hold PnL is stored separately from the hedge leg and combined PnL so the hedge can be evaluated as an A/B shadow portfolio.
