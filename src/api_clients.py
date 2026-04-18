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
        params = {"limit": limit, "offset": offset, "closed": str(closed).lower(), "active": str(active).lower()}
        return self._get("markets", params=params)

class DataClient(BaseClient):
    def __init__(self):
        super().__init__(DATA_API_URL)

    def get_trades(self, user_address=None, asset=None, limit=100):
        params = {"limit": limit}
        if user_address: params["user"] = user_address
        if asset: params["asset"] = asset
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

class CLOBClient(BaseClient):
    def __init__(self):
        super().__init__(CLOB_API_URL)

    def get_orderbook(self, token_id):
        return self._get("book", params={"token_id": token_id})
