import json
import os
from src.wallet_ranker import WalletRanker
from src.reports import ReportGenerator


def main():
    if not os.path.exists("reports/scored_wallets.json") or not os.path.exists("reports/enriched_wallets.json"):
        return

    with open("reports/scored_wallets.json", "r") as f:
        scored = json.load(f)
    with open("reports/enriched_wallets.json", "r") as f:
        enriched = json.load(f)

    transitions = {
        "new_wallets": [],
        "rising_wallets": [],
        "dropped_wallets": [],
        "stale_wallets": [],
        "upgraded_wallets": [],
        "downgraded_wallets": [],
    }
    if os.path.exists("reports/wallet_transitions.json"):
        with open("reports/wallet_transitions.json", "r") as f:
            transitions = json.load(f)

    ranker = WalletRanker(scored, enriched)
    generator = ReportGenerator()
    generator.generate_all_reports(ranker, transitions)


if __name__ == "__main__":
    main()
