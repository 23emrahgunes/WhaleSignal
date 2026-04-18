import json, os
from src.wallet_ranker import WalletRanker
from src.reports import ReportGenerator
def main():
    if not os.path.exists("reports/scored_wallets.json"): return
    with open("reports/scored_wallets.json", "r") as f: scored = json.load(f)
    with open("reports/enriched_wallets.json", "r") as f: enriched = json.load(f)
    trans = {"new_wallets": [], "rising_wallets": [], "dropped_wallets": [], "stale_wallets": [], "upgraded_wallets": [], "downgraded_wallets": []}
    if os.path.exists("reports/wallet_transitions.json"):
        with open("reports/wallet_transitions.json", "r") as f: trans = json.load(f)
    r = WalletRanker(scored, enriched); g = ReportGenerator(); g.generate_all_reports(r, trans)
if __name__ == "__main__": main()
