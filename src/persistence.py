import os
import json
from datetime import datetime
import glob

class PersistenceManager:
    def __init__(self, history_dir="data/history"):
        self.history_dir = history_dir
        os.makedirs(self.history_dir, exist_ok=True)

    def save_snapshot(self, data, snapshot_type="scored_wallets"):
        """Save a daily snapshot of the data."""
        date_str = datetime.now().strftime("%Y-%m-%d")
        filename = f"{snapshot_type}_{date_str}.json"
        filepath = os.path.join(self.history_dir, filename)

        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
        print(f"Snapshot saved: {filepath}")
        return filepath

    def get_latest_snapshot(self, snapshot_type="scored_wallets", before_date=None):
        """Retrieve the most recent snapshot before a certain date."""
        files = glob.glob(os.path.join(self.history_dir, f"{snapshot_type}_*.json"))
        if not files:
            return None

        # Sort by filename (which contains date)
        files.sort()

        if before_date:
            # Filter files that are strictly before before_date
            # scored_wallets_2026-04-17.json -> split("_") -> ['scored', 'wallets', '2026-04-17.json']
            # We need index 2
            files = [f for f in files if os.path.basename(f).split("_")[2].split(".")[0] < before_date]

        if not files:
            return None

        latest_file = files[-1]
        with open(latest_file, "r") as f:
            return json.load(f)

    def get_historical_trend(self, address, snapshot_type="scored_wallets", days=30):
        """Get the score trend for a specific wallet address over the last X days."""
        files = glob.glob(os.path.join(self.history_dir, f"{snapshot_type}_*.json"))
        files.sort(reverse=True)

        trend = []
        for f in files[:days]:
            date_str = os.path.basename(f).split("_")[2].split(".")[0]
            with open(f, "r") as json_file:
                data = json.load(json_file)
                # Find the wallet in this snapshot
                wallet_data = next((w for w in data if w["address"] == address), None)
                if wallet_data:
                    trend.append({
                        "date": date_str,
                        "score": wallet_data.get("final_score"),
                        "tier": wallet_data.get("tier")
                    })
        return trend
