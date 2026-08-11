from pathlib import Path


def replace(path, old, new, count=1):
    p = Path(path)
    s = p.read_text()
    if old not in s:
        raise SystemExit(f'missing marker in {path}: {old[:120]!r}')
    p.write_text(s.replace(old, new, count))

# SQLite: one connection for all in-process readers/writers + WAL/busy timeout.
replace('internal/storage/sqlite.go', '''\tdb, err := sql.Open("sqlite", dbPath)\n\tif err != nil {\n\t\treturn nil, err\n\t}\n\tinst := &Database{db: db}''', '''\tdb, err := sql.Open("sqlite", dbPath)\n\tif err != nil {\n\t\treturn nil, err\n\t}\n\t// modernc/sqlite opens multiple connections by default. The 5m runtime,\n\t// 15m runtime, settlement loop and research writer can then contend for\n\t// SQLite's single writer lock. Serialize access inside this small service\n\t// and keep a busy timeout as a second line of defense.\n\tdb.SetMaxOpenConns(1)\n\tdb.SetMaxIdleConns(1)\n\tfor _, pragma := range []string{\n\t\t"PRAGMA busy_timeout=5000",\n\t\t"PRAGMA journal_mode=WAL",\n\t\t"PRAGMA synchronous=NORMAL",\n\t} {\n\t\tif _, err := db.Exec(pragma); err != nil {\n\t\t\t_ = db.Close()\n\t\t\treturn nil, fmt.Errorf("sqlite setup %q: %w", pragma, err)\n\t\t}\n\t}\n\tinst := &Database{db: db}''')

# Prefer Binance's market-data-only host as an additional WS route.
replace('internal/binance/microstructure.go', '''var deepWSURLs = []string{\n\t"wss://stream.binance.com:9443/stream?streams=btcusdt@depth@100ms/btcusdt@aggTrade",\n\t"wss://stream.binance.com:443/stream?streams=btcusdt@depth@100ms/btcusdt@aggTrade",\n}''', '''var deepWSURLs = []string{\n\t"wss://data-stream.binance.vision/stream?streams=btcusdt@depth@100ms/btcusdt@aggTrade",\n\t"wss://stream.binance.com:9443/stream?streams=btcusdt@depth@100ms/btcusdt@aggTrade",\n\t"wss://stream.binance.com:443/stream?streams=btcusdt@depth@100ms/btcusdt@aggTrade",\n}\n\nvar deepRESTBases = []string{\n\t"https://api.binance.com",\n\t"https://api-gcp.binance.com",\n\t"https://data-api.binance.vision",\n}''')

replace('internal/binance/microstructure.go', '''\tLastUpdateID       int64         `json:"lastUpdateId"`\n\tLastTradeAgeMs     int64         `json:"lastTradeAgeMs"`\n\tTradeFlowAvailable bool          `json:"tradeFlowAvailable"`''', '''\tLastUpdateID       int64         `json:"lastUpdateId"`\n\tLastTradeAgeMs     int64         `json:"lastTradeAgeMs"`\n\tTradeFlowAvailable bool          `json:"tradeFlowAvailable"`\n\tPTBPrice           float64       `json:"ptbPrice"`''')

replace('internal/binance/microstructure.go', '''\tlastTradeTime time.Time\n\tsource        string''', '''\tlastTradeTime time.Time\n\tlastAggTradeID int64\n\tsource        string''')

replace('internal/binance/microstructure.go', '''func (c *MicrostructureClient) Start() {\n\tc.wg.Add(1)\n\tgo c.run()\n}''', '''func (c *MicrostructureClient) Start() {\n\tc.wg.Add(2)\n\tgo c.run()\n\tgo c.runRESTFallback()\n}''')

replace('internal/binance/microstructure.go', '''type aggTradeEvent struct {\n\tEventTime    int64  `json:"E"`\n\tPrice        string `json:"p"`\n\tQuantity     string `json:"q"`\n\tBuyerIsMaker bool   `json:"m"`\n}''', '''type aggTradeEvent struct {\n\tEventTime       int64  `json:"E"`\n\tAggregateTradeID int64  `json:"a"`\n\tPrice           string `json:"p"`\n\tQuantity        string `json:"q"`\n\tTradeTime       int64  `json:"T"`\n\tBuyerIsMaker    bool   `json:"m"`\n}\n\ntype aggTradeRESTEvent struct {\n\tAggregateTradeID int64  `json:"a"`\n\tPrice            string `json:"p"`\n\tQuantity         string `json:"q"`\n\tTradeTime        int64  `json:"T"`\n\tBuyerIsMaker     bool   `json:"m"`\n}''')

