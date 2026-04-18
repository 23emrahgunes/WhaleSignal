# Polymarket Whale Intelligence Engine

Polymarket Whale Engine scans Polymarket markets to discover leaderboard-independent, stable, followable, high-quality wallets. The repo is not for trade execution; it is an intelligence engine for wallet discovery, scoring, ranking, and watchlist generation.

## Milestone 2 Capabilities
- Persistent daily snapshots of wallet scores and tiers.
- Trend analysis with stability and volatility tracking.
- Transition tracking for Rising, Dropped, Stale, Upgraded, and Downgraded wallets.
- Automated Core, Emerging, Probation, and category-specific watchlists.

## Project Structure
- src/: core logic and API clients.
- scripts/: daily pipeline scripts.
- data/history/: persistent snapshot storage.
- reports/watchlists/: bot-consumable JSON/CSV outputs.

## Daily Pipeline
1. python3 scripts/run_fast_scan.py
2. python3 scripts/run_enrichment.py
3. python3 scripts/run_daily_rescore.py
4. python3 scripts/run_transitions.py
5. python3 scripts/publish_watchlists.py
