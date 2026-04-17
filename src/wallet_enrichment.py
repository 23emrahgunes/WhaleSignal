import time
import pandas as pd
from tqdm import tqdm
from src.api_clients import DataClient, CLOBClient
from src.config import WINDOWS

class WalletEnrichment:
    def __init__(self):
        self.data_client = DataClient()
        self.clob_client = CLOBClient()

    def enrich_wallets(self, wallet_addresses, markets_df):
        """
        For each wallet, gather activity, positions, and trades.
        Cross-reference with markets_df to get category distribution and liquidity.
        """
        enriched_data = []

        # Create a lookup for market data by token_id/asset_id
        token_to_market = {}
        for _, m in markets_df.iterrows():
            # Support both DataFrame rows and dicts
            tokens = m.get("tokens", [])
            if isinstance(tokens, str):
                import json
                try:
                    tokens = json.loads(tokens.replace("'", '"'))
                except:
                    tokens = []
            for t in tokens:
                token_to_market[str(t)] = m

        print(f"Enriching {len(wallet_addresses)} wallets...")
        for address in tqdm(wallet_addresses):
            try:
                wallet_info = self._enrich_single_wallet(address, token_to_market)
                enriched_data.append(wallet_info)
            except Exception as e:
                # print(f"Error enriching {address}: {e}")
                continue

        return enriched_data

    def _enrich_single_wallet(self, address, token_to_market):
        # 1. Fetch data
        trades = self.data_client.get_trades(user_address=address, limit=200)
        positions = self.data_client.get_positions(user_address=address)
        closed_positions = self.data_client.get_closed_positions(user_address=address)
        activity = self.data_client.get_activity(user_address=address, limit=200)

        # 2. Process Statistics
        current_time = time.time()

        # Windows in seconds
        short_window = WINDOWS["short"] * 86400
        mid_window = WINDOWS["mid"] * 86400
        long_window = WINDOWS["long"] * 86400

        stats = {
            "address": address,
            "total_trades": len(trades),
            "total_activity": len(activity),
            "open_positions_count": len(positions),
            "current_value": sum(float(p.get("currentValue", 0)) for p in positions),
            "trades_7d": 0,
            "trades_30d": 0,
            "trades_90d": 0,
            "categories": {},
            "market_concentration": {}, # trades per market
            "last_active_ts": 0,
            "liquidity_exposure": [],
            "realized_pnl_open": sum(float(p.get("realizedPnl", 0)) for p in positions),
            "realized_pnl_closed": sum(float(p.get("realizedPnl", 0)) for p in closed_positions),
        }

        # Process Trades
        for t in trades:
            ts = int(t.get("timestamp", 0))
            if ts > stats["last_active_ts"]:
                stats["last_active_ts"] = ts

            age = current_time - ts
            if age <= short_window: stats["trades_7d"] += 1
            if age <= mid_window: stats["trades_30d"] += 1
            if age <= long_window: stats["trades_90d"] += 1

            asset_id = str(t.get("asset"))
            market = token_to_market.get(asset_id)
            if market is not None:
                m_id = str(market.get("market_id"))
                stats["market_concentration"][m_id] = stats["market_concentration"].get(m_id, 0) + 1

                cat = market.get("category", "OTHER")
                stats["categories"][cat] = stats["categories"].get(cat, 0) + 1

                # Collect liquidity info
                stats["liquidity_exposure"].append({
                    "liquidity": market.get("liquidity", 0),
                    "volume": market.get("volume", 0),
                    "timestamp": ts
                })

        return stats

if __name__ == "__main__":
    # Small test
    enricher = WalletEnrichment()
    from src.market_census import MarketCensus
    census = MarketCensus()
    markets = census.fetch_active_markets(limit=50)
    markets_df = census.get_market_universe()

    test_wallets = ["0xd34f657c7e0a8e0a9d84cbcfa402de6ad9383ae4"]
    results = enricher.enrich_wallets(test_wallets, markets_df)
    print(results)
