from collections import Counter


class WalletArchetypeClassifier:
    def __init__(self):
        self.archetypes = [
            "Stable Compounder",
            "Crypto Scalper",
            "Event Sniper",
            "High-Risk Sprinter",
            "Noisy / Unfollowable",
        ]

    def classify_wallet(self, scored_wallet, enriched_wallet):
        final_score = float(scored_wallet.get("final_score", 0))
        sub_scores = scored_wallet.get("sub_scores", {})
        penalties = scored_wallet.get("penalties", {})
        categories = enriched_wallet.get("categories", {})
        market_concentration = enriched_wallet.get("market_concentration", {})

        realized_total = float(enriched_wallet.get("realized_pnl_open", 0)) + float(enriched_wallet.get("realized_pnl_closed", 0))
        total_trades = max(1, int(enriched_wallet.get("total_trades", 0)))
        trades_7d = int(enriched_wallet.get("trades_7d", 0))
        trades_30d = int(enriched_wallet.get("trades_30d", 0))
        avg_liquidity = self._avg_liquidity(enriched_wallet)
        category_specialization = self._category_specialization(categories)

        pnl_velocity_1d = round(realized_total / max(1, min(trades_7d, 7)), 4)
        pnl_velocity_7d = round(realized_total / max(1, trades_30d), 4)
        trade_frequency_score = round(min(1.0, total_trades / 80.0), 4)
        copyability_score = round(
            min(
                1.0,
                max(
                    0.0,
                    final_score * 0.35
                    + float(sub_scores.get("followability", 0)) * 0.35
                    + float(sub_scores.get("liquidity_adjusted", 0)) * 0.20
                    - float(penalties.get("stale", 0)) * 0.5
                    - float(penalties.get("noise", 0)) * 0.25,
                ),
            ),
            4,
        )
        drawdown_risk_score = round(
            min(
                1.0,
                max(
                    0.0,
                    float(penalties.get("concentration", 0)) * 2.5
                    + float(penalties.get("stale", 0)) * 2.0
                    + float(penalties.get("noise", 0)) * 1.5
                    + self._max_market_share(market_concentration) * 0.35,
                ),
            ),
            4,
        )

        archetype, confidence, reason_codes = self._resolve_archetype(
            final_score=final_score,
            copyability_score=copyability_score,
            drawdown_risk_score=drawdown_risk_score,
            trade_frequency_score=trade_frequency_score,
            pnl_velocity_1d=pnl_velocity_1d,
            pnl_velocity_7d=pnl_velocity_7d,
            top_category=self._top_category(categories),
            category_specialization=category_specialization,
            consistency=float(sub_scores.get("consistency", 0)),
            realized_quality=float(sub_scores.get("realized_quality", 0)),
            followability=float(sub_scores.get("followability", 0)),
            avg_liquidity=avg_liquidity,
            trades_7d=trades_7d,
        )

        return {
            "address": scored_wallet.get("address"),
            "archetype": archetype,
            "archetype_confidence": confidence,
            "pnl_velocity_1d": pnl_velocity_1d,
            "pnl_velocity_7d": pnl_velocity_7d,
            "trade_frequency_score": trade_frequency_score,
            "copyability_score": copyability_score,
            "drawdown_risk_score": drawdown_risk_score,
            "category_specialization_score": round(category_specialization, 4),
            "top_category": self._top_category(categories),
            "reason_codes": reason_codes,
        }

    def classify_many(self, scored_wallets, enriched_wallets):
        enriched_map = {w["address"]: w for w in enriched_wallets}
        results = []
        for scored in scored_wallets:
            enriched = enriched_map.get(scored.get("address"))
            if not enriched:
                continue
            results.append(self.classify_wallet(scored, enriched))
        return results

    def _resolve_archetype(self, **kwargs):
        reasons = []
        top_category = kwargs["top_category"]
        if kwargs["final_score"] >= 0.75 and kwargs["copyability_score"] >= 0.65 and kwargs["drawdown_risk_score"] <= 0.35 and kwargs["consistency"] >= 0.65:
            reasons.extend(["high_final_score", "high_copyability", "low_drawdown"])
            return "Stable Compounder", 0.82, reasons
        if top_category == "CRYPTO" and kwargs["trade_frequency_score"] >= 0.55 and kwargs["pnl_velocity_1d"] > 0 and kwargs["avg_liquidity"] >= 50000:
            reasons.extend(["crypto_specialist", "high_trade_frequency"])
            return "Crypto Scalper", 0.73, reasons
        if kwargs["category_specialization"] >= 0.7 and kwargs["realized_quality"] >= 0.6 and kwargs["trades_7d"] <= 12:
            reasons.extend(["category_specialist", "event_timing_profile"])
            return "Event Sniper", 0.68, reasons
        if kwargs["pnl_velocity_7d"] > 0 and kwargs["drawdown_risk_score"] >= 0.45 and kwargs["copyability_score"] < 0.65:
            reasons.extend(["fast_pnl_profile", "elevated_risk"])
            return "High-Risk Sprinter", 0.66, reasons
        reasons.extend(["low_copyability_or_quality", "fallback_classification"])
        return "Noisy / Unfollowable", 0.58, reasons

    def _avg_liquidity(self, enriched_wallet):
        exposures = enriched_wallet.get("liquidity_exposure", [])
        if not exposures:
            return 0.0
        vals = [float(item.get("liquidity", 0) or 0) for item in exposures]
        return sum(vals) / max(1, len(vals))

    def _top_category(self, categories):
        if not categories:
            return "OTHER"
        return Counter(categories).most_common(1)[0][0] if isinstance(categories, Counter) else max(categories.items(), key=lambda x: x[1])[0]

    def _category_specialization(self, categories):
        if not categories:
            return 0.0
        total = sum(categories.values())
        if total <= 0:
            return 0.0
        return max(categories.values()) / total

    def _max_market_share(self, market_concentration):
        if not market_concentration:
            return 0.0
        total = sum(market_concentration.values())
        if total <= 0:
            return 0.0
        return max(market_concentration.values()) / total
