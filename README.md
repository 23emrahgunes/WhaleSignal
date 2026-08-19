# Direction Engine vNext

SHADOW-only direction-data service for BTC / ETH / SOL / XRP across 5m, 15m and 1h Polymarket Up/Down markets.

## P1 invariants

- No live orders, signatures or private keys.
- `PHASE=P1` hard-disables model training and calibration.
- 5m/15m official opening reference follows the Polymarket market rule's **Chainlink Data Stream** lineage via public Polymarket RTDS (`crypto_prices_chainlink`). Binance opening prices are analytics-only proxies and are never promoted to PTB.
- 1h official opening reference is the Binance 1h candle OPEN aligned to the canonical ET-slot market start.
- CLOB market WebSocket parsing supports `book`, official `price_changes[]`, and `best_bid_ask` events and routes by token `asset_id`.
- Snapshot labels are written only when official and computed results MATCH; UNKNOWN/MISMATCH remain training-ineligible.

## Local verification

```bash
python -m py_compile *.py reference/*.py
pytest -q
```

Protocol-level regressions live in `test_protocol_regressions.py` and exercise documented RTDS/CLOB wire shapes rather than store helpers alone.

## Run

```bash
python -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python main.py
```

Dashboard defaults to `http://0.0.0.0:8091/`; API state is available at `/api/state`.

## Runtime P1 acceptance

After deploying a fresh commit and allowing at least one new 5m/15m market rotation, verify:

- Chainlink status contains BTC/ETH/SOL/XRP and connection is `connected`.
- `ptb_states_healthy` reaches the expected active-market count when boundary observations are available.
- `clob_price_change_events` and/or `clob_best_bid_ask_events` increase while quotes remain fresh.
- safety counters remain zero: model learn/save/calibration/live orders.
- real resolved markets are recorded; only MATCH rows receive `final_result`.
