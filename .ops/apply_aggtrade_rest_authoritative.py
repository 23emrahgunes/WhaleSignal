from pathlib import Path

p = Path('internal/binance/microstructure.go')
s = p.read_text()

old = '''const (\n\tdeepBookFreshAfter = 3 * time.Second\n\ttradeFlowRetention = 65 * time.Second\n)'''
new = '''const (\n\tdeepBookFreshAfter      = 3 * time.Second\n\ttradeFlowRetention      = 65 * time.Second\n\taggTradeRESTFreshAfter  = 3 * time.Second\n\taggTradeRESTPageSize    = 1000\n\taggTradeRESTMaxPages    = 8\n)'''
assert old in s
s = s.replace(old, new, 1)

old = '''\tLastTradeAgeMs     int64         `json:"lastTradeAgeMs"`\n\tTradeFlowAvailable bool          `json:"tradeFlowAvailable"`\n\tPTBPrice           float64       `json:"ptbPrice"`'''
new = '''\tLastTradeAgeMs     int64         `json:"lastTradeAgeMs"`\n\tTradeRESTAgeMs     int64         `json:"tradeRestAgeMs"`\n\tTradeFlowAvailable bool          `json:"tradeFlowAvailable"`\n\tTradeFlowSource    string        `json:"tradeFlowSource"`\n\tPTBPrice           float64       `json:"ptbPrice"`'''
assert old in s
s = s.replace(old, new, 1)

old = '''\tlastTradeTime   time.Time\n\tlastAggTradeID  int64\n\tseenAggTradeIDs map[int64]time.Time'''
new = '''\tlastTradeTime    time.Time\n\tlastAggRESTTime  time.Time\n\tlastAggTradeID   int64\n\tseenAggTradeIDs  map[int64]time.Time'''
assert old in s
s = s.replace(old, new, 1)