replace('internal/binance/microstructure.go', '''\t\t\tif errP == nil && errQ == nil && price > 0 && qty > 0 {\n\t\t\t\tc.recordTrade(price, qty, ev.BuyerIsMaker, time.UnixMilli(ev.EventTime).UTC())\n\t\t\t}\n''', '''\t\t\tif errP == nil && errQ == nil && price > 0 && qty > 0 {\n\t\t\t\tts := ev.TradeTime\n\t\t\t\tif ts <= 0 {\n\t\t\t\t\tts = ev.EventTime\n\t\t\t\t}\n\t\t\t\tc.recordTradeWithID(price, qty, ev.BuyerIsMaker, time.UnixMilli(ts).UTC(), ev.AggregateTradeID)\n\t\t\t}\n''')

replace('internal/binance/microstructure.go', '''func (c *MicrostructureClient) recordTrade(price, qty float64, buyerIsMaker bool, ts time.Time) {\n\tnotional := price * qty\n\ttr := aggressiveTrade{Time: ts}\n\tif buyerIsMaker {\n\t\ttr.SellUSD = notional\n\t} else {\n\t\ttr.BuyUSD = notional\n\t}\n\tcutoff := ts.Add(-tradeFlowRetention)\n\tc.mu.Lock()\n\tdefer c.mu.Unlock()\n\tc.trades = append(c.trades, tr)\n\tfirst := 0\n\tfor first < len(c.trades) && c.trades[first].Time.Before(cutoff) {\n\t\tfirst++\n\t}\n\tif first > 0 {\n\t\tc.trades = append([]aggressiveTrade(nil), c.trades[first:]...)\n\t}\n\tc.lastTradeTime = ts\n}''', '''func (c *MicrostructureClient) recordTrade(price, qty float64, buyerIsMaker bool, ts time.Time) {\n\tc.recordTradeWithID(price, qty, buyerIsMaker, ts, 0)\n}\n\nfunc (c *MicrostructureClient) recordTradeWithID(price, qty float64, buyerIsMaker bool, ts time.Time, aggregateID int64) {\n\tnotional := price * qty\n\ttr := aggressiveTrade{Time: ts}\n\tif buyerIsMaker {\n\t\ttr.SellUSD = notional\n\t} else {\n\t\ttr.BuyUSD = notional\n\t}\n\tcutoff := ts.Add(-tradeFlowRetention)\n\tc.mu.Lock()\n\tdefer c.mu.Unlock()\n\tif aggregateID > 0 {\n\t\tif aggregateID <= c.lastAggTradeID {\n\t\t\treturn\n\t\t}\n\t\tc.lastAggTradeID = aggregateID\n\t}\n\tc.trades = append(c.trades, tr)\n\tfirst := 0\n\tfor first < len(c.trades) && c.trades[first].Time.Before(cutoff) {\n\t\tfirst++\n\t}\n\tif first > 0 {\n\t\tc.trades = append([]aggressiveTrade(nil), c.trades[first:]...)\n\t}\n\tif c.lastTradeTime.IsZero() || ts.After(c.lastTradeTime) {\n\t\tc.lastTradeTime = ts\n\t}\n}''')

