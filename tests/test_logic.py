import pytest
import pandas as pd
from src.market_census import MarketCensus
from src.wallet_quality import WalletQualityScorer

def test_market_census_normalization():
    census = MarketCensus()
    raw_markets = [{"id": "1", "question": "Will Bitcoin hit $100k?", "volume": "1000", "liquidity": "500", "category": "Crypto"}]
    normalized = census._normalize_markets(raw_markets)
    assert len(normalized) == 1
    assert normalized[0]["category"] == "CRYPTO"

def test_wallet_quality_scorer():
    scorer = WalletQualityScorer()
    wallet = {"address": "0x123", "trades_7d": 10, "trades_30d": 20, "trades_90d": 30, "current_value": 1000, "realized_pnl": 100, "total_trades": 50, "categories": {"CRYPTO": 45, "SPORTS": 5}, "last_active_ts": 0, "liquidity_exposure": [{"liquidity": 100000}]}
    import time; wallet["last_active_ts"] = time.time()
    score = scorer.score_wallet(wallet)
    assert score["final_score"] > 0
    assert "tier" in score

def test_stale_penalty_thresholds():
    import time; scorer = WalletQualityScorer(); stale_weight = scorer.penalties["stale"]
    assert scorer._calc_stale_penalty({"last_active_ts": time.time()}) == 0
    assert scorer._calc_stale_penalty({"last_active_ts": time.time() - (15 * 86400)}) == stale_weight
    assert scorer._calc_stale_penalty({"last_active_ts": time.time() - (31 * 86400)}) == stale_weight * 2
