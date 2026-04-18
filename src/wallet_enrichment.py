import time
from tqdm import tqdm
from src.api_clients import DataClient
from src.config import WINDOWS

class WalletEnrichment:
    def __init__(self):
        self.data_client = DataClient()

    def enrich_wallets(self, wallet_addresses, markets_df):
        enriched_data = []
        token_to_market = {}
        for _, m in markets_df.iterrows():
            tokens = m.get("tokens", [])
            for t in tokens: token_to_market[str(t)] = m

        for address in tqdm(wallet_addresses):
            try:
                trades = self.data_client.get_trades(user_address=address, limit=200)
                positions = self.data_client.get_positions(user_address=address)
                closed_positions = self.data_client.get_closed_positions(user_address=address)
                activity = self.data_client.get_activity(user_address=address, limit=200)

                now = time.time()
                stats = {
                    "address": address,
                    "total_trades": len(trades),
                    "current_value": sum(float(p.get("currentValue", 0)) for p in positions),
                    "trades_7d": 0, "trades_30d": 0, "trades_90d": 0,
                    "categories": {}, "market_concentration": {},
                    "last_active_ts": 0, "liquidity_exposure": [],
                    "realized_pnl_open": sum(float(p.get("realizedPnl", 0)) for p in positions),
                    "realized_pnl_closed": sum(float(p.get("realizedPnl", 0)) for p in closed_positions),
                }
                for t in trades:
                    ts = int(t.get("timestamp", 0))
                    if ts > stats["last_active_ts"]: stats["last_active_ts"] = ts
                    age = now - ts
                    if age <= WINDOWS["short"] * 86400: stats["trades_7d"] += 1
                    if age <= WINDOWS["mid"] * 86400: stats["trades_30d"] += 1
                    if age <= WINDOWS["long"] * 86400: stats["trades_90d"] += 1

                    asset_id = str(t.get("asset"))
                    market = token_to_market.get(asset_id)
                    if market is not None:
                        cat = market.get("category", "OTHER")
                        stats["categories"][cat] = stats["categories"].get(cat, 0) + 1
                        m_id = str(market.get("market_id"))
                        stats["market_concentration"][m_id] = stats["market_concentration"].get(m_id, 0) + 1
                        stats["liquidity_exposure"].append({"liquidity": market.get("liquidity", 0)})
                enriched_data.append(stats)
            except: continue
        return enriched_data
