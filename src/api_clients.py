import requests
from src.config import GAMMA_API_URL, DATA_API_URL, CLOB_API_URL

class BaseClient:
    def __init__(self, base_url):
        self.base_url = base_url

    def _get(self, endpoint, params=None):
        url = f"{self.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        response = requests.get(url, params=params)
        response.raise_for_status()
        return response.json()

class GammaClient(BaseClient):
    def __init__(self):
        super().__init__(GAMMA_API_URL)

    def get_markets(self, limit=100, offset=0, closed=False, active=True):
        params = {
            "limit": limit,
            "offset": offset,
            "closed": str(closed).lower(),
            "active": str(active).lower(),
        }
        return self._get("markets", params=params)

    def get_market(self, market_id):
        return self._get(f"markets/{market_id}")

    def get_events(self, limit=100, offset=0):
        params = {"limit": limit, "offset": offset}
        return self._get("events", params=params)

class DataClient(BaseClient):
    def __init__(self):
        super().__init__(DATA_API_URL)

    def get_trades(self, user_address=None, market_id=None, limit=100):
        params = {"limit": limit}
        if user_address:
            params["user"] = user_address
        if market_id:
            params["market_id"] = market_id
        return self._get("trades", params=params)

    def get_positions(self, user_address, limit=100):
        params = {"user": user_address, "limit": limit}
        return self._get("positions", params=params)

    def get_closed_positions(self, user_address, limit=100):
        params = {"user": user_address, "limit": limit}
        return self._get("closed-positions", params=params)

    def get_activity(self, user_address, limit=100):
        params = {"user": user_address, "limit": limit}
        return self._get("activity", params=params)

    def get_holders(self, token_id, limit=100):
        params = {"token_id": token_id, "limit": limit}
        return self._get("holders", params=params)

class CLOBClient(BaseClient):
    def __init__(self):
        super().__init__(CLOB_API_URL)

    def get_orderbook(self, token_id):
        params = {"token_id": token_id}
        return self._get("book", params=params)

    def get_price(self, token_id):
        params = {"token_id": token_id}
        return self._get("price", params=params)

    def get_spread(self, token_id):
        # Implementation depends on actual endpoint return structure
        book = self.get_orderbook(token_id)
        # Simplified spread calculation
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if bids and asks:
            best_bid = float(bids[0]["price"])
            best_ask = float(asks[0]["price"])
            return best_ask - best_bid
        return None
