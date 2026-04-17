import pandas as pd
import json
import os

class ReportGenerator:
    def __init__(self, history_dir="data/history"):
        self.history_dir = history_dir
        os.makedirs("reports/watchlists", exist_ok=True)

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

    def publish_watchlists(self, watchlists, category_rankings):
        """Export specialized watchlists to reports/watchlists/."""
        for name, df in watchlists.items():
            if not df.empty:
                self.export_json(df, f"watchlists/{name}_watchlist.json")
                self.export_csv(df, f"watchlists/{name}_watchlist.csv")

        for cat, df in category_rankings.items():
            if not df.empty:
                self.export_json(df, f"watchlists/category_{cat.lower()}_watchlist.json")
                self.export_csv(df, f"watchlists/category_{cat.lower()}_watchlist.csv")

    def generate_all_reports(self, ranker, transitions):
        # 1. Standard Reports
        self.export_json(ranker.get_global_top(), "global_top_wallets.json")
        self.export_json(transitions, "wallet_transitions.json")

        # 2. Watchlists
        watchlists = ranker.generate_watchlists(transitions)

        # 3. Category Rankings
        categories = ["CRYPTO", "SPORTS", "POLITICS", "OTHER"]
        cat_rankings = {cat: ranker.get_category_rankings(cat) for cat in categories}

        self.publish_watchlists(watchlists, cat_rankings)

        # 4. Watchlist CSV (Legacy/Core)
        if not watchlists["core"].empty:
            self.export_csv(watchlists["core"][["address", "final_score", "tier"]], "watchlist.csv")
