import time
from src.config import WEIGHTS, WINDOW_WEIGHTS, PENALTIES, LIQUIDITY_THRESHOLDS


class WalletQualityScorer:
    def __init__(self, history=None):
        self.weights = WEIGHTS
        self.window_weights = WINDOW_WEIGHTS
        self.penalties = PENALTIES
        self.history = history

    def score_wallet(self, enriched_wallet):
        consistency_score = self._calc_consistency(enriched_wallet)
        realized_quality_score = self._calc_realized_quality(enriched_wallet)
        recency_score = self._calc_recency(enriched_wallet)
        category_strength_score = self._calc_category_strength(enriched_wallet)
        liquidity_adjusted_score = self._calc_liquidity_adjusted(enriched_wallet)
        followability_score = self._calc_followability(enriched_wallet)

        base_score = (
            consistency_score * self.weights["consistency"]
            + realized_quality_score * self.weights["realized_quality"]
            + recency_score * self.weights["recency"]
            + category_strength_score * self.weights["category_strength"]
            + liquidity_adjusted_score * self.weights["liquidity_adjusted"]
            + followability_score * self.weights["followability"]
        )

        stability_modifier = self._calc_stability_modifier()
        base_score += stability_modifier

        concentration_penalty = self._calc_concentration_penalty(enriched_wallet)
        stale_penalty = self._calc_stale_penalty(enriched_wallet)
        noise_penalty = self._calc_noise_penalty(enriched_wallet)

        total_penalty = concentration_penalty + stale_penalty + noise_penalty
        final_score = max(0, min(1.0, base_score - total_penalty))

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
                "stability_modifier": round(stability_modifier, 4),
            },
            "penalties": {
                "concentration": round(concentration_penalty, 4),
                "stale": round(stale_penalty, 4),
                "noise": round(noise_penalty, 4),
            },
        }

    def _calc_consistency(self, w):
        denom = 50.0
        s = (w["trades_7d"] / denom) * self.window_weights["short"] + (w["trades_30d"] / denom) * self.window_weights["mid"] + (w["trades_90d"] / denom) * self.window_weights["long"]
        return min(1.0, s)

    def _calc_realized_quality(self, w):
        realized_total = w.get("realized_pnl_open", 0) + w.get("realized_pnl_closed", 0)
        if w["current_value"] > 10:
            roi = realized_total / w["current_value"]
            return min(1.0, max(0, 0.5 + roi))
        if realized_total > 100:
            return 0.8
        if realized_total > 0:
            return 0.6
        return 0.4

    def _calc_recency(self, w):
        if w["trades_30d"] == 0:
            return 0
        ratio = w["trades_7d"] / (w["trades_30d"] / 4.0)
        return min(1.0, ratio * 0.5 + (w["trades_7d"] / 20.0) * 0.5)

    def _calc_category_strength(self, w):
        cats = w.get("categories", {})
        if not cats:
            return 0
        max_trades = max(cats.values())
        diversity_bonus = min(0.2, len(cats) * 0.05)
        return min(1.0, (max_trades / 15.0) + diversity_bonus)

    def _calc_liquidity_adjusted(self, w):
        lexp = w.get("liquidity_exposure", [])
        if not lexp:
            return 0.4
        avg_liq = sum(float(l["liquidity"]) for l in lexp) / len(lexp)
        return min(1.0, avg_liq / 150000.0)

    def _calc_followability(self, w):
        liq_score = self._calc_liquidity_adjusted(w)
        activity_bonus = min(0.2, w["total_trades"] / 100.0)
        return min(1.0, liq_score * 0.8 + activity_bonus)

    def _calc_stability_modifier(self):
        if not self.history or len(self.history) < 3:
            return 0
        scores = [h["score"] for h in self.history]
        avg_score = sum(scores) / len(scores)
        variance = sum((s - avg_score) ** 2 for s in scores) / len(scores)
        if variance < 0.01:
            return 0.05
        if variance > 0.05:
            return -0.05
        return 0

    def _calc_concentration_penalty(self, w):
        m_con = w.get("market_concentration", {})
        if not m_con:
            return 0
        total = sum(m_con.values())
        if total == 0:
            return 0
        max_share = max(m_con.values()) / total
        if max_share > 0.8:
            return self.penalties["concentration"]
        return 0

    def _calc_stale_penalty(self, w):
        last_active = w.get("last_active_ts", 0)
        if last_active == 0:
            return self.penalties["stale"]
        age_days = (time.time() - last_active) / 86400
        if age_days > 30:
            return self.penalties["stale"] * 2
        if age_days > 14:
            return self.penalties["stale"]
        return 0

    def _calc_noise_penalty(self, w):
        if w["total_trades"] < 10:
            return self.penalties["noise"]
        return 0
