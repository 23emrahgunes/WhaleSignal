import os
from src.market_census import MarketCensus

def main():
    print("Running Fast Scan - Market Discovery...")
    census = MarketCensus()
    markets = census.fetch_active_markets(limit=250)
    df = census.get_market_universe()

    os.makedirs("reports", exist_ok=True)
    df.to_csv("reports/market_universe.csv", index=False)
    print(f"Discovered {len(markets)} markets. Saved to reports/market_universe.csv")

if __name__ == "__main__":
    main()
