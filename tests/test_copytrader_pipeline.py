import json
from src.stable_wallets import StableWalletSelector
from src.copytrader_paper import PaperCopyTrader


class MockDataClient:
    def get_trades(self, user_address=None, limit=10):
        return [{"asset": f"token-{user_address}", "market_id": f"m-{user_address}", "price": 0.5, "side": "BUY", "outcome": "YES", "timestamp": 1710000000}]


class MockCLOBClient:
    def get_price(self, token_id):
        return {"price": 0.55}


def test_pipeline_selects_and_opens_positions(tmp_path):
    selector = StableWalletSelector()
    scored = [{"address": "0x1", "final_score": 0.8, "tier": "A"}]
    archetypes = [{"address": "0x1", "archetype": "Stable Compounder", "archetype_confidence": 0.9, "metrics": {"dominant_category": "CRYPTO", "copyability_score": 0.85, "drawdown_risk_score": 0.2}}]
    opportunities = [{"address": "0x1", "decision": "FOLLOW", "opportunity_score": 0.75, "policy": {"policy_action": "ALLOW", "suggested_size_multiplier": 1.0}}]

    stable_wallets = selector.select(scored, archetypes, opportunities)
    assert len(stable_wallets) == 1

    trader = PaperCopyTrader(
        starting_capital=1000,
        data_client=MockDataClient(),
        clob_client=MockCLOBClient(),
        state_path=str(tmp_path / "state.json"),
        actions_path=str(tmp_path / "actions.json"),
    )
    result = trader.sync(stable_wallets, [])
    assert len(result["state"]["open_positions"]) == 1
    assert result["state"]["available_capital"] < 1000
