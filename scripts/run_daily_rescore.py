import json
import os
from src.wallet_quality import WalletQualityScorer
from src.persistence import PersistenceManager


def main():
    print("Running Wallet Rescoring (Milestone 2)...")

    if not os.path.exists("reports/enriched_wallets.json"):
        print("Enriched wallet data not found. Run enrichment script first.")
        return

    with open("reports/enriched_wallets.json", "r") as f:
        enriched_data = json.load(f)

    pm = PersistenceManager()
    scored_wallets = []

    for wallet in enriched_data:
        history = pm.get_historical_trend(wallet["address"])
        scorer = WalletQualityScorer(history=history)
        scored_wallets.append(scorer.score_wallet(wallet))

    scored_wallets.sort(key=lambda x: x["final_score"], reverse=True)

    with open("reports/scored_wallets.json", "w") as f:
        json.dump(scored_wallets, f, indent=2)

    pm.save_snapshot(scored_wallets, "scored_wallets")
    print(f"Scored {len(scored_wallets)} wallets. Saved to reports/scored_wallets.json and persistence.")


if __name__ == "__main__":
    main()
