import os, json, pandas as pd
from src.wallet_census import WalletCensus
from src.wallet_enrichment import WalletEnrichment
def main():
    m = pd.read_csv("reports/market_universe.csv")
    m["tokens"] = m["tokens"].apply(lambda x: json.loads(x.replace("'", '"')))
    wc = WalletCensus()
    wallets = wc.discover_wallets(m.sort_values("volume", ascending=False).head(50))
    e = WalletEnrichment()
    res = e.enrich_wallets(wallets[:100], m)
    with open("reports/enriched_wallets.json", "w") as f: json.dump(res, f, indent=2)
if __name__ == "__main__": main()
