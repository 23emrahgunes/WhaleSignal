import time
from src.config import WEIGHTS, WINDOW_WEIGHTS, PENALTIES

class WalletQualityScorer:
    def __init__(self, history=None):
        self.weights = WEIGHTS
        self.window_weights = WINDOW_WEIGHTS
        self.penalties = PENALTIES
        self.history = history

    def score_wallet(self, w):
        consistency = self._calc_consistency(w)
        realized = self._calc_realized_quality(w)
        recency = self._calc_recency(w)
        cat_strength = self._calc_category_strength(w)
        liquidity = self._calc_liquidity(w)
        followability = self._calc_followability(w, liquidity)

        base_score = (consistency * self.weights["consistency"] + realized * self.weights["realized_quality"] +
                      recency * self.weights["recency"] + cat_strength * self.weights["category_strength"] +
                      liquidity * self.weights["liquidity_adjusted"] + followability * self.weights["followability"])

        stability = self._calc_stability_modifier()
        final_score = max(0, min(1.0, base_score + stability - self._calc_penalties(w)))

        tier = "C"
        if final_score >= 0.7: tier = "A"
        elif final_score >= 0.4: tier = "B"

        return {"address": w["address"], "final_score": round(final_score, 4), "tier": tier,
                "sub_scores": {"consistency": round(consistency, 4), "realized": round(realized, 4), "recency": round(recency, 4), "stability": round(stability, 4)},
                "penalties": {"stale": round(self._calc_stale_penalty(w), 4)}}

    def _calc_consistency(self, w):
        denom = 50.0
        s = (w["trades_7d"]/denom)*self.window_weights["short"] + (w["trades_30d"]/denom)*self.window_weights["mid"] + (w["trades_90d"]/denom)*self.window_weights["long"]
        return min(1.0, s)

    def _calc_realized_quality(self, w):
        total = w.get("realized_pnl_open", 0) + w.get("realized_pnl_closed", 0)
        if w["current_value"] > 10: return min(1.0, max(0, 0.5 + (total/w["current_value"])))
        return 0.8 if total > 100 else 0.6 if total > 0 else 0.4

    def _calc_recency(self, w):
        if w["trades_30d"] == 0: return 0
        return min(1.0, (w["trades_7d"] / (w["trades_30d"]/4.0)) * 0.5 + (w["trades_7d"]/20.0)*0.5)

    def _calc_category_strength(self, w):
        cats = w.get("categories", {})
        if not cats: return 0
        return min(1.0, (max(cats.values())/15.0) + min(0.2, len(cats)*0.05))

    def _calc_liquidity(self, w):
        lexp = w.get("liquidity_exposure", [])
        if not lexp: return 0.4
        return min(1.0, (sum(float(l["liquidity"]) for l in lexp)/len(lexp))/150000.0)

    def _calc_followability(self, w, liq):
        return min(1.0, liq * 0.8 + min(0.2, w["total_trades"]/100.0))

    def _calc_stability_modifier(self):
        if not self.history or len(self.history) < 3: return 0
        scores = [h["score"] for h in self.history]
        avg = sum(scores)/len(scores)
        var = sum((s-avg)**2 for s in scores)/len(scores)
        return 0.05 if var < 0.01 else -0.05 if var > 0.05 else 0

    def _calc_penalties(self, w):
        return self._calc_stale_penalty(w) + self._calc_concentration_penalty(w) + (0.1 if w["total_trades"] < 10 else 0)

    def _calc_concentration_penalty(self, w):
        m_con = w.get("market_concentration", {})
        if not m_con: return 0
        total = sum(m_con.values())
        return 0.1 if total > 0 and max(m_con.values())/total > 0.8 else 0

    def _calc_stale_penalty(self, w):
        last = w.get("last_active_ts", 0)
        if last == 0: return self.penalties["stale"]
        age = (time.time() - last) / 86400
        if age > 30: return self.penalties["stale"] * 2
        if age > 14: return self.penalties["stale"]
        return 0
