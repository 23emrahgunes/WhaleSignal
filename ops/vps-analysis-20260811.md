# PM-Edge VPS statistical snapshot — 2026-08-11 13:17 UTC

Source: public VPS APIs fetched from `http://34.107.85.209` by GitHub Actions run 31495350951. Snapshot health: `OK`.

## 1. Portfolio summary

| Metric | BTC 5m | BTC 15m |
|---|---:|---:|
| Total trades | 81 | 42 |
| Settled | 78 | 42 |
| Open | 3 | 0 |
| Wins / Losses | 72 / 6 | 32 / 10 |
| Win rate | 92.31% | 76.19% |
| Realized PnL | -$3.1253 | -$16.1042 |
| Settled stake | $195 | $105 |
| Return on stake | -1.60% | -15.34% |
| Average model probability | 83.45% | 81.76% |
| Actual win probability | 92.31% | 76.19% |
| Brier score (model) | 0.0871 | 0.1826 |

The comparison API reports a 13.73 percentage-point average-return advantage for 5m, z=1.674, so the current difference is not yet separated at the two-sided 95% threshold (`|z| >= 1.96`). Practical performance nevertheless strongly favors 5m in this sample.

## 2. Payoff economics: high win rate is not enough

For settled 5m trades, average effective cost per share (`stake / shares`) is 0.9383. Average winning PnL is only +$0.1649 while every full loss costs -$2.50. The implied break-even win rate is approximately 93.81%; observed win rate is 92.31%, hence negative PnL despite 72 wins out of 78.

For 15m, average effective cost per share is 0.9001, average winning PnL is +$0.2780, and break-even win rate is approximately 89.99%. Observed 76.19% is far below break-even.

Average raw model probability is also below average effective execution cost: 5m 0.8345 vs 0.9383; 15m 0.8176 vs 0.9001. On a raw model-EV calculation, 74/78 settled 5m trades and 34/42 settled 15m trades had negative model expected value at the execution price. This proves the current entry engine is primarily a direction/confidence filter, not an economic-edge filter.

## 3. Model probability calibration versus market price

5m probability buckets:

- 70–80%: n=21, actual win 95.2%, PnL +$1.0923.
- 80–90%: n=48, actual win 95.8%, PnL +$2.4312.
- 90–100%: n=9, actual win 66.7%, PnL -$6.6488.

The 90–100% model tail is the single largest warning in the current 5m sample. Excluding only `entryProbability >= 0.90` would leave 69 trades, 95.65% win rate, +$3.5235 PnL and +2.04% return on stake. This is an in-sample diagnostic, not yet a production rule.

15m probability buckets are all negative in aggregate:

- 70–80%: n=19, actual 78.9%, PnL -$6.5932.
- 80–90%: n=17, actual 70.6%, PnL -$7.7182.
- 90–100%: n=6, actual 83.3%, PnL -$1.7928.

Using effective market cost as a probability benchmark, 5m market-price Brier is about 0.0690 versus model Brier 0.0871, so the market price forecast beats the model on this 5m sample. For 15m, model Brier 0.1826 is slightly better than market-price Brier 0.1923, but that forecast advantage is not large enough to overcome expensive entries.

## 4. Composite confidence

5m:

- confidence 55–60: n=50, win 96.0%, PnL +$3.6907, ROI +2.95%.
- 60–65: n=17, win 88.2%, PnL -$3.0759.
- 65–70: n=5, win 80.0%, PnL -$1.7594.
- >=70: n=6, win 83.3%, PnL -$1.9807.

Higher composite confidence is not monotonic with profitability in the 5m sample. The strongest composite scores are currently more dangerous, not safer. This suggests order-flow/technical contributions can amplify directional conviction without improving economic expectancy.

15m:

- 55–60: n=27, win 70.4%, PnL -$15.3865.
- 60–65: n=4, win 100%, PnL +$1.0187.
- 65–70: n=8, win 75%, PnL -$2.2578.
- >=70: n=3, win 100%, PnL +$0.5214.

The 15m bucket sample sizes above 60 are too small to set production thresholds.

## 5. Entry timing

5m:

- <60s: n=10, 90.0% win, PnL -$2.2353.
- 60–120s: n=21, 100% win, PnL +$2.5654, ROI +4.89%.
- 120–180s: n=22, 90.9% win, PnL -$1.7591.
- 180–240s: n=25, 88.0% win, PnL -$1.6964.

