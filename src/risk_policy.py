from typing import Dict


DEFAULT_RISK_POLICY = {
    "max_wallet_exposure": 0.10,
    "max_category_exposure": 0.25,
    "min_liquidity_score": 0.20,
    "max_drawdown_risk_score": 0.70,
    "block_stale_penalty": 0.20,
    "high_risk_size_multiplier": 0.50,
}


class RiskPolicyEngine:
    def __init__(self, policy: Dict = None):
        self.policy = policy or DEFAULT_RISK_POLICY.copy()

    def evaluate(self, opportunity_wallet: Dict) -> Dict:
        inputs = opportunity_wallet.get("inputs", {}) or {}
        liquidity_score = float(inputs.get("liquidity_score", 0) or 0)
        risk_score = float(inputs.get("drawdown_risk_score", 0) or 0)
        freshness_score = float(inputs.get("freshness_score", 0) or 0)
        archetype = opportunity_wallet.get("archetype", "Unknown")

        blocked = False
        reasons = []
        suggested_size_multiplier = 1.0

        if liquidity_score < self.policy["min_liquidity_score"]:
            blocked = True
            reasons.append("liquidity_below_policy")

        if freshness_score <= (1.0 - self.policy["block_stale_penalty"]):
            blocked = True
            reasons.append("stale_wallet_block")

        if risk_score > self.policy["max_drawdown_risk_score"]:
            blocked = True
            reasons.append("risk_above_policy")

        if archetype == "High-Risk Sprinter":
            suggested_size_multiplier *= self.policy["high_risk_size_multiplier"]
            reasons.append("high_risk_size_reduction")

        action = "ALLOW"
        if blocked:
            action = "BLOCK"
        elif suggested_size_multiplier < 1.0:
            action = "ALLOW_REDUCED"

        return {
            "address": opportunity_wallet.get("address"),
            "policy_action": action,
            "policy_reasons": reasons,
            "suggested_size_multiplier": round(suggested_size_multiplier, 4),
        }
