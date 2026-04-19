import pandas as pd
import json

class WalletRanker:
    def __init__(self, scored_wallets, enriched_wallets):
        self.scored_df = pd.DataFrame(scored_wallets)
        self.enriched_df = pd.DataFrame(enriched_wallets)

    def get_global_top(self, limit=100):
        return self.scored_df.sort_values("final_score", ascending=False).head(limit)

    def get_category_rankings(self, category, limit=50):
        enriched_map = {w["address"]: w for w in self.enriched_df.to_dict("records")}

        def is_cat_expert(addr):
            w = enriched_map.get(addr)
            if not w:
                return False
            cats = w.get("categories", {})
            return cats.get(category, 0) > 5

        category_experts = self.scored_df[self.scored_df["address"].apply(is_cat_expert)]
        return category_experts.sort_values("final_score", ascending=False).head(limit)

    def generate_watchlists(self, transitions):
        return {
            "core": self.scored_df[self.scored_df["tier"] == "A"].sort_values("final_score", ascending=False).head(50),
            "emerging": pd.DataFrame(transitions["rising_wallets"]).head(50),
            "probation": pd.DataFrame(transitions["dropped_wallets"] + transitions["downgraded_wallets"]).head(50),
        }
