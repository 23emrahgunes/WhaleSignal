# PM-Edge TV-Direction Research Engine (Paper-Only)

PM-Edge TV-Direction is a high-frequency probability & direction intelligence research engine. It utilizes real-time BTCUSDT spot market prices and deep orderbook levels from Binance to analyze and calculate continuous mathematical probability and order-flow signals targeting active 5-minute binary outcome contracts on Polymarket.

---

### ⚠️ IMPORTANT: PAPER-ONLY POLICY
This project is exclusively designed for backtesting, academic research, and statistics.
- **NO trade execution**
- **NO order placement**
- **NO private key signatures / integrations**
- **NO autonomous execution capabilities**

All data logs, predictions, decisions, and analytics generated are purely academic and informational.

---

### Core Mechanics & Equations

1. **CDF-based Probability Model**:
   $$P_{\text{up}} = \Phi\left(\frac{\ln(S/K) + \mu T}{\sigma \sqrt{T}}\right)$$
   - $S$: Spot Price
   - $K$: target price (`priceToBeat`)
   - $T$: remaining time to expiry (annualized)
   - $\sigma$: annualized realized volatility (calculated from past 60s returns)
   - $\mu$: annualized expected short-term drift

2. **Order Flow Imbalance**:
   $$\text{Imbalance} = \frac{\text{BidVol} - \text{AskVol}}{\text{BidVol} + \text{AskVol}}$$
   $$\text{Weighted Imbalance} = \frac{\text{WeightedBidVol} - \text{WeightedAskVol}}{\text{WeightedBidVol} + \text{WeightedAskVol}}$$
   *Weighted volumes apply a squared-distance penalty relative to the current spot price.*

3. **In-Memory Candlesticks**:
   Generates live, thread-safe `1m` and `5m` candlestick bars using Binance `btcusdt@trade` data to feed a suite of 11 core technical indicator systems.

---

### Installation & Run

#### Prerequisites
- Go 1.25+
- Modern C SQLite driver (bundled)

#### Compilation & Running
```bash
# Setup Environment variables
cp .env.example .env

# Run the live prediction pipeline
go run ./cmd/pm-edge tv-direction
```

#### Makefile Targets
- `make build`: Compiles the binary
- `make run`: Starts the live paper engine
- `make test`: Runs unit tests
- `make fmt`: Standardizes code layouts using `gofmt`

---

### API Reference
- `GET /health`: Server diagnostic check (Returns "OK")
- `GET /api/live`: Real-time state details including spot prices, current active Polymarket question, computed parameters, indicator signals, and the final combined bias recommendation.
- `GET /api/history?limit=100`: Query historical computed records.
- `GET /api/market`: Polymarket contract metadata detail.
- `GET /api/orderflow`: Specific summary of bids and asks sizes and imbalances.

---

*Geoblock / Jurisdiction Notice: This tool serves purely analytical and simulation purposes and does not communicate with, or place transactions on, any restricted environments.*
