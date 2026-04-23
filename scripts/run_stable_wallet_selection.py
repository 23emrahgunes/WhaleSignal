import json
import os
from src.stable_wallets import StableWalletSelector


def main():
    required = [
        "reports/scored_wallets.json",
        "reports/wallet_archetypes.json",
        "reports/followable_opportunities.json",
    ]
    missing = [x for x in required if not os.path.exists(x)]
    if missing:
        print(f"Missing required inputs: {', '.join(missing)}")
        return

    with open("reports/scored_wallets.json", "r") as f:
        scored_wallets = json.load(f)
    with open("reports/wallet_archetypes.json", "r") as f:
        archetypes = json.load(f)
    with open("reports/followable_opportunities.json", "r") as f:
        opportunities = json.load(f)

    selector = StableWalletSelector()
    selected = selector.select(scored_wallets, archetypes, opportunities)

    os.makedirs("reports", exist_ok=True)
    with open("reports/stable_wallets.json", "w") as f:
        json.dump(selected, f, indent=2)

    try:
        import csv
        with open("reports/stable_wallets.csv", "w", newline="") as f:
            if selected:
                writer = csv.DictWriter(f, fieldnames=list(selected[0].keys()))
                writer.writeheader()
                writer.writerows(selected)
            else:
                writer = csv.writer(f)
                writer.writerow(["address", "final_score", "tier", "archetype", "opportunity_score"])
    except Exception as exc:
        print(f"CSV export skipped: {exc}")

    print(f"Selected {len(selected)} stable wallets.")


if __name__ == "__main__":
    main()
