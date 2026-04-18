import os, json
from src.market_census import MarketCensus
def main():
    c = MarketCensus()
    c.fetch_active_markets(limit=250)
    df = c.get_market_universe()
    df.to_csv("reports/market_universe.csv", index=False)
if __name__ == "__main__": main()
