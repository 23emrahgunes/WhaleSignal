import time
from src.config import WEIGHTS, WINDOW_WEIGHTS, PENALTIES, LIQUIDITY_THRESHOLDS

class WalletQualityScorer:
    def __init__(self):
        self.weights = WEIGHTS
        self.window_weights = WINDOW_WEIGHTS
        self.penalties = PENALTIES

    def score_wallet(self, enriched_wallet):
        """
        Calculate the Wallet Quality Score based on enriched data.
        Returns a dict with final score and sub-scores.
        """
        # 1. Consistency Score (0.22)
        consistency_score = self._calc_consistency(enriched_wallet)

        # 2. Realized Quality Score (0.20)
        realized_quality_score = self._calc_realized_quality(enriched_wallet)

        # 3. Recency Score (0.14)
        recency_score = self._calc_recency(enriched_wallet)

        # 4. Category Strength Score (0.14)
        category_strength_score = self._calc_category_strength(enriched_wallet)

        # 5. Liquidity Adjusted Score (0.15)
        liquidity_adjusted_score = self._calc_liquidity_adjusted(enriched_wallet)

        # 6. Followability Score (0.15)
        followability_score = self._calc_followability(enriched_wallet)

        # Base Score
        base_score = (
            consistency_score * self.weights["consistency"] +
            realized_quality_score * self.weights["realized_quality"] +
            recency_score * self.weights["recency"] +
            category_strength_score * self.weights["category_strength"] +
            liquidity_adjusted_score * self.weights["liquidity_adjusted"] +
            followability_score * self.weights["followability"]
        )

        # Penalties
        concentration_penalty = self._calc_concentration_penalty(enriched_wallet)
        stale_penalty = self._calc_stale_penalty(enriched_wallet)
        noise_penalty = self._calc_noise_penalty(enriched_wallet)

        total_penalty = concentration_penalty + stale_penalty + noise_penalty
        final_score = max(0, base_score - total_penalty)

        # Classification
        tier = "C"
        if final_score >= 0.7:
            tier = "A"
        elif final_score >= 0.4:
            tier = "B"

        return {
            "address": enriched_wallet["address"],
            "final_score": round(final_score, 4),
            "tier": tier,
            "sub_scores": {
                "consistency": round(consistency_score, 4),
                "realized_quality": round(realized_quality_score, 4),
                "recency": round(recency_score, 4),
                "category_strength": round(category_strength_score, 4),
                "liquidity_adjusted": round(liquidity_adjusted_score, 4),
                "followability": round(followability_score, 4),
            },
            "penalties": {
                "concentration": round(concentration_penalty, 4),
                "stale": round(stale_penalty, 4),
                "noise": round(noise_penalty, 4),
            }
        }

    def _calc_consistency(self, w):
        # Ratio of mid-term activity vs total, or similar
        # For now: Weighted activity across windows
        # Normalize by a "reasonable" number of trades (e.g. 50 trades = 1.0)
        denom = 50.0
        s = (w["trades_7d"] / denom) * self.window_weights["short"] + \
            (w["trades_30d"] / denom) * self.window_weights["mid"] + \
            (w["trades_90d"] / denom) * self.window_weights["long"]
        return min(1.0, s)

    def _calc_realized_quality(self, w):
        # Based on realized PnL normalized by value
        if w["current_value"] > 0:
            roi = w["realized_pnl"] / w["current_value"]
            # Simple mapping: ROI > 20% = 1.0, ROI < 0 = 0
            return min(1.0, max(0, roi * 5))
        return 0.5 # Neutral if no data

    def _calc_recency(self, w):
        # Activity in last 7 days
        return min(1.0, w["trades_7d"] / 10.0)

    def _calc_category_strength(self, w):
        # If wallet has 3+ trades in a single category, it shows strength/focus
        cats = w.get("categories", {})
        if not cats: return 0
        max_trades = max(cats.values())
        return min(1.0, max_trades / 10.0)

    def _calc_liquidity_adjusted(self, w):
        # Average liquidity exposure
        lexp = w.get("liquidity_exposure", [])
        if not lexp: return 0.5
        avg_liq = sum(float(l["liquidity"]) for l in lexp) / len(lexp)
        # Normalize: $100k liquidity = 1.0
        return min(1.0, avg_liq / 100000.0)

    def _calc_followability(self, w):
        # High followability if trades are in liquid markets
        # For now, similar to liquidity adjusted but could involve spread
        return self._calc_liquidity_adjusted(w)

    def _calc_concentration_penalty(self, w):
        # Penalty if 90%+ trades are in one category
        cats = w.get("categories", {})
        if not cats: return 0
        total = sum(cats.values())
        if total == 0: return 0
        max_share = max(cats.values()) / total
        if max_share > 0.9:
            return self.penalties["concentration"]
        return 0

    def _calc_stale_penalty(self, w):
        # Penalty if last active > 14 days ago
        last_active = w.get("last_active_ts", 0)
        if last_active == 0: return self.penalties["stale"]
        age_days = (time.time() - last_active) / 86400
        if age_days > 14:
            return self.penalties["stale"]
        return 0

    def _calc_noise_penalty(self, w):
        # Penalty for low trade count or "noise"
        if w["total_trades"] < 5:
            return self.penalties["noise"]
        return 0
