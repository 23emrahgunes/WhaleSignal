import json
import os
from src.wallet_ranker import WalletRanker
from src.reports import ReportGenerator

def main():
    print("Exporting Reports...")

    if not os.path.exists("reports/scored_wallets.json") or not os.path.exists("reports/enriched_wallets.json"):
        print("Scored or Enriched wallet data not found.")
        return

    with open("reports/scored_wallets.json", "r") as f:
        scored_data = json.load(f)
    with open("reports/enriched_wallets.json", "r") as f:
        enriched_data = json.load(f)

    ranker = WalletRanker(scored_data, enriched_data)
    generator = ReportGenerator(scored_data)

    generator.generate_all_reports(ranker)
    print("Reporting complete.")

if __name__ == "__main__":
    main()
