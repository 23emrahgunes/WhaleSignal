# Polymarket Whale Intelligence Engine

This repository provides a comprehensive engine to discover, enrich, and rank "stable" wallets and whales on Polymarket. It is an intelligence engine designed for stability tracking and watchlist generation.

## Milestone 2: Continuous Intelligence

Milestone 2 transforms the engine from a static reporter into a continuous tracking system:
- **Persistent History**: Daily snapshots of wallet scores and tiers stored in `data/history/`.
- **Trend Analysis**: Calculation of score stability and volatility over time.
- **Transition Tracking**: Automatic detection of `Rising`, `Dropped`, `Stale`, `Upgraded`, and `Downgraded` wallets.
- **Bot-Ready Watchlists**: Automated production of `Core`, `Emerging`, `Probation`, and Category-specific watchlists in JSON and CSV formats.

## Project Structure

- `src/`: Core logic and API clients.
  - `persistence.py`: Manages historical snapshots.
  - `wallet_transitions.py`: Analyzes changes between snapshots.
  - `wallet_ranker.py`: Categorizes expertise and generates watchlists.
- `scripts/`: Execution scripts for the daily pipeline.
- `data/history/`: Persistent storage for daily snapshots.
- `reports/watchlists/`: Bot-consumable JSON/CSV files.

## Daily Pipeline Workflow

Run the following scripts in sequence to update the intelligence engine and publish new watchlists:

1. **Market & Wallet Discovery** (Fast Scan):
   ```bash
   export PYTHONPATH=$PYTHONPATH:.
   python3 scripts/run_fast_scan.py
   ```

2. **Wallet Enrichment**:
   ```bash
   python3 scripts/run_enrichment.py
   ```

3. **Daily Rescore** (Calculates scores and persists to history):
   ```bash
   python3 scripts/run_daily_rescore.py
   ```

4. **Transition Analysis** (Detects Rising/Dropped/Tier changes):
   ```bash
   python3 scripts/run_transitions.py
   ```

5. **Publish Watchlists** (Generates bot-ready JSON/CSV outputs):
   ```bash
   python3 scripts/publish_watchlists.py
   ```

## Watchlist Definitions

- **Core Watchlist**: Long-term stable A-tier wallets. Highest conviction for following.
- **Emerging Watchlist**: Wallets with rapidly rising scores or recent upgrades.
- **Probation Watchlist**: Previously high-quality wallets showing signs of decay, downgrades, or staleness.
- **Category Watchlists**: Top wallets filtered by domain expertise (Crypto, Sports, Politics, Other).
