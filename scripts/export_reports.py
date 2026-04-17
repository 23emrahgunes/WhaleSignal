import json
import os
from src.wallet_ranker import WalletRanker
from src.reports import ReportGenerator

def main():
    print("Exporting Reports (Milestone 1 Compatibility)...")

    if not os.path.exists("reports/scored_wallets.json") or not os.path.exists("reports/enriched_wallets.json"):
        print("Required data for exporting not found.")
        return

    with open("reports/scored_wallets.json", "r") as f:
        scored_data = json.load(f)
    with open("reports/enriched_wallets.json", "r") as f:
        enriched_data = json.load(f)

    # transitions might not exist if run_transitions wasn't called
    transitions = {"new_wallets": [], "rising_wallets": [], "dropped_wallets": [], "stale_wallets": [], "upgraded_wallets": [], "downgraded_wallets": []}
    if os.path.exists("reports/wallet_transitions.json"):
        with open("reports/wallet_transitions.json", "r") as f:
            transitions = json.load(f)

    ranker = WalletRanker(scored_data, enriched_data)
    generator = ReportGenerator()

    generator.generate_all_reports(ranker, transitions)
    print("Reporting complete.")

if __name__ == "__main__":
    main()
