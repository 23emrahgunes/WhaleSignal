# Polymarket Whale Intelligence Engine

This repository provides a comprehensive engine to discover, enrich, and rank "stable" wallets and whales on Polymarket. It is an intelligence engine designed for stability tracking and watchlist generation.

## Milestone 2 Capabilities

Milestone 2 transforms the engine from a static reporter into a continuous tracking system:
- **Persistent History**: Daily snapshots of wallet scores and tiers.
- **Trend Analysis**: Calculation of score stability and volatility over 7/30/90 days.
- **Transition Tracking**: Automatic detection of Rising, Dropped, Stale, Upgraded, and Downgraded wallets.
- **Specialized Watchlists**: Automated production of Core, Emerging, Probation, and Category-specific (Crypto, Sports, Politics) watchlists for trader bots.

## Project Structure

- `src/`: Core logic and API clients.
  - `persistence.py`: Manages historical snapshots.
  - `wallet_transitions.py`: Analyzes changes between snapshots.
  - `wallet_ranker.py`: Categorizes expertise and generates watchlists.
- `scripts/`: Execution scripts.
- `data/history/`: Persistent storage for daily snapshots.
- `reports/watchlists/`: Target for bot-consumable JSON/CSV files.

## Setup

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Usage (Daily Pipeline)

Run the following scripts in sequence to update the intelligence engine:

1. **Market & Wallet Discovery**:
   ```bash
   export PYTHONPATH=$PYTHONPATH:.
   python3 scripts/run_fast_scan.py
   ```

2. **Deep Enrichment**:
   ```bash
   python3 scripts/run_enrichment.py
   ```

3. **Scoring with History**:
   ```bash
   python3 scripts/run_daily_rescore.py
   ```

4. **Transition Analysis**:
   ```bash
   python3 scripts/run_transitions.py
   ```

5. **Publish Watchlists**:
   ```bash
   python3 scripts/publish_watchlists.py
   ```

## Watchlist Definitions

- **Core Watchlist**: Long-term stable A-tier wallets. Highest conviction.
- **Emerging Watchlist**: Wallets with rapidly rising scores or recent upgrades to A/B tier.
- **Probation Watchlist**: High-quality wallets showing signs of decay or downgrades.
- **Category Watchlists**: Top-ranked wallets filtered by their specific domain expertise (e.g., Politics experts).

## Technical Scoring (v2)

The engine applies weighted sub-scores and history-based modifiers:
- **Stability Bonus**: Extra points for wallets with a steady score trend.
- **Volatility Penalty**: Deductions for wallets with erratic performance.
- **Concentration Penalty**: Applied to wallets over-reliant on single markets.
- **Followability**: Real-time metric assessing liquidity and entry detectability.
