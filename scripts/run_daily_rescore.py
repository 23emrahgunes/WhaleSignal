import json, os
from src.wallet_quality import WalletQualityScorer
from src.persistence import PersistenceManager
def main():
    if not os.path.exists("reports/enriched_wallets.json"): return
    with open("reports/enriched_wallets.json", "r") as f: enriched = json.load(f)
    pm = PersistenceManager(); scored = []
    for w in enriched:
        hist = pm.get_historical_trend(w["address"])
        s = WalletQualityScorer(history=hist)
        scored.append(s.score_wallet(w))
    scored.sort(key=lambda x: x["final_score"], reverse=True)
    with open("reports/scored_wallets.json", "w") as f: json.dump(scored, f, indent=2)
    pm.save_snapshot(scored, "scored_wallets")
if __name__ == "__main__": main()
