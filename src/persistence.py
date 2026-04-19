import os, json, glob
from datetime import datetime

class PersistenceManager:
    def __init__(self, history_dir="data/history"):
        self.history_dir = history_dir
        os.makedirs(self.history_dir, exist_ok=True)

    def save_snapshot(self, data, snapshot_type="scored_wallets"):
        date_str = datetime.now().strftime("%Y-%m-%d")
        path = os.path.join(self.history_dir, f"{snapshot_type}_{date_str}.json")
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        return path

    def get_latest_snapshot(self, snapshot_type="scored_wallets", before_date=None):
        files = sorted(glob.glob(os.path.join(self.history_dir, f"{snapshot_type}_*.json")))
        if before_date:
            files = [f for f in files if os.path.basename(f).split("_")[2].split(".")[0] < before_date]
        if not files:
            return None
        with open(files[-1], "r") as f:
            return json.load(f)

    def get_historical_trend(self, address, snapshot_type="scored_wallets", days=30):
        files = sorted(glob.glob(os.path.join(self.history_dir, f"{snapshot_type}_*.json")), reverse=True)
        trend = []
        for f in files[:days]:
            date_str = os.path.basename(f).split("_")[2].split(".")[0]
            with open(f, "r") as json_file:
                data = json.load(json_file)
                wallet = next((w for w in data if w["address"] == address), None)
                if wallet:
                    trend.append({"date": date_str, "score": wallet.get("final_score")})
        return trend