start = s.index('func (c *MicrostructureClient) loadRESTAggTradesFallback() error {')
end = s.index('\nfunc (c *MicrostructureClient) markDesynced', start)
replacement = r'''func (c *MicrostructureClient) loadRESTAggTradesFallback() error {
	windowEnd := time.Now().UTC()
	var lastErr error
	for _, base := range deepRESTBases {
		rows, err := c.fetchRESTAggTradeWindow(base, windowEnd)
		if err != nil {
			lastErr = err
			continue
		}
		if err := c.reconcileRESTAggTradeWindow(rows, windowEnd); err != nil {
			lastErr = err
			continue
		}
		return nil
	}
	if lastErr == nil {
		lastErr = fmt.Errorf("no Binance aggTrades REST endpoint available")
	}
	return lastErr
}

// fetchRESTAggTradeWindow retrieves the complete recent aggregate-trade window.
// Binance returns the oldest rows first when startTime is supplied, so a busy
// market can require pagination beyond the first 1000 rows. Subsequent pages
// continue from the last aggregate ID and are clipped to the original window.
func (c *MicrostructureClient) fetchRESTAggTradeWindow(base string, windowEnd time.Time) ([]aggTradeRESTEvent, error) {
	windowEnd = windowEnd.UTC()
	startMs := windowEnd.Add(-tradeFlowRetention).UnixMilli()
	endMs := windowEnd.UnixMilli()
	nextURL := fmt.Sprintf("%s/api/v3/aggTrades?symbol=BTCUSDT&startTime=%d&endTime=%d&limit=%d", base, startMs, endMs, aggTradeRESTPageSize)
	all := make([]aggTradeRESTEvent, 0, 2048)
	seen := make(map[int64]struct{}, 2048)
	complete := false

	for page := 0; page < aggTradeRESTMaxPages; page++ {
		rows, err := c.fetchAggTradePage(nextURL)
		if err != nil {
			return nil, err
		}
		if len(rows) == 0 {
			complete = true
			break
		}

		lastID := int64(0)
		reachedWindowEnd := false
		for _, ev := range rows {
			if ev.AggregateTradeID > lastID {
				lastID = ev.AggregateTradeID
			}
			if ev.TradeTime < startMs {
				continue
			}
			if ev.TradeTime > endMs {
				reachedWindowEnd = true
				continue
			}
			if ev.AggregateTradeID <= 0 {
				continue
			}
			if _, exists := seen[ev.AggregateTradeID]; exists {
				continue
			}
			seen[ev.AggregateTradeID] = struct{}{}
			all = append(all, ev)
		}

		last := rows[len(rows)-1]
		if len(rows) < aggTradeRESTPageSize || last.TradeTime >= endMs || reachedWindowEnd {
			complete = true
			break
		}
		if lastID <= 0 {
			return nil, fmt.Errorf("aggTrades REST pagination missing aggregate ID")
		}
		nextURL = fmt.Sprintf("%s/api/v3/aggTrades?symbol=BTCUSDT&fromId=%d&limit=%d", base, lastID+1, aggTradeRESTPageSize)
	}
	if !complete {
		return nil, fmt.Errorf("aggTrades REST window exceeds pagination cap of %d rows", aggTradeRESTPageSize*aggTradeRESTMaxPages)
	}

	sort.Slice(all, func(i, j int) bool {
		if all[i].TradeTime == all[j].TradeTime {
			return all[i].AggregateTradeID < all[j].AggregateTradeID
		}
		return all[i].TradeTime < all[j].TradeTime
	})
	return all, nil
}

func (c *MicrostructureClient) fetchAggTradePage(url string) ([]aggTradeRESTEvent, error) {
	resp, err := c.httpClient.Get(url)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("aggTrades REST http status %d", resp.StatusCode)
	}
	var rows []aggTradeRESTEvent
	if err := json.NewDecoder(resp.Body).Decode(&rows); err != nil {
		return nil, err
	}
	return rows, nil
}

// reconcileRESTAggTradeWindow makes REST authoritative for the overlapping
// retention window. WebSocket events newer than windowEnd are kept provisionally;
// any overlapping WS classification is replaced by Binance's REST record. This
// prevents a bad/partial WS side from being permanently frozen by ID dedupe.
func (c *MicrostructureClient) reconcileRESTAggTradeWindow(rows []aggTradeRESTEvent, windowEnd time.Time) error {
	windowEnd = windowEnd.UTC()
	cutoff := windowEnd.Add(-tradeFlowRetention)
	authoritative := make([]aggressiveTrade, 0, len(rows))
	authoritativeIDs := make(map[int64]time.Time, len(rows))

	for _, ev := range rows {
		if ev.AggregateTradeID <= 0 || ev.TradeTime <= 0 {
			continue
		}
		price, errP := strconv.ParseFloat(ev.Price, 64)
		qty, errQ := strconv.ParseFloat(ev.Quantity, 64)
		if errP != nil || errQ != nil || price <= 0 || qty <= 0 {
			continue
		}
		ts := time.UnixMilli(ev.TradeTime).UTC()
		if ts.Before(cutoff) || ts.After(windowEnd) {
			continue
		}
		tr := aggressiveTrade{Time: ts}
		if ev.BuyerIsMaker {
			tr.SellUSD = price * qty
		} else {
			tr.BuyUSD = price * qty
		}
		authoritative = append(authoritative, tr)
		authoritativeIDs[ev.AggregateTradeID] = ts
	}

	c.mu.Lock()
	defer c.mu.Unlock()

	// Keep only WS events that happened after the REST snapshot boundary. They
	// will themselves become REST-authoritative on the next poll.
	future := make([]aggressiveTrade, 0, 64)
	for _, tr := range c.trades {
		if tr.Time.After(windowEnd) {
			future = append(future, tr)
		}
	}
	c.trades = append(authoritative, future...)

	for id, seenAt := range c.seenAggTradeIDs {
		if seenAt.After(windowEnd) {
			authoritativeIDs[id] = seenAt
		}
	}
	c.seenAggTradeIDs = authoritativeIDs
	c.lastAggTradeID = 0
	for id := range authoritativeIDs {
		if id > c.lastAggTradeID {
			c.lastAggTradeID = id
		}
	}

	c.lastTradeTime = time.Time{}
	for _, tr := range c.trades {
		if c.lastTradeTime.IsZero() || tr.Time.After(c.lastTradeTime) {
			c.lastTradeTime = tr.Time
		}
	}
	c.lastAggRESTTime = time.Now().UTC()
	return nil
}
'''
s = s[:start] + replacement + s[end:]

old = '''\tout := DeepMicroSnapshot{Synchronized: c.synchronized, Source: c.source, LastUpdateID: c.lastUpdateID, PTBPrice: priceToBeat}\n\tif c.lastBookTime.IsZero() {\n\t\tout.AgeMs = -1\n\t\treturn out\n\t}'''
new = '''\tout := DeepMicroSnapshot{\n\t\tSynchronized:    c.synchronized,\n\t\tSource:          c.source,\n\t\tLastUpdateID:    c.lastUpdateID,\n\t\tPTBPrice:         priceToBeat,\n\t\tLastTradeAgeMs:  -1,\n\t\tTradeRESTAgeMs:  -1,\n\t\tTradeFlowSource: "UNVERIFIED",\n\t}\n\tif c.lastBookTime.IsZero() {\n\t\tout.AgeMs = -1\n\t\treturn out\n\t}'''
assert old in s
s = s.replace(old, new, 1)

