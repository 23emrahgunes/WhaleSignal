from src.copytrader_paper import PaperCopyTrader


class MockDataClient:
    def __init__(self, trades):
        self.trades = trades

    def get_trades(self, user_address=None, limit=10):
        return self.trades.get(user_address, [])


class MockCLOBClient:
    def get_price(self, token_id):
        return {"price": 0.62}


def test_copytrader_opens_position_from_wallet_buy_signal(tmp_path):
    trades = {
        "0xwallet": [
            {"asset": "token-1", "market_id": "m1", "price": 0.55, "side": "BUY", "outcome": "YES", "timestamp": 1710000000}
        ]
    }
    trader = PaperCopyTrader(
        starting_capital=1000,
        allocation_per_trade=0.1,
        data_client=MockDataClient(trades),
        clob_client=MockCLOBClient(),
        state_path=str(tmp_path / "state.json"),
        actions_path=str(tmp_path / "actions.json"),
    )

    stable_wallets = [
        {"address": "0xwallet", "suggested_size_multiplier": 1.0, "dominant_category": "CRYPTO"}
    ]
    enriched_wallets = []

    result = trader.sync(stable_wallets, enriched_wallets)
    assert result["state"]["open_positions"]
    assert result["actions"][0]["action"] == "OPEN"
