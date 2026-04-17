import os
import pandas as pd
from src.market_census import MarketCensus
from src.wallet_census import WalletCensus
from src.wallet_enrichment import WalletEnrichment
import json

def main():
    print("Running Wallet Enrichment Pipeline...")

    # 1. Load or discover markets
    if os.path.exists("reports/market_universe.csv"):
        markets_df = pd.read_csv("reports/market_universe.csv")
        # tokens need to be converted back to list from string
        markets_df['tokens'] = markets_df['tokens'].apply(lambda x: json.loads(x.replace("'", '"')))
    else:
        census = MarketCensus()
        census.fetch_active_markets(limit=250)
        markets_df = census.get_market_universe()
        markets_df.to_csv("reports/market_universe.csv", index=False)

    # 2. Discover Wallets (using subset for speed in this demo/milestone if needed)
    wallet_census = WalletCensus()
    # To be faster for first run, only take top 50 markets by volume
    top_markets = markets_df.sort_values("volume", ascending=False).head(50)
    wallets = wallet_census.discover_wallets(top_markets, trades_per_market=50)
    print(f"Discovered {len(wallets)} unique wallets.")

    # 3. Enrich Wallets
    enricher = WalletEnrichment()
    # For the first milestone, maybe we limit to top 100 discovered for speed
    enriched_data = enricher.enrich_wallets(wallets[:100], markets_df)

    # 4. Save Enrichment results
    with open("reports/enriched_wallets.json", "w") as f:
        json.dump(enriched_data, f, indent=2)

    print(f"Enriched {len(enriched_data)} wallets. Saved to reports/enriched_wallets.json")

if __name__ == "__main__":
    main()
