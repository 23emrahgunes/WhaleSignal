import pandas as pd
from tqdm import tqdm
from src.api_clients import DataClient

class WalletCensus:
    def __init__(self):
        self.data_client = DataClient()
        self.wallets = set()

    def discover_wallets(self, markets_df, trades_per_market=50):
        for _, market in tqdm(markets_df.iterrows(), total=len(markets_df)):
            tokens = market.get("tokens", [])
            for token_id in tokens:
                try:
                    trades = self.data_client.get_trades(asset=token_id, limit=trades_per_market)
                    for t in trades:
                        if t.get("proxyWallet"): self.wallets.add(t["proxyWallet"].lower())
                except: continue
        return list(self.wallets)
