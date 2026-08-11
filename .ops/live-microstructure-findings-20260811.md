# Live microstructure verification — 2026-08-11 19:52 UTC

Public VPS health returned `OK`. The public microstructure endpoints were queried from GitHub Actions using `http://34.107.85.209`.

At the exact query instant `/api/microstructure?tf=5m`, `/api/microstructure?tf=15m`, and `/api/live?tf=5m` returned `{"status":"waiting_for_data"}` because the request landed during a market-transition/evaluation gap. The microstructure history endpoints contained fresh snapshots from seconds earlier.

## Latest 5m microstructure history snapshot

- timestamp: `2026-08-11T19:52:06.229717584Z`
- slug: `btc-updown-5m-1786477800`
- ready: `true`
- synchronized: `true`
- source: `BINANCE_DEEP_REST1000`
- ageMs: `900`
- bidLevels / askLevels: `1000 / 1000`
- ±$10: bid `$1,002,831.63`, ask `$276,211.36`, imbalance `+0.5681`
- ±$25: bid `$2,676,715.94`, ask `$1,865,322.16`, imbalance `+0.1786`
- ±$50: bid `$4,947,862.42`, ask `$4,663,508.85`, imbalance `+0.0296`
- ±$75: bid `$7,128,206.21`, ask `$6,795,882.82`, imbalance `+0.0239`
- PTB path bid USD: `$999,056.46`
- PTB path ask USD: `$267,786.56`
- PTB beyond USD: `$2,211,079.38`
- PTB barrier score: `+0.7376`
- deep-book score: `+0.1385`
- trade-flow score: `-0.9000`
- microstructure score: `-0.2445`
- shadow Model-B: score `-0.0189`, decision `NEUTRAL`

The executed-trade windows in the last five saved snapshots are suspicious: all show `buyUsd=0` and non-zero `sellUsd`, producing `trade*Imbalance=-1`. This is unlikely to be trusted as a valid directional signal until the aggTrade fallback/parsing/dedup path is independently verified.

## Current model interpretation

The repo does calculate a PTB corridor heuristic. It converts Chainlink PTB to the Binance price axis, then compares resting bid/ask notional on the path between current Binance spot and the PTB, plus 35% weight for liquidity in the first $25 beyond PTB. This produces `PTBBarrierScore` in [-1,+1].

However, this is not yet a calibrated probability model of `P(close above PTB)` / `P(close below PTB)` derived from deep liquidity. It does not explicitly combine remaining seconds, required dollar distance, corridor liquidity consumption rate, aggressive buy/sell volume rate, wall refill/depletion velocity, and expected volume needed to cross/hold PTB into a terminal probability. PTB barrier is only 5% of Shadow Model-B; deep book 20%, trade flow 20%, terminal probability score 45%, technical 10%.

Conclusion: Binance depth collection is live and sufficiently deep, but the user's requested `will the market close above/below PTB based on the liquidity path` model is only partially implemented as a heuristic, not as a dedicated terminal microstructure probability model.
