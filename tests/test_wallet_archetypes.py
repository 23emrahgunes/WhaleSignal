from src.wallet_archetypes import WalletArchetypeEngine


def test_stable_compounder_classification():
    engine = WalletArchetypeEngine()
    scored = {
        "address": "0xabc",
        "final_score": 0.82,
        "tier": "A",
        "sub_scores": {"followability": 0.8},
        "penalties": {"noise": 0.0, "stale": 0.0, "concentration": 0.0},
    }
    enriched = {
        "address": "0xabc",
        "total_trades": 60,
        "trades_7d": 18,
        "trades_30d": 30,
        "current_value": 1000,
        "realized_pnl_open": 200,
        "realized_pnl_closed": 150,
        "categories": {"CRYPTO": 20, "SPORTS": 10},
        "liquidity_exposure": [{"liquidity": 220000}, {"liquidity": 180000}],
    }
    result = engine.classify_wallet(scored, enriched)
    assert result["archetype"] == "Stable Compounder"
    assert result["metrics"]["copyability_score"] > 0.7
