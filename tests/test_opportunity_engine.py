from src.opportunity_engine import OpportunityEngine


def test_follow_opportunity():
    engine = OpportunityEngine()
    scored = {
        "address": "0xdef",
        "tier": "A",
        "final_score": 0.82,
        "penalties": {"stale": 0.0, "noise": 0.0, "concentration": 0.0},
    }
    enriched = {
        "address": "0xdef",
        "liquidity_exposure": [{"liquidity": 250000}, {"liquidity": 200000}],
    }
    arche = {
        "address": "0xdef",
        "archetype": "Stable Compounder",
        "metrics": {
            "copyability_score": 0.8,
            "drawdown_risk_score": 0.2,
            "category_specialization_score": 0.55,
        },
    }
    result = engine.evaluate_wallet(scored, enriched, arche)
    assert result["decision"] == "FOLLOW"
    assert result["opportunity_score"] >= 0.7
