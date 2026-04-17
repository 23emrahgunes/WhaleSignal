import os
from dotenv import load_dotenv

load_dotenv()

# API Base URLs
GAMMA_API_URL = os.getenv("GAMMA_API_URL", "https://gamma-api.polymarket.com")
DATA_API_URL = os.getenv("DATA_API_URL", "https://data-api.polymarket.com")
CLOB_API_URL = os.getenv("CLOB_API_URL", "https://clob.polymarket.com")

# Category Mapping
CATEGORY_MAP = {
    "Politics": "POLITICS",
    "Crypto": "CRYPTO",
    "Sports": "SPORTS",
    "Entertainment": "OTHER",
    "Science": "OTHER",
    "Business": "OTHER",
    # Add more mappings as discovered from Gamma API
}

# Scoring Weights
WEIGHTS = {
    "consistency": 0.22,
    "realized_quality": 0.20,
    "recency": 0.14,
    "category_strength": 0.14,
    "liquidity_adjusted": 0.15,
    "followability": 0.15,
}

PENALTIES = {
    "concentration": 0.1,
    "stale": 0.1,
    "noise": 0.1,
}

# Thresholds
LIQUIDITY_THRESHOLDS = {
    "low_spread": 0.04,
    "high_spread": 0.08,
}

# Time Windows (days)
WINDOWS = {
    "short": 7,
    "mid": 30,
    "long": 90,
}

WINDOW_WEIGHTS = {
    "short": 0.25,
    "mid": 0.45,
    "long": 0.30,
}
