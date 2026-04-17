import json
import os
from src.wallet_ranker import WalletRanker
from src.reports import ReportGenerator

def main():
    print("Publishing Watchlists...")

    if not os.path.exists("reports/scored_wallets.json") or not os.path.exists("reports/wallet_transitions.json"):
        print("Required data for publishing not found.")
        return

    with open("reports/scored_wallets.json", "r") as f:
        scored_data = json.load(f)
    with open("reports/enriched_wallets.json", "r") as f:
        enriched_data = json.load(f)
    with open("reports/wallet_transitions.json", "r") as f:
        transitions = json.load(f)

    ranker = WalletRanker(scored_data, enriched_data)
    generator = ReportGenerator()

    generator.generate_all_reports(ranker, transitions)
    print("Watchlists published to reports/watchlists/")

if __name__ == "__main__":
    main()
