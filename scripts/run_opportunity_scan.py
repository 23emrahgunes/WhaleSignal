import json
import os
from src.wallet_archetypes import WalletArchetypeEngine
from src.opportunity_engine import OpportunityEngine
from src.risk_policy import RiskPolicyEngine


def main():
    if not os.path.exists("reports/scored_wallets.json") or not os.path.exists("reports/enriched_wallets.json"):
        print("Required scored/enriched wallet data not found.")
        return

    with open("reports/scored_wallets.json", "r") as f:
        scored_wallets = json.load(f)
    with open("reports/enriched_wallets.json", "r") as f:
        enriched_wallets = json.load(f)

    archetype_engine = WalletArchetypeEngine()
    archetypes = archetype_engine.classify_many(scored_wallets, enriched_wallets)

    opportunity_engine = OpportunityEngine()
    opportunities = opportunity_engine.evaluate_many(scored_wallets, enriched_wallets, archetypes)

    risk_engine = RiskPolicyEngine()
    policy_results = [risk_engine.evaluate(x) for x in opportunities]
    policy_map = {x["address"]: x for x in policy_results}

    followable = []
    high_risk = []
    for item in opportunities:
        merged = dict(item)
        merged["policy"] = policy_map.get(item["address"], {})
        if item["decision"] == "FOLLOW" and merged["policy"].get("policy_action") != "BLOCK":
            followable.append(merged)
        if merged["policy"].get("policy_action") in ["BLOCK", "ALLOW_REDUCED"]:
            high_risk.append(merged)

    with open("reports/followable_opportunities.json", "w") as f:
        json.dump(followable, f, indent=2)
    with open("reports/high_risk_opportunities.json", "w") as f:
        json.dump(high_risk, f, indent=2)

    print(f"Evaluated {len(opportunities)} opportunities.")


if __name__ == "__main__":
    main()
