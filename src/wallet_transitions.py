from datetime import datetime
from src.persistence import PersistenceManager

class TransitionEngine:
    def __init__(self, history_dir="data/history"):
        self.pm = PersistenceManager(history_dir)

    def analyze_transitions(self, current_scores):
        prev = self.pm.get_latest_snapshot(before_date=datetime.now().strftime("%Y-%m-%d"))
        if not prev:
            return {
                "new_wallets": current_scores,
                "rising_wallets": [],
                "dropped_wallets": [],
                "stale_wallets": [],
                "upgraded_wallets": [],
                "downgraded_wallets": [],
            }

        prev_map = {w["address"]: w for w in prev}
        trans = {k: [] for k in ["new_wallets", "rising_wallets", "dropped_wallets", "stale_wallets", "upgraded_wallets", "downgraded_wallets"]}
        tiers = {"A": 3, "B": 2, "C": 1}

        for curr in current_scores:
            addr = curr["address"]
            prev_wallet = prev_map.get(addr)
            if not prev_wallet:
                trans["new_wallets"].append(curr)
                continue

            delta = curr["final_score"] - prev_wallet["final_score"]
            curr["score_delta"] = round(delta, 4)

            if delta >= 0.05:
                trans["rising_wallets"].append(curr)
            elif delta <= -0.05:
                trans["dropped_wallets"].append(curr)

            if tiers.get(curr["tier"], 0) > tiers.get(prev_wallet["tier"], 0):
                trans["upgraded_wallets"].append(curr)
            elif tiers.get(curr["tier"], 0) < tiers.get(prev_wallet["tier"], 0):
                trans["downgraded_wallets"].append(curr)

            if curr["penalties"]["stale"] > prev_wallet["penalties"]["stale"]:
                trans["stale_wallets"].append(curr)

        return trans