# Insert REST fallback engine before markDesynced.
replace('internal/binance/microstructure.go', '''func (c *MicrostructureClient) markDesynced(source string) {''', r'''func (c *MicrostructureClient) runRESTFallback() {
	defer c.wg.Done()
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	poll := func(now time.Time) {
		if c.bookNeedsREST(now) {
			if err := c.loadRESTBookFallback(); err != nil {
				util.Logger.Warn("Binance deep REST book fallback failed", zap.Error(err))
			}
		}
		if c.tradeNeedsREST(now) {
			if err := c.loadRESTAggTradesFallback(); err != nil {
				util.Logger.Warn("Binance aggTrades REST fallback failed", zap.Error(err))
			}
		}
	}
	poll(time.Now().UTC())
	for {
		select {
		case <-c.stopChan:
			return
		case now := <-ticker.C:
			poll(now.UTC())
		}
	}
}

func (c *MicrostructureClient) bookNeedsREST(now time.Time) bool {
	c.mu.RLock()
	defer c.mu.RUnlock()
	if c.lastBookTime.IsZero() {
		return true
	}
	return c.source != "BINANCE_DEEP_DIFF" || now.Sub(c.lastBookTime) > 1500*time.Millisecond
}

func (c *MicrostructureClient) tradeNeedsREST(now time.Time) bool {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.lastTradeTime.IsZero() || now.Sub(c.lastTradeTime) > 1200*time.Millisecond
}

func (c *MicrostructureClient) loadRESTBookFallback() error {
	var lastErr error
	for _, base := range deepRESTBases {
		resp, err := c.httpClient.Get(base + "/api/v3/depth?symbol=BTCUSDT&limit=1000")
		if err != nil {
			lastErr = err
			continue
		}
		if resp.StatusCode != http.StatusOK {
			lastErr = fmt.Errorf("deep REST snapshot http status %d", resp.StatusCode)
			_ = resp.Body.Close()
			continue
		}
		var snap deepSnapshotResponse
		err = json.NewDecoder(resp.Body).Decode(&snap)
		_ = resp.Body.Close()
		if err != nil {
			lastErr = err
			continue
		}
		bids := parseLevels(snap.Bids)
		asks := parseLevels(snap.Asks)
		if snap.LastUpdateID <= 0 || len(bids) == 0 || len(asks) == 0 {
			lastErr = fmt.Errorf("invalid deep REST snapshot")
			continue
		}
		now := time.Now().UTC()
		c.mu.Lock()
		c.bidLife = reconcileLife(c.bidLife, bids, now)
		c.askLife = reconcileLife(c.askLife, asks, now)
		c.bids = bids
		c.asks = asks
		c.lastUpdateID = snap.LastUpdateID
		c.lastBookTime = now
		c.synchronized = true
		c.source = "BINANCE_DEEP_REST1000"
		c.mu.Unlock()
		return nil
	}
	if lastErr == nil {
		lastErr = fmt.Errorf("no Binance deep REST endpoint available")
	}
	return lastErr
}

func reconcileLife(old map[float64]levelLife, book map[float64]float64, now time.Time) map[float64]levelLife {
	out := make(map[float64]levelLife, len(book))
	for p, size := range book {
		if st, ok := old[p]; ok {
			st.Size = size
			out[p] = st
		} else {
			out[p] = levelLife{FirstSeen: now, InitialSize: size, Size: size}
		}
	}
	return out
}

func (c *MicrostructureClient) loadRESTAggTradesFallback() error {
	var lastErr error
	for _, base := range deepRESTBases {
		resp, err := c.httpClient.Get(base + "/api/v3/aggTrades?symbol=BTCUSDT&limit=1000")
		if err != nil {
			lastErr = err
			continue
		}
		if resp.StatusCode != http.StatusOK {
			lastErr = fmt.Errorf("aggTrades REST http status %d", resp.StatusCode)
			_ = resp.Body.Close()
			continue
		}
		var rows []aggTradeRESTEvent
		err = json.NewDecoder(resp.Body).Decode(&rows)
		_ = resp.Body.Close()
		if err != nil {
			lastErr = err
			continue
		}
		for _, ev := range rows {
			price, errP := strconv.ParseFloat(ev.Price, 64)
			qty, errQ := strconv.ParseFloat(ev.Quantity, 64)
			if errP != nil || errQ != nil || price <= 0 || qty <= 0 || ev.TradeTime <= 0 {
				continue
			}
			c.recordTradeWithID(price, qty, ev.BuyerIsMaker, time.UnixMilli(ev.TradeTime).UTC(), ev.AggregateTradeID)
		}
		return nil
	}
	if lastErr == nil {
		lastErr = fmt.Errorf("no Binance aggTrades REST endpoint available")
	}
	return lastErr
}

func (c *MicrostructureClient) markDesynced(source string) {''')

replace('internal/binance/microstructure.go', '''\tout := DeepMicroSnapshot{Synchronized: c.synchronized, Source: c.source, LastUpdateID: c.lastUpdateID}''', '''\tout := DeepMicroSnapshot{Synchronized: c.synchronized, Source: c.source, LastUpdateID: c.lastUpdateID, PTBPrice: priceToBeat}''')

