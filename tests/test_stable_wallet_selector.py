from src.stable_wallets import StableWalletSelector


def test_selector_filters_only_copyable_follow_wallets():
    selector = StableWalletSelector(min_final_score=0.7, min_opportunity_score=0.65)

    scored = [
        {"address": "0x1", "final_score": 0.82, "tier": "A"},
        {"address": "0x2", "final_score": 0.60, "tier": "B"},
    ]
    archetypes = [
        {
            "address": "0x1",
            "archetype": "Stable Compounder",
            "archetype_confidence": 0.84,
            "metrics": {"dominant_category": "CRYPTO", "copyability_score": 0.8, "drawdown_risk_score": 0.2},
        },
        {
            "address": "0x2",
            "archetype": "Noisy / Unfollowable",
            "archetype_confidence": 0.55,
            "metrics": {"dominant_category": "OTHER", "copyability_score": 0.3, "drawdown_risk_score": 0.6},
        },
    ]
    opportunities = [
        {"address": "0x1", "decision": "FOLLOW", "opportunity_score": 0.78, "policy": {"policy_action": "ALLOW", "suggested_size_multiplier": 1.0}},
        {"address": "0x2", "decision": "FOLLOW", "opportunity_score": 0.72, "policy": {"policy_action": "ALLOW", "suggested_size_multiplier": 1.0}},
    ]

    selected = selector.select(scored, archetypes, opportunities)
    assert len(selected) == 1
    assert selected[0]["address"] == "0x1"
