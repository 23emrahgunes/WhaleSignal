from src.risk_policy import RiskPolicyEngine


def test_high_risk_wallet_gets_reduced_or_blocked():
    engine = RiskPolicyEngine()
    wallet = {
        "address": "0x123",
        "archetype": "High-Risk Sprinter",
        "inputs": {
            "liquidity_score": 0.4,
            "drawdown_risk_score": 0.6,
            "freshness_score": 0.95,
        },
    }
    result = engine.evaluate(wallet)
    assert result["policy_action"] in ["ALLOW_REDUCED", "BLOCK"]
    assert result["suggested_size_multiplier"] <= 1.0
