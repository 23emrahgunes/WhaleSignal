import pandas as pd
import json, os

class ReportGenerator:
    def __init__(self, history_dir="data/history"):
        self.history_dir = history_dir
        os.makedirs("reports/watchlists", exist_ok=True)

    def export_json(self, data, filename):
        path = f"reports/{filename}"
        with open(path, "w") as f:
            if isinstance(data, pd.DataFrame): data.to_json(f, orient="records", indent=2)
            else: json.dump(data, f, indent=2)

    def export_csv(self, df, filename):
        df.to_csv(f"reports/{filename}", index=False)

    def publish_watchlists(self, watchlists, category_rankings):
        for name, df in watchlists.items():
            if not df.empty:
                self.export_json(df, f"watchlists/{name}_watchlist.json")
                self.export_csv(df, f"watchlists/{name}_watchlist.csv")
        for cat, df in category_rankings.items():
            if not df.empty:
                self.export_json(df, f"watchlists/category_{cat.lower()}_watchlist.json")
                self.export_csv(df, f"watchlists/category_{cat.lower()}_watchlist.csv")

    def generate_all_reports(self, ranker, transitions):
        self.export_json(ranker.get_global_top(), "global_top_wallets.json")
        self.export_json(transitions, "wallet_transitions.json")
        wl = ranker.generate_watchlists(transitions)
        cat_rank = {c: ranker.get_category_rankings(c) for c in ["CRYPTO", "SPORTS", "POLITICS", "OTHER"]}
        self.publish_watchlists(wl, cat_rank)
        if not wl["core"].empty: self.export_csv(wl["core"][["address", "final_score", "tier"]], "watchlist.csv")
