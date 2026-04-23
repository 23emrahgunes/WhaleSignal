from typing import Dict, List


class StableWalletSelector:
    def __init__(self, min_final_score: float = 0.7, min_opportunity_score: float = 0.65):
        self.min_final_score = min_final_score
        self.min_opportunity_score = min_opportunity_score

    def select(self, scored_wallets: List[Dict], archetypes: List[Dict], opportunities: List[Dict]) -> List[Dict]:
        scored_map = {w.get('address'): w for w in scored_wallets}
        arche_map = {w.get('address'): w for w in archetypes}
        selected = []

        for opp in opportunities:
            address = opp.get('address')
            scored = scored_map.get(address, {})
            arche = arche_map.get(address, {})
            policy = opp.get('policy', {}) or {}

            if opp.get('decision') != 'FOLLOW':
                continue
            if policy.get('policy_action') == 'BLOCK':
                continue
            if float(opp.get('opportunity_score', 0) or 0) < self.min_opportunity_score:
                continue
            if float(scored.get('final_score', 0) or 0) < self.min_final_score:
                continue
            if arche.get('archetype') == 'Noisy / Unfollowable':
                continue

            row = {
                'address': address,
                'final_score': scored.get('final_score'),
                'tier': scored.get('tier'),
                'archetype': arche.get('archetype'),
                'archetype_confidence': arche.get('archetype_confidence'),
                'opportunity_score': opp.get('opportunity_score'),
                'policy_action': policy.get('policy_action', 'ALLOW'),
                'suggested_size_multiplier': policy.get('suggested_size_multiplier', 1.0),
                'dominant_category': (arche.get('metrics') or {}).get('dominant_category', 'OTHER'),
                'copyability_score': (arche.get('metrics') or {}).get('copyability_score', 0),
                'drawdown_risk_score': (arche.get('metrics') or {}).get('drawdown_risk_score', 0),
            }
            selected.append(row)

        selected.sort(key=lambda x: (x.get('opportunity_score', 0), x.get('final_score', 0), x.get('archetype_confidence', 0)), reverse=True)
        return selected
