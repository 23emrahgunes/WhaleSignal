import pandas as pd
import json
from src.api_clients import GammaClient
from src.config import CATEGORY_MAP

class MarketCensus:
    def __init__(self):
        self.gamma_client = GammaClient()
        self.markets = []

    def fetch_active_markets(self, limit=250):
        """Fetch active markets from Gamma API and normalize categories."""
        raw_markets = self.gamma_client.get_markets(limit=limit, active=True, closed=False)
        self.markets = self._normalize_markets(raw_markets)
        return self.markets

    def _normalize_markets(self, raw_markets):
        normalized = []
        for m in raw_markets:
            # Extract possible category sources
            raw_cat = m.get("category")
            event_title = ""
            event_desc = ""

            if m.get("events"):
                event = m["events"][0]
                if not raw_cat:
                    raw_cat = event.get("category")
                event_title = event.get("title", "")
                event_desc = event.get("description", "")

            question = m.get("question", "")

            # Extract basic info
            market_info = {
                "market_id": m.get("id"),
                "question": question,
                "slug": m.get("slug"),
                "end_date": m.get("endDate"),
                "active": m.get("active"),
                "closed": m.get("closed"),
                "category_raw": raw_cat,
                "group_item_title": m.get("groupItemTitle"),
                "volume": float(m.get("volume", 0) or 0),
                "liquidity": float(m.get("liquidity", 0) or 0),
            }

            market_info["condition_id"] = m.get("conditionId")

            # tokens can be a string representation of a list in Gamma response
            tokens = m.get("clobTokenIds", [])
            if isinstance(tokens, str):
                try:
                    tokens = json.loads(tokens)
                except:
                    tokens = []
            market_info["tokens"] = tokens

            # Normalize Category
            market_info["category"] = self._map_category(raw_cat, question, event_title, event_desc)

            normalized.append(market_info)
        return normalized

    def _map_category(self, raw_cat, question, event_title, event_desc):
        # 1. Direct map
        if raw_cat and raw_cat in CATEGORY_MAP:
            return CATEGORY_MAP[raw_cat]

        # 2. Text based search
        search_text = f"{raw_cat or ''} {question} {event_title} {event_desc}".lower()

        if any(w in search_text for w in ["election", "trump", "biden", "harris", "republican", "democrat", "governor", "senate", "politics"]):
            return "POLITICS"
        if any(w in search_text for w in ["bitcoin", "eth", "crypto", "solana", "binance", "coinbase"]):
            return "CRYPTO"
        if any(w in search_text for w in ["nba", "nfl", "mlb", "nhl", "soccer", "football", "tennis", "ufc", "sports"]):
            return "SPORTS"

        return "OTHER"

    def get_market_universe(self):
        return pd.DataFrame(self.markets)

if __name__ == "__main__":
    census = MarketCensus()
    markets = census.fetch_active_markets(limit=100)
    print(f"Fetched {len(markets)} markets.")
    df = census.get_market_universe()
    if not df.empty:
        print(df["category"].value_counts())
        print(df[["question", "category", "volume"]].head())