old = '''\tif !c.lastTradeTime.IsZero() {\n\t\tout.LastTradeAgeMs = now.Sub(c.lastTradeTime).Milliseconds()\n\t\tout.TradeFlowAvailable = out.LastTradeAgeMs >= 0 && out.LastTradeAgeMs <= 3000\n\t} else {\n\t\tout.LastTradeAgeMs = -1\n\t}\n\tout.Ready = c.synchronized && age >= 0 && age <= deepBookFreshAfter && out.BidRangeUSD >= 75 && out.AskRangeUSD >= 75'''
new = '''\tif !c.lastTradeTime.IsZero() {\n\t\tout.LastTradeAgeMs = now.Sub(c.lastTradeTime).Milliseconds()\n\t}\n\tif !c.lastAggRESTTime.IsZero() {\n\t\tout.TradeRESTAgeMs = now.Sub(c.lastAggRESTTime).Milliseconds()\n\t\tout.TradeFlowSource = "BINANCE_AGGTRADES_REST_AUTH"\n\t}\n\tout.TradeFlowAvailable = out.LastTradeAgeMs >= 0 && out.LastTradeAgeMs <= 3000 &&\n\t\tout.TradeRESTAgeMs >= 0 && out.TradeRESTAgeMs <= aggTradeRESTFreshAfter.Milliseconds()\n\tout.Ready = c.synchronized && age >= 0 && age <= deepBookFreshAfter && out.BidRangeUSD >= 75 && out.AskRangeUSD >= 75'''
assert old in s
s = s.replace(old, new, 1)

p.write_text(s)

p = Path('internal/binance/microstructure_test.go')
s = p.read_text()
append = r'''

func TestRESTAuthoritativeReconcileCorrectsWrongWSClassification(t *testing.T) {
	c := NewMicrostructureClient()
	now := time.Now().UTC().Truncate(time.Millisecond)

	// Simulate a provisional WS record with the wrong aggressor side.
	c.recordTradeWithID(100, 2, true, now.Add(-2*time.Second), 42)
	rows := []aggTradeRESTEvent{{
		AggregateTradeID: 42,
		Price:            "100",
		Quantity:         "2",
		TradeTime:        now.Add(-2 * time.Second).UnixMilli(),
		BuyerIsMaker:     false, // authoritative REST says aggressive BUY
	}}
	if err := c.reconcileRESTAggTradeWindow(rows, now); err != nil {
		t.Fatal(err)
	}

	c.mu.RLock()
	buy, sell := tradeWindow(c.trades, now.Add(-5*time.Second))
	restFresh := !c.lastAggRESTTime.IsZero()
	c.mu.RUnlock()
	if buy != 200 || sell != 0 {
		t.Fatalf("REST did not correct WS side: buy %.2f sell %.2f", buy, sell)
	}
	if !restFresh {
		t.Fatal("expected authoritative REST reconcile timestamp")
	}
}

func TestRESTAuthoritativeReconcilePreservesNewerWSRecord(t *testing.T) {
	c := NewMicrostructureClient()
	now := time.Now().UTC().Truncate(time.Millisecond)
	c.recordTradeWithID(100, 1, true, now.Add(time.Second), 101)
	rows := []aggTradeRESTEvent{{
		AggregateTradeID: 100,
		Price:            "100",
		Quantity:         "2",
		TradeTime:        now.Add(-time.Second).UnixMilli(),
		BuyerIsMaker:     false,
	}}
	if err := c.reconcileRESTAggTradeWindow(rows, now); err != nil {
		t.Fatal(err)
	}
	c.mu.RLock()
	buy, sell := tradeWindow(c.trades, now.Add(-5*time.Second))
	c.mu.RUnlock()
	if buy != 200 || sell != 100 {
		t.Fatalf("newer provisional WS record lost: buy %.2f sell %.2f", buy, sell)
	}
}

func TestTradeFlowRequiresFreshAuthoritativeREST(t *testing.T) {
	c := NewMicrostructureClient()
	now := time.Now().UTC()
	c.mu.Lock()
	c.synchronized = true
	c.source = "BINANCE_DEEP_REST1000"
	c.lastBookTime = now
	c.bids = map[float64]float64{100: 1, 20: 1}
	c.asks = map[float64]float64{101: 1, 181: 1}
	c.lastTradeTime = now
	c.trades = []aggressiveTrade{{Time: now, BuyUSD: 100}}
	c.mu.Unlock()

	before := c.Snapshot(100.5, 110, now)
	if before.TradeFlowAvailable {
		t.Fatal("unverified WS-only trade flow must fail closed")
	}

	c.mu.Lock()
	c.lastAggRESTTime = now
	c.mu.Unlock()
	after := c.Snapshot(100.5, 110, now)
	if !after.TradeFlowAvailable {
		t.Fatalf("fresh REST-authoritative flow should be available: %#v", after)
	}
	if after.TradeFlowSource != "BINANCE_AGGTRADES_REST_AUTH" {
		t.Fatalf("unexpected trade-flow source %q", after.TradeFlowSource)
	}
}
'''
if 'TestRESTAuthoritativeReconcileCorrectsWrongWSClassification' not in s:
    s += append
p.write_text(s)