# Main WS manager gets the same market-data-only route.
replace('internal/binance/websocket.go', '''var binanceWSURLs = []string{\n\t"wss://stream.binance.com:9443/stream?streams=btcusdt@trade/btcusdt@depth20@100ms",\n\t"wss://stream.binance.com:443/stream?streams=btcusdt@trade/btcusdt@depth20@100ms",\n}''', '''var binanceWSURLs = []string{\n\t"wss://data-stream.binance.vision/stream?streams=btcusdt@trade/btcusdt@depth20@100ms",\n\t"wss://stream.binance.com:9443/stream?streams=btcusdt@trade/btcusdt@depth20@100ms",\n\t"wss://stream.binance.com:443/stream?streams=btcusdt@trade/btcusdt@depth20@100ms",\n}''')

# PTB corridor must be converted from Chainlink price frame to Binance frame.
replace('internal/engine/evaluator.go', '''\tif e.micro != nil {\n\t\tdeep = e.micro.Snapshot(binanceSpot, priceToBeat, nowTime)''', '''\tif e.micro != nil {\n\t\tbinancePTB := binanceEquivalentPTB(priceToBeat, currentPrice, binanceSpot)\n\t\tdeep = e.micro.Snapshot(binanceSpot, binancePTB, nowTime)''')

replace('internal/engine/evaluator.go', '''type Evaluator struct {\n\tbasis basisTracker\n\tmicro *binance.MicrostructureClient\n}''', '''type Evaluator struct {\n\tbasis basisTracker\n\tmicro *binance.MicrostructureClient\n}\n\nfunc binanceEquivalentPTB(chainlinkPTB, chainlinkCurrent, binanceSpot float64) float64 {\n\tif chainlinkPTB <= 0 || chainlinkCurrent <= 0 || binanceSpot <= 0 {\n\t\treturn chainlinkPTB\n\t}\n\treturn chainlinkPTB - (chainlinkCurrent - binanceSpot)\n}''')

# Tests.
p = Path('internal/binance/microstructure_test.go')
s = p.read_text()
s += r'''

func TestRecordTradeWithIDDeduplicatesRESTAndWS(t *testing.T) {
	c := NewMicrostructureClient()
	now := time.Now().UTC()
	c.recordTradeWithID(100, 1, false, now, 10)
	c.recordTradeWithID(100, 1, false, now, 10)
	c.mu.RLock()
	defer c.mu.RUnlock()
	if len(c.trades) != 1 {
		t.Fatalf("duplicate aggregate trade stored: %d", len(c.trades))
	}
}

func TestReconcileLifePreservesFirstSeen(t *testing.T) {
	now := time.Now().UTC()
	first := now.Add(-5 * time.Second)
	old := map[float64]levelLife{100: {FirstSeen: first, InitialSize: 10, Size: 10}}
	got := reconcileLife(old, map[float64]float64{100: 4, 99: 2}, now)
	if !got[100].FirstSeen.Equal(first) || got[100].InitialSize != 10 || got[100].Size != 4 {
		t.Fatalf("existing level lifecycle lost: %#v", got[100])
	}
	if !got[99].FirstSeen.Equal(now) || got[99].InitialSize != 2 {
		t.Fatalf("new level lifecycle wrong: %#v", got[99])
	}
}
'''
p.write_text(s)

p = Path('internal/engine/microstructure_test.go')
s = p.read_text()
s += r'''

func TestBinanceEquivalentPTBRemovesChainlinkBasis(t *testing.T) {
	got := binanceEquivalentPTB(63475, 63480, 63534)
	want := 63529.0
	if got != want {
		t.Fatalf("got %.2f want %.2f", got, want)
	}
}
'''
p.write_text(s)

p = Path('internal/storage/sqlite_test.go')
s = p.read_text()
s += r'''

func TestDatabaseSerializesSQLiteConnectionsAndSetsBusyTimeout(t *testing.T) {
	path := filepath.Join(t.TempDir(), "sqlite-locking.sqlite")
	db, err := NewDatabase(path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if got := db.db.Stats().MaxOpenConnections; got != 1 {
		t.Fatalf("MaxOpenConnections=%d want 1", got)
	}
	var busy int
	if err := db.db.QueryRow("PRAGMA busy_timeout").Scan(&busy); err != nil {
		t.Fatal(err)
	}
	if busy < 5000 {
		t.Fatalf("busy_timeout=%d want >=5000", busy)
	}
}
'''
p.write_text(s)
