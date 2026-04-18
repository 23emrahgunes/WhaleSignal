import pandas as pd
import json
from src.api_clients import GammaClient
from src.config import CATEGORY_MAP

class MarketCensus:
    def __init__(self):
        self.gamma_client = GammaClient()
        self.markets = []

    def fetch_active_markets(self, limit=250):
        raw_markets = self.gamma_client.get_markets(limit=limit, active=True, closed=False)
        self.markets = self._normalize_markets(raw_markets)
        return self.markets

    def _normalize_markets(self, raw_markets):
        normalized = []
        for m in raw_markets:
            raw_cat = m.get("category")
            question = m.get("question", "")
            event_title = ""
            event_desc = ""
            if m.get("events"):
                event = m["events"][0]
                if not raw_cat: raw_cat = event.get("category")
                event_title = event.get("title", "")
                event_desc = event.get("description", "")

            market_info = {
                "market_id": m.get("id"),
                "question": question,
                "slug": m.get("slug"),
                "volume": float(m.get("volume", 0) or 0),
                "liquidity": float(m.get("liquidity", 0) or 0),
                "condition_id": m.get("conditionId"),
            }
            tokens = m.get("clobTokenIds", [])
            if isinstance(tokens, str):
                try: tokens = json.loads(tokens)
                except: tokens = []
            market_info["tokens"] = tokens
            market_info["category"] = self._map_category(raw_cat, question, event_title, event_desc)
            normalized.append(market_info)
        return normalized

    def _map_category(self, raw_cat, question, event_title, event_desc):
        if raw_cat and raw_cat in CATEGORY_MAP: return CATEGORY_MAP[raw_cat]
        text = f"{raw_cat or ''} {question} {event_title} {event_desc}".lower()
        if any(w in text for w in ["election", "trump", "biden", "politics"]): return "POLITICS"
        if any(w in text for w in ["bitcoin", "crypto", "solana"]): return "CRYPTO"
        if any(w in text for w in ["nba", "nfl", "soccer", "sports"]): return "SPORTS"
        return "OTHER"

    def get_market_universe(self):
        return pd.DataFrame(self.markets)
