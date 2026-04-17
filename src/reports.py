import pandas as pd
import json
import os

class ReportGenerator:
    def __init__(self, ranked_data):
        self.ranked_data = ranked_data
        os.makedirs("reports", exist_ok=True)

    def export_json(self, data, filename):
        filepath = f"reports/{filename}"
        with open(filepath, "w") as f:
            if isinstance(data, pd.DataFrame):
                data.to_json(f, orient="records", indent=2)
            else:
                json.dump(data, f, indent=2)
        print(f"Exported {filepath}")

    def export_csv(self, df, filename):
        filepath = f"reports/{filename}"
        df.to_csv(filepath, index=False)
        print(f"Exported {filepath}")

    def generate_all_reports(self, ranker):
        # 1. global_top_wallets.json
        top_wallets = ranker.get_global_top()
        self.export_json(top_wallets, "global_top_wallets.json")

        # 2. category_rankings.json
        # Simplified: all for now
        self.export_json(top_wallets, "category_rankings.json")

        # 3. rising_wallets.json
        rising = ranker.get_rising_wallets()
        self.export_json(rising, "rising_wallets.json")

        # 4. dropped_wallets.json
        dropped = ranker.get_dropped_wallets()
        self.export_json(dropped, "dropped_wallets.json")

        # 5. watchlist.csv
        watchlist = top_wallets[["address", "final_score", "tier"]]
        self.export_csv(watchlist, "watchlist.csv")
