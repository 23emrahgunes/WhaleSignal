from typing import Dict, List, Optional


class WalletArchetypeEngine:
    def _safe_div(self, a: float, b: float) -> float:
        return a / b if b else 0.0

    def _dominant_category(self, enriched_wallet: Dict) -> str:
        categories = enriched_wallet.get("categories", {}) or {}
        if not categories:
            return "OTHER"
        return max(categories, key=categories.get)

    def _category_specialization_score(self, enriched_wallet: Dict) -> float:
        categories = enriched_wallet.get("categories", {}) or {}
        total = sum(categories.values())
        if total <= 0:
            return 0.0
        return max(categories.values()) / total

    def _avg_liquidity(self, enriched_wallet: Dict) -> float:
        exposure = enriched_wallet.get("liquidity_exposure", []) or []
        if not exposure:
            return 0.0
        return sum(float(x.get("liquidity", 0) or 0) for x in exposure) / len(exposure)

    def compute_metrics(self, scored_wallet: Dict, enriched_wallet: Dict) -> Dict:
        realized_total = float(enriched_wallet.get("realized_pnl_open", 0) or 0) + float(enriched_wallet.get("realized_pnl_closed", 0) or 0)
        total_trades = float(enriched_wallet.get("total_trades", 0) or 0)
        trades_7d = float(enriched_wallet.get("trades_7d", 0) or 0)
        trades_30d = float(enriched_wallet.get("trades_30d", 0) or 0)
        current_value = float(enriched_wallet.get("current_value", 0) or 0)
        sub_scores = scored_wallet.get("sub_scores", {}) or {}
        penalties = scored_wallet.get("penalties", {}) or {}

        pnl_velocity_1d = round(self._safe_div(realized_total, max(1.0, min(7.0, trades_7d or 1.0))), 4)
        pnl_velocity_7d = round(self._safe_div(realized_total, max(1.0, trades_7d or 1.0)), 4)
        trade_frequency_score = round(min(1.0, self._safe_div(trades_30d, 30.0)), 4)
        category_specialization_score = round(self._category_specialization_score(enriched_wallet), 4)
        avg_liquidity = self._avg_liquidity(enriched_wallet)
        liquidity_score = min(1.0, self._safe_div(avg_liquidity, 150000.0))
        copyability_score = max(0.0, min(1.0, (sub_scores.get("followability", 0) * 0.55) + (liquidity_score * 0.25) + ((1.0 - penalties.get("noise", 0)) * 0.10) + ((1.0 - penalties.get("stale", 0)) * 0.10)))
        drawdown_risk_score = max(0.0, min(1.0, (penalties.get("stale", 0) * 0.35) + (penalties.get("concentration", 0) * 0.35) + (penalties.get("noise", 0) * 0.20) + ((1.0 - liquidity_score) * 0.10)))
        roi_proxy = self._safe_div(realized_total, max(1.0, current_value))

        return {
            "dominant_category": self._dominant_category(enriched_wallet),
            "pnl_velocity_1d": pnl_velocity_1d,
            "pnl_velocity_7d": pnl_velocity_7d,
            "trade_frequency_score": trade_frequency_score,
            "category_specialization_score": category_specialization_score,
            "copyability_score": round(copyability_score, 4),
            "drawdown_risk_score": round(drawdown_risk_score, 4),
            "liquidity_score": round(liquidity_score, 4),
            "roi_proxy": round(roi_proxy, 4),
            "realized_total": round(realized_total, 4),
            "total_trades": int(total_trades),
        }

    def _classify(self, scored_wallet: Dict, metrics: Dict) -> Dict:
        final_score = float(scored_wallet.get("final_score", 0) or 0)
        category = metrics["dominant_category"]
        copyability = metrics["copyability_score"]
        risk = metrics["drawdown_risk_score"]
        trade_frequency = metrics["trade_frequency_score"]
        specialization = metrics["category_specialization_score"]
        velocity = metrics["pnl_velocity_7d"]

        archetype = "Noisy / Unfollowable"
        confidence = 0.55
        reason_codes: List[str] = []

        if final_score >= 0.75 and copyability >= 0.7 and risk <= 0.35:
            archetype = "Stable Compounder"
            confidence = 0.84
            reason_codes.extend(["high_final_score", "high_copyability", "low_risk"])
        elif category == "CRYPTO" and trade_frequency >= 0.55 and copyability >= 0.5:
            archetype = "Crypto Scalper"
            confidence = 0.78
            reason_codes.extend(["crypto_dominant", "high_trade_frequency"])
        elif specialization >= 0.7 and final_score >= 0.55:
            archetype = "Event Sniper"
            confidence = 0.74
            reason_codes.extend(["high_specialization", "targeted_category_edge"])
        elif velocity > 10 and risk >= 0.45:
            archetype = "High-Risk Sprinter"
            confidence = 0.73
            reason_codes.extend(["high_velocity", "elevated_risk"])
        else:
            reason_codes.extend(["low_copyability_or_score", "needs_manual_review"])

        return {
            "archetype": archetype,
            "archetype_confidence": round(confidence, 4),
            "reason_codes": reason_codes,
        }

    def classify_wallet(self, scored_wallet: Dict, enriched_wallet: Optional[Dict] = None) -> Dict:
        enriched_wallet = enriched_wallet or {}
        metrics = self.compute_metrics(scored_wallet, enriched_wallet)
        classification = self._classify(scored_wallet, metrics)
        return {
            "address": scored_wallet.get("address"),
            "final_score": scored_wallet.get("final_score"),
            "tier": scored_wallet.get("tier"),
            **classification,
            "metrics": metrics,
        }

    def classify_many(self, scored_wallets: List[Dict], enriched_wallets: List[Dict]) -> List[Dict]:
        enriched_map = {w.get("address"): w for w in enriched_wallets}
        results = [self.classify_wallet(w, enriched_map.get(w.get("address"), {})) for w in scored_wallets]
        results.sort(key=lambda x: (x.get("archetype_confidence", 0), x.get("final_score", 0)), reverse=True)
        return results
