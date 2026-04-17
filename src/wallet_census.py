import pandas as pd
from tqdm import tqdm
from src.api_clients import DataClient

class WalletCensus:
    def __init__(self):
        self.data_client = DataClient()
        self.wallets = set()

    def discover_wallets(self, markets_df, trades_per_market=100):
        """
        Scan markets to find active wallets.
        Uses Data API to fetch trades for specific assets (tokens).
        """
        print(f"Discovering wallets from {len(markets_df)} markets...")

        # We'll use the tokens (clobTokenIds) from each market to fetch trades
        for _, market in tqdm(markets_df.iterrows(), total=len(markets_df)):
            tokens = market.get("tokens", [])
            # In case tokens is a string (though it should be a list now)
            if isinstance(tokens, str):
                import json
                try:
                    tokens = json.loads(tokens)
                except:
                    tokens = []

            for token_id in tokens:
                try:
                    # Fetch recent trades for this token to find active wallets
                    trades = self.data_client._get("trades", params={"asset": token_id, "limit": trades_per_market})
                    for trade in trades:
                        wallet = trade.get("proxyWallet")
                        if wallet:
                            self.wallets.add(wallet.lower())
                except Exception as e:
                    # Skip tokens that fail
                    continue

        # Also fetch global recent trades to find very active wallets
        try:
            print("Fetching global recent trades...")
            global_trades = self.data_client.get_trades(limit=500)
            for trade in global_trades:
                wallet = trade.get("proxyWallet")
                if wallet:
                    self.wallets.add(wallet.lower())
        except Exception as e:
            print(f"Error fetching global trades: {e}")

        return list(self.wallets)

    def get_wallet_universe(self):
        return pd.DataFrame({"wallet_address": list(self.wallets)})

if __name__ == "__main__":
    # Test with a few markets
    mock_markets = pd.DataFrame([
        {"tokens": ["8501497159083948713316135768103773293754490207922884688769443031624417212426"]}
    ])
    census = WalletCensus()
    wallets = census.discover_wallets(mock_markets, trades_per_market=10)
    print(f"Discovered {len(wallets)} unique wallets.")