The current sample strongly points to 60–120 seconds as the most promising 5m entry zone. It is still only 21 settled trades, so this should be shadow-tested rather than immediately declared optimal.

15m timing is unstable rather than monotonic. The 360–480s and 480–600s groups are especially poor, while 600–720s is positive. This suggests the short-horizon features do not transfer cleanly to a 15-minute expiry by simply multiplying the 5m timing window by three.

## 6. Side asymmetry

5m DOWN: n=35, win 94.29%, PnL +$0.0777 (approximately flat).
5m UP: n=43, win 90.70%, PnL -$3.2030.

15m DOWN: n=17, win 70.59%, PnL -$8.7914.
15m UP: n=25, win 80.0%, PnL -$7.3127.

There is no evidence yet that 15m has a profitable direction. On 5m, DOWN is materially healthier than UP in this sample.

## 7. Hedge engine

Hedge stats are zero for both timeframes: no hedge has opened or settled.

The recent history available through the API covers 27 recent 5m trades and 10 recent 15m trades. Replaying the pre-quote hedge gates on this retained history:

- 5m: only one trade ever produced a reverse decision inside the 20–120s hedge window; it reached reverse probability >=65% but never reached the required reverse PTB-Z confirmation. No recent 5m trade passed the full pre-quote reverse-regime gate.
- 15m: one of 10 recent trades passed the full pre-quote persistence/probability/PTB-Z/EWMA gate.

Thus the hedge engine is currently extremely selective/dormant. Zero hedges is not evidence of a broken hedge write path by itself; the reverse-regime gates rarely qualify.

## 8. Data-feed / research-data quality

Recent signal history contains 5,126 5m rows and 4,874 15m rows. Every retained row reports composite source `CHAINLINK_RTDS+BINANCE_REST+BINANCE_REST_DEPTH20`.

Within each market, median signal cadence is about 2 seconds. For 5m, 9.66% of consecutive rows have a gap >3s, 3.11% >5s, and max observed gap is 86s. For 15m, 12.60% are >3s, 4.75% >5s, and max gap is 134s. These long holes matter for a short-horizon strategy and for hedge detection.

The history API shows `depthSource=""`, `depthFresh=false`, and zero depth notional/range fields even when `weightedImbalance` is present. Repository inspection shows this is a persistence-schema issue: the SQLite signal insert/read path stores legacy volume/imbalance fields and forecast diagnostics, but does not persist the newer Depth20 source/freshness/notional/range diagnostics or `forecastReady`. Therefore historical depth metadata cannot currently be trusted even though live order-flow computation may still be valid.

## 9. Settlement integrity issue

At snapshot time 13:17 UTC the 5m portfolio reports three OPEN trades. Only one belongs to the current market ending 13:20. Two stale trades ended at 13:05 and 13:10 UTC but remain OPEN. Their combined $5 stake is still counted as open stake.

The Chainlink client records a boundary anchor only when an RTDS tick is within a ±3-second grace around the exact 5-minute boundary. Paper settlement calls `BoundaryPrice(endTime)` and simply waits if that anchor is missing. There is currently no official-market-outcome fallback. A brief RTDS gap around an exact boundary can therefore leave a paper trade OPEN indefinitely and distort cash/equity/open-stake statistics.

## 10. Analyst priority order

1. **Fix settlement integrity first.** Add an official resolved-market outcome fallback when the exact Chainlink boundary anchor is unavailable, and auto-reconcile stale OPEN paper trades.
2. **Fix research persistence.** Persist Depth20 source/freshness/notional/range/spread fields and forecast-ready status so historical feature studies are valid.
3. **Instrument feed uptime.** Track WS-vs-REST usage, evaluation availability, gap duration and rejection reason per timeframe. The current retained history is entirely REST-labelled and contains large data holes.
4. **Do not optimize on win rate.** Add effective CLOB cost / break-even probability / model-minus-market edge to every entry record and dashboard gate.
5. **Keep 5m as the primary experiment.** Current 5m economics are close to break-even and much better calibrated than 15m; 15m is currently materially negative.
6. **Shadow-test, do not immediately hard-code, the two strongest 5m hypotheses:** entry probability below 90%, and entry timing 60–120 seconds. Both look good in-sample but need more out-of-sample trades.
7. **Do not relax the hedge yet.** First repair data continuity and settlement. Then log hedge gate rejection counts to determine whether thresholds are too strict or whether true reverse regimes are simply rare.
