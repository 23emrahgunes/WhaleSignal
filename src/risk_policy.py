import json
import os


DEFAULT_RISK_POLICY = {
    "min_final_score": 0.6,
    "min_copyability_score": 0.55,
    "max_drawdown_risk_score": 0.55,
    "min_avg_liquidity": 25000,
    "max_stale_penalty": 0.0,
    "block_noisy_wallets": True,
    "reduce_high_risk_sprinters": True,
    "max_wallet_weight": 0.15,
    "max_category_weight": 0.35,
}


class RiskPolicy:
    def __init__(self, policy_path="config/risk_policy.json", policy=None):
        self.policy_path = policy_path
        self.policy = dict(DEFAULT_RISK_POLICY)
        if policy:
            self.policy.update(policy)
        elif os.path.exists(policy_path):
            with open(policy_path, "r") as f:
                loaded = json.load(f)
            self.policy.update(loaded)

    def evaluate_wallet(self, scored_wallet, archetype_result, enriched_wallet):
        penalties = scored_wallet.get("penalties", {})
        final_score = float(scored_wallet.get("final_score", 0))
        copyability_score = float(archetype_result.get("copyability_score", 0))
        drawdown_risk_score = float(archetype_result.get("drawdown_risk_score", 0))
        archetype = archetype_result.get("archetype", "Noisy / Unfollowable")
        avg_liquidity = self._avg_liquidity(enriched_wallet)

        violations = []
        allocation_multiplier = 1.0

        if final_score < self.policy["min_final_score"]:
            violations.append("below_min_final_score")
        if copyability_score < self.policy["min_copyability_score"]:
            violations.append("below_min_copyability")
        if drawdown_risk_score > self.policy["max_drawdown_risk_score"]:
            violations.append("above_max_drawdown_risk")
        if avg_liquidity < self.policy["min_avg_liquidity"]:
            violations.append("below_min_liquidity")
        if float(penalties.get("stale", 0)) > self.policy["max_stale_penalty"]:
            violations.append("stale_wallet_block")
        if self.policy["block_noisy_wallets"] and archetype == "Noisy / Unfollowable":
            violations.append("noisy_wallet_block")
        if self.policy["reduce_high_risk_sprinters"] and archetype == "High-Risk Sprinter":
            allocation_multiplier = 0.5

        blocked = len(violations) > 0
        return {
            "blocked": blocked,
            "violations": violations,
            "allocation_multiplier": allocation_multiplier,
            "avg_liquidity": round(avg_liquidity, 4),
        }

    def _avg_liquidity(self, enriched_wallet):
        exposures = enriched_wallet.get("liquidity_exposure", [])
        if not exposures:
            return 0.0
        values = [float(item.get("liquidity", 0) or 0) for item in exposures]
        return sum(values) / max(1, len(values))
