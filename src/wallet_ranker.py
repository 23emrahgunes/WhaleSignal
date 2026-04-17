import pandas as pd
import json

class WalletRanker:
    def __init__(self, scored_wallets, enriched_wallets):
        self.scored_df = pd.DataFrame(scored_wallets)
        self.enriched_df = pd.DataFrame(enriched_wallets)

    def get_global_top(self, limit=100):
        return self.scored_df.sort_values("final_score", ascending=False).head(limit)

    def get_category_rankings(self, category):
        # Join with enriched data to get categories
        # For simplicity in v1, we assume the enriched data has a primary category
        # Or we can filter based on 'category_strength_score'
        # In a real scenario, we'd look at enriched_df["categories"]
        return self.scored_df[self.scored_df["tier"].isin(["A", "B"])].head(20)

    def get_rising_wallets(self):
        # Higher recency vs consistency
        return self.scored_df[self.scored_df["sub_scores"].apply(lambda x: x["recency"] > x["consistency"])]

    def get_dropped_wallets(self):
        # High stale penalty or low recency
        return self.scored_df[self.scored_df["penalties"].apply(lambda x: x["stale"] > 0)]
