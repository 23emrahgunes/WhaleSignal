import os
import json
from src.persistence import PersistenceManager

class TransitionEngine:
    def __init__(self, history_dir="data/history"):
        self.pm = PersistenceManager(history_dir)

    def analyze_transitions(self, current_scores):
        """Compare current scores with the latest historical snapshot to find transitions."""
        from datetime import datetime
        today_str = datetime.now().strftime("%Y-%m-%d")

        # Look for the latest snapshot strictly BEFORE today
        prev_scores = self.pm.get_latest_snapshot(before_date=today_str)

        if not prev_scores:
            print("No previous snapshot found for transition analysis.")
            return self._empty_transitions()

        prev_map = {w["address"]: w for w in prev_scores}

        transitions = {
            "new_wallets": [],
            "rising_wallets": [], # Score up by > 10%
            "dropped_wallets": [], # Score down by > 10%
            "stale_wallets": [], # Tier became lower or stale penalty applied
            "upgraded_wallets": [], # Tier B -> A
            "downgraded_wallets": [], # Tier A -> B/C
        }

        for curr in current_scores:
            addr = curr["address"]
            prev = prev_map.get(addr)

            if not prev:
                transitions["new_wallets"].append(curr)
                continue

            score_delta = curr["final_score"] - prev["final_score"]
            curr["score_delta"] = round(score_delta, 4)

            # Upgraded / Downgraded
            tier_values = {"A": 3, "B": 2, "C": 1}
            prev_val = tier_values.get(prev["tier"], 0)
            curr_val = tier_values.get(curr["tier"], 0)

            if curr_val > prev_val:
                transitions["upgraded_wallets"].append(curr)
            elif curr_val < prev_val:
                transitions["downgraded_wallets"].append(curr)

            # Rising / Dropped
            if score_delta >= 0.05:
                transitions["rising_wallets"].append(curr)
            elif score_delta <= -0.05:
                transitions["dropped_wallets"].append(curr)

            # Stale (simple check: if stale penalty increased)
            if curr["penalties"]["stale"] > prev["penalties"]["stale"]:
                transitions["stale_wallets"].append(curr)

        return transitions

    def _empty_transitions(self):
        return {k: [] for k in ["new_wallets", "rising_wallets", "dropped_wallets", "stale_wallets", "upgraded_wallets", "downgraded_wallets"]}
