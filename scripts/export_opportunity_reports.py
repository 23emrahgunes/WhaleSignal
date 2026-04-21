import json
import os
import csv


def main():
    if not os.path.exists("reports/followable_opportunities.json"):
        print("No followable opportunities file found.")
        return

    with open("reports/followable_opportunities.json", "r") as f:
        followable = json.load(f)

    os.makedirs("reports/opportunities", exist_ok=True)
    with open("reports/opportunities/followable_summary.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["address", "archetype", "tier", "opportunity_score", "decision"])
        writer.writeheader()
        for row in followable:
            writer.writerow({
                "address": row.get("address"),
                "archetype": row.get("archetype"),
                "tier": row.get("tier"),
                "opportunity_score": row.get("opportunity_score"),
                "decision": row.get("decision"),
            })

    with open("reports/opportunities/opportunity_summary.json", "w") as f:
        json.dump({"count": len(followable), "top": followable[:20]}, f, indent=2)

    print(f"Exported {len(followable)} followable opportunities.")


if __name__ == "__main__":
    main()
