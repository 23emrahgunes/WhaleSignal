from typing import Dict, List


class OpportunityEngine:
    def _avg_liquidity(self, enriched_wallet: Dict) -> float:
        exposure = enriched_wallet.get("liquidity_exposure", []) or []
        if not exposure:
            return 0.0
        return sum(float(x.get("liquidity", 0) or 0) for x in exposure) / len(exposure)

    def evaluate_wallet(self, scored_wallet: Dict, enriched_wallet: Dict, archetype_wallet: Dict) -> Dict:
        final_score = float(scored_wallet.get("final_score", 0) or 0)
        penalties = scored_wallet.get("penalties", {}) or {}
        metrics = archetype_wallet.get("metrics", {}) or {}
        avg_liquidity = self._avg_liquidity(enriched_wallet)
        liquidity_score = min(1.0, avg_liquidity / 150000.0)
        freshness_score = max(0.0, 1.0 - float(penalties.get("stale", 0) or 0))
        copyability_score = float(metrics.get("copyability_score", 0) or 0)
        risk_score = float(metrics.get("drawdown_risk_score", 0) or 0)
        specialization_score = float(metrics.get("category_specialization_score", 0) or 0)

        hard_block = penalties.get("stale", 0) >= 0.2 or final_score < 0.35 or liquidity_score < 0.15
        opportunity_score = max(0.0, min(1.0, (final_score * 0.40) + (copyability_score * 0.25) + (liquidity_score * 0.15) + (freshness_score * 0.10) + (specialization_score * 0.10) - (risk_score * 0.15)))

        decision = "IGNORE"
        reason_codes: List[str] = []
        if hard_block:
            reason_codes.append("hard_block")
            if liquidity_score < 0.15:
                reason_codes.append("low_liquidity")
            if penalties.get("stale", 0) >= 0.2:
                reason_codes.append("stale_wallet")
            if final_score < 0.35:
                reason_codes.append("low_final_score")
        elif opportunity_score >= 0.7 and copyability_score >= 0.55:
            decision = "FOLLOW"
            reason_codes.extend(["strong_wallet_market_fit", "good_copyability"])
        elif opportunity_score >= 0.45:
            decision = "WATCH"
            reason_codes.extend(["needs_confirmation"])
        else:
            reason_codes.extend(["weak_opportunity_score"])

        return {
            "address": scored_wallet.get("address"),
            "archetype": archetype_wallet.get("archetype"),
            "tier": scored_wallet.get("tier"),
            "opportunity_score": round(opportunity_score, 4),
            "decision": decision,
            "reason_codes": reason_codes,
            "inputs": {
                "final_score": round(final_score, 4),
                "copyability_score": round(copyability_score, 4),
                "liquidity_score": round(liquidity_score, 4),
                "freshness_score": round(freshness_score, 4),
                "drawdown_risk_score": round(risk_score, 4),
            },
        }

    def evaluate_many(self, scored_wallets: List[Dict], enriched_wallets: List[Dict], archetype_wallets: List[Dict]) -> List[Dict]:
        scored_map = {w.get("address"): w for w in scored_wallets}
        enriched_map = {w.get("address"): w for w in enriched_wallets}
        results = []
        for arche in archetype_wallets:
            addr = arche.get("address")
            scored = scored_map.get(addr, {})
            enriched = enriched_map.get(addr, {})
            if scored:
                results.append(self.evaluate_wallet(scored, enriched, arche))
        results.sort(key=lambda x: x.get("opportunity_score", 0), reverse=True)
        return results
