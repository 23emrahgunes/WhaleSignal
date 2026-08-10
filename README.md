# PM-Edge TV-Direction Research Engine (Paper-Only)

PM-Edge is a read-only research engine for Polymarket's active BTC **Up or Down - 5 Minutes** markets. It uses the market's canonical 5-minute window, Chainlink BTC/USD reference data for settlement-aligned price inputs, and Binance BTCUSDT data for predictive order-flow and technical features.

## Paper-only policy

This repository intentionally contains **no order execution path**:

- no trade execution
- no order placement
- no private keys or signing
- no autonomous trading

If required market/reference data is missing or stale, the engine **fails closed and emits no signal** rather than inventing fallback values.

## Data model

### Polymarket market discovery

The current 5-minute event is derived from the UTC five-minute boundary:

```text
btc-updown-5m-{window_start_unix}
```

The event/market metadata is fetched from Gamma by exact event slug. There is no generic `$100,000` fallback market.

### Canonical price inputs

- **Price to beat:** Chainlink BTC/USD value anchored at the 5-minute window boundary.
- **Current settlement-aligned price:** fresh Chainlink BTC/USD RTDS value.
- **Mid-window startup fallback for PTB only:** Polymarket's read-only crypto reference-price endpoint. If unavailable, the market remains visible for metadata but no signal is emitted.
- **Binance BTCUSDT:** predictive/order-flow source only; it does not define the Polymarket settlement threshold.

### Binance features

The engine consumes BTCUSDT trades, depth, `1m`/`5m` candles and technical indicators. Returns are sampled at a one-second equivalent cadence before annualization. A freshness watchdog switches to Binance REST if the WebSocket silently stalls. Newly appeared oversized depth walls are treated as untrusted until they persist long enough to reduce transient-spoof contamination.

## Signal safety gates

A result is not generated or persisted unless all decision-critical conditions are valid, including:

- active canonical BTC 5m market
- positive real price-to-beat
- fresh Chainlink reference price
- fresh Binance price/features
- non-expired market window
- non-mock source

SQLite insertion independently validates these invariants. Migration removes the exact legacy synthetic fallback rows created by the old `btc-above-100k-1505` path.

## Local development

Prerequisite: Go 1.25+.

```bash
cp .env.example .env
go mod download
go vet ./...
go test -count=1 ./...
go test -race -count=1 ./...
go build -o pm-edge ./cmd/pm-edge
./pm-edge tv-direction
```

For an explicit synthetic runtime smoke test:

```bash
./pm-edge tv-direction --mock
```

Mock output is never written to the live research dataset.

## VPS deployment

### Recommended for small VPS instances

Do **not** compile `modernc.org/sqlite` locally on a ~1 GB / no-swap VPS unless necessary. GitHub Actions builds a tested Linux amd64 binary on every successful CI run.

1. Open the successful **PM-Edge CI** run.
2. Download the artifact named `pm-edge-linux-amd64`.
3. Extract `pm-edge-linux-amd64` into the repository directory on the VPS.
4. Run:

```bash
chmod +x deploy.sh pm-edge-linux-amd64
./deploy.sh
```

`deploy.sh` automatically prefers the prebuilt binary. If no prebuilt binary exists, it uses a serial low-memory Go build (`GOMAXPROCS=1`, `GOGC=20`, `-p=1`) and fails with a clear message if the host still cannot compile it.

To use a binary stored elsewhere:

```bash
PM_EDGE_PREBUILT=/path/to/pm-edge-linux-amd64 ./deploy.sh
```

The deployment script does not require `sudo`, does not rewrite `~/.bashrc`, and verifies `/health` before declaring success.

## API

- `GET /health` — process health check
- `GET /api/live` — current validated signal state
- `GET /api/history?limit=100` — persisted validated history
- `GET /api/market` — current market metadata
- `GET /api/orderflow` — order-flow summary

## CI quality gate

The GitHub Actions workflow must pass all of the following before a revision is considered deployable:

```text
gofmt cleanliness
go mod download
go vet ./...
go test -count=1 ./...
go test -race -count=1 ./...
Linux amd64 build
runtime /health smoke test
artifact upload
```

## Jurisdiction / geoblock notice

This project is analytical and simulation-only. It does not place transactions or bypass access restrictions. Any future execution component would need to remain disabled in restricted jurisdictions and comply with applicable platform and local requirements.
