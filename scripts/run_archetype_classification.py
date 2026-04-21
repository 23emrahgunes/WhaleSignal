import json
import os
from src.wallet_archetypes import WalletArchetypeEngine


def main():
    if not os.path.exists("reports/scored_wallets.json") or not os.path.exists("reports/enriched_wallets.json"):
        print("Required scored/enriched wallet data not found.")
        return

    with open("reports/scored_wallets.json", "r") as f:
        scored_wallets = json.load(f)
    with open("reports/enriched_wallets.json", "r") as f:
        enriched_wallets = json.load(f)

    engine = WalletArchetypeEngine()
    results = engine.classify_many(scored_wallets, enriched_wallets)

    with open("reports/wallet_archetypes.json", "w") as f:
        json.dump(results, f, indent=2)

    print(f"Classified {len(results)} wallets into archetypes.")


if __name__ == "__main__":
    main()
