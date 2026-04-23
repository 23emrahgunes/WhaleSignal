import json
import os
import time
from typing import Dict, List


class PaperCopyTrader:
    def __init__(self, state_path: str = "reports/copytrader_paper_state.json", starting_capital: float = 10000.0, max_wallets: int = 5, max_positions_per_wallet: int = 3):
        self.state_path = state_path
        self.starting_capital = starting_capital
        self.max_wallets = max_wallets
        self.max_positions_per_wallet = max_positions_per_wallet

    def _default_state(self) -> Dict:
        return {
            "starting_capital": self.starting_capital,
            "available_capital": self.starting_capital,
            "selected_wallets": [],
            "open_positions": [],
            "closed_positions": [],
            "activity_log": [],
            "last_run_at": None,
        }

    def load_state(self) -> Dict:
        if not os.path.exists(self.state_path):
            return self._default_state()
        with open(self.state_path, "r") as f:
            state = json.load(f)
        default = self._default_state()
        default.update(state)
        return default

    def save_state(self, state: Dict) -> None:
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        with open(self.state_path, "w") as f:
            json.dump(state, f, indent=2)

    def _wallet_budget(self, stable_wallet: Dict) -> float:
        base_budget = self.starting_capital / max(1, self.max_wallets)
        multiplier = float(stable_wallet.get("suggested_size_multiplier", 1.0) or 1.0)
        return round(base_budget * multiplier, 4)

    def _build_source_positions(self, stable_wallet: Dict, enriched_wallet: Dict) -> List[Dict]:
        positions = enriched_wallet.get("open_positions_snapshot", []) or []
        if positions:
            return positions[: self.max_positions_per_wallet]

        fallback_value = float(enriched_wallet.get("current_value", 0) or 0)
        if fallback_value <= 0:
            fallback_value = 100.0
        return [
            {
                "position_id": "wallet-basket",
                "asset": "",
                "market_id": stable_wallet.get("dominant_category", "OTHER"),
                "market_label": f"{stable_wallet.get('dominant_category', 'OTHER')} wallet basket",
                "outcome": "COPY_BASKET",
                "current_value": fallback_value,
                "realized_pnl": 0.0,
                "size": 1.0,
                "synthetic": True,
            }
        ]

    def sync(self, stable_wallets: List[Dict], enriched_wallets: List[Dict]) -> Dict:
        state = self.load_state()
        now = int(time.time())
        enriched_map = {w.get("address"): w for w in enriched_wallets}
        selected_wallets = stable_wallets[: self.max_wallets]
        state["selected_wallets"] = selected_wallets

        existing_open = {p.get("tracking_key"): p for p in state.get("open_positions", [])}
        new_open_positions = []
        current_keys = set()

        for stable_wallet in selected_wallets:
            address = stable_wallet.get("address")
            enriched_wallet = enriched_map.get(address, {})
            source_positions = self._build_source_positions(stable_wallet, enriched_wallet)
            total_source_value = sum(float(p.get("current_value", 0) or 0) for p in source_positions)
            if total_source_value <= 0:
                total_source_value = float(len(source_positions) or 1)
            wallet_budget = self._wallet_budget(stable_wallet)

            for position in source_positions:
                position_id = str(position.get("position_id") or position.get("asset") or position.get("market_id") or "unknown")
                tracking_key = f"{address}:{position_id}"
                current_keys.add(tracking_key)
                weight = float(position.get("current_value", 0) or 0) / total_source_value if total_source_value else 1.0
                if weight <= 0:
                    weight = 1.0 / max(1, len(source_positions))
                allocation = round(wallet_budget * weight, 4)

                existing = existing_open.get(tracking_key)
                if existing:
                    existing["allocation"] = allocation
                    existing["last_seen_at"] = now
                    existing["source_value"] = float(position.get("current_value", 0) or 0)
                    new_open_positions.append(existing)
                    continue

                new_position = {
                    "tracking_key": tracking_key,
                    "status": "OPEN",
                    "opened_at": now,
                    "last_seen_at": now,
                    "source_wallet": address,
                    "source_position_id": position_id,
                    "market_id": position.get("market_id"),
                    "market_label": position.get("market_label"),
                    "outcome": position.get("outcome"),
                    "allocation": allocation,
                    "source_value": float(position.get("current_value", 0) or 0),
                    "suggested_size_multiplier": stable_wallet.get("suggested_size_multiplier", 1.0),
                    "synthetic": bool(position.get("synthetic", False)),
                }
                state["activity_log"].append({
                    "ts": now,
                    "action": "OPEN",
                    "tracking_key": tracking_key,
                    "source_wallet": address,
                    "allocation": allocation,
                })
                new_open_positions.append(new_position)

        still_open = []
        for position in new_open_positions:
            still_open.append(position)

        previously_open = state.get("open_positions", [])
        for position in previously_open:
            if position.get("tracking_key") in current_keys:
                continue
            closed = dict(position)
            closed["status"] = "CLOSED"
            closed["closed_at"] = now
            state["closed_positions"].append(closed)
            state["activity_log"].append({
                "ts": now,
                "action": "CLOSE",
                "tracking_key": position.get("tracking_key"),
                "source_wallet": position.get("source_wallet"),
            })

        state["open_positions"] = still_open
        state["available_capital"] = round(max(0.0, self.starting_capital - sum(float(p.get("allocation", 0) or 0) for p in still_open)), 4)
        state["last_run_at"] = now
        self.save_state(state)
        return state
