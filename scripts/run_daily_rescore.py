import json
import os
from src.wallet_quality import WalletQualityScorer

def main():
    print("Running Wallet Rescoring...")

    if not os.path.exists("reports/enriched_wallets.json"):
        print("Enriched wallet data not found. Run enrichment script first.")
        return

    with open("reports/enriched_wallets.json", "r") as f:
        enriched_data = json.load(f)

    scorer = WalletQualityScorer()
    scored_wallets = []

    for wallet in enriched_data:
        score_result = scorer.score_wallet(wallet)
        scored_wallets.append(score_result)

    # Sort by final score
    scored_wallets.sort(key=lambda x: x["final_score"], reverse=True)

    with open("reports/scored_wallets.json", "w") as f:
        json.dump(scored_wallets, f, indent=2)

    print(f"Scored {len(scored_wallets)} wallets. Saved to reports/scored_wallets.json")

if __name__ == "__main__":
    main()
