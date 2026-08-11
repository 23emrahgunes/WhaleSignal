package binance

import (
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"sort"
	"strconv"
	"sync"
	"time"

	"github.com/gorilla/websocket"
	"go.uber.org/zap"
	"pm-edge/internal/util"
)

const (
	deepBookFreshAfter = 3 * time.Second
	tradeFlowRetention = 65 * time.Second
)

var deepWSURLs = []string{
	"wss://data-stream.binance.vision/stream?streams=btcusdt@depth@100ms/btcusdt@aggTrade",
	"wss://stream.binance.com:9443/stream?streams=btcusdt@depth@100ms/btcusdt@aggTrade",
	"wss://stream.binance.com:443/stream?streams=btcusdt@depth@100ms/btcusdt@aggTrade",
}

var deepRESTBases = []string{
	"https://api.binance.com",
	"https://api-gcp.binance.com",
	"https://data-api.binance.vision",
}

type DeepBand struct {
	DistanceUSD float64 `json:"distanceUsd"`
	BidUSD      float64 `json:"bidUsd"`
	AskUSD      float64 `json:"askUsd"`
	Imbalance   float64 `json:"imbalance"`
}

type TradeWindow struct {
	Seconds   int     `json:"seconds"`
	BuyUSD    float64 `json:"buyUsd"`
	SellUSD   float64 `json:"sellUsd"`
	Imbalance float64 `json:"imbalance"`
}

type DeepMicroSnapshot struct {
	Ready              bool          `json:"ready"`
	Synchronized       bool          `json:"synchronized"`
	Source             string        `json:"source"`
	AgeMs              int64         `json:"ageMs"`
	BidLevels          int           `json:"bidLevels"`
	AskLevels          int           `json:"askLevels"`
	BestBid            float64       `json:"bestBid"`
	BestAsk            float64       `json:"bestAsk"`
	MidPrice           float64       `json:"midPrice"`
	BidRangeUSD        float64       `json:"bidRangeUsd"`
	AskRangeUSD        float64       `json:"askRangeUsd"`
	Bands              []DeepBand    `json:"bands"`
	Trades             []TradeWindow `json:"trades"`
	TradeAcceleration  float64       `json:"tradeAcceleration"`
	BidWallScore       float64       `json:"bidWallScore"`
	AskWallScore       float64       `json:"askWallScore"`
	AskDepletionScore  float64       `json:"askDepletionScore"`
	BidDepletionScore  float64       `json:"bidDepletionScore"`
	PTBPathBidUSD      float64       `json:"ptbPathBidUsd"`
	PTBPathAskUSD      float64       `json:"ptbPathAskUsd"`
	PTBBeyondUSD       float64       `json:"ptbBeyondUsd"`
	PTBBarrierScore    float64       `json:"ptbBarrierScore"`
	LastUpdateID       int64         `json:"lastUpdateId"`
	LastTradeAgeMs     int64         `json:"lastTradeAgeMs"`
	TradeFlowAvailable bool          `json:"tradeFlowAvailable"`
	PTBPrice           float64       `json:"ptbPrice"`
	PTBDistanceUSD     float64       `json:"ptbDistanceUsd"`
	PTBCorridorCovered bool          `json:"ptbCorridorCovered"`
}

type aggressiveTrade struct {
	Time    time.Time
	BuyUSD  float64
	SellUSD float64
}

type levelLife struct {
	FirstSeen   time.Time
	InitialSize float64
	Size        float64
}

type MicrostructureClient struct {
	mu sync.RWMutex

	httpClient *http.Client
	bids       map[float64]float64
	asks       map[float64]float64
	bidLife    map[float64]levelLife
	askLife    map[float64]levelLife
	trades     []aggressiveTrade

	synchronized    bool
	lastUpdateID    int64
	lastBookTime    time.Time
	lastTradeTime   time.Time
	lastAggTradeID  int64
	seenAggTradeIDs map[int64]time.Time
	source          string

	stopChan chan struct{}
	stopOnce sync.Once
	wg       sync.WaitGroup
	connMu   sync.Mutex
	conn     *websocket.Conn
}

func NewMicrostructureClient() *MicrostructureClient {
	return &MicrostructureClient{
		httpClient:      &http.Client{Timeout: 8 * time.Second},
		bids:            make(map[float64]float64),
		asks:            make(map[float64]float64),
		bidLife:         make(map[float64]levelLife),
		askLife:         make(map[float64]levelLife),
		trades:          make([]aggressiveTrade, 0, 4096),
		seenAggTradeIDs: make(map[int64]time.Time, 8192),
		source:          "UNINITIALIZED",
		stopChan:        make(chan struct{}),
	}
}

func (c *MicrostructureClient) Start() {
	c.wg.Add(2)
	go c.run()
	go c.runRESTFallback()
}

func (c *MicrostructureClient) Stop() {
	c.stopOnce.Do(func() { close(c.stopChan) })
	c.connMu.Lock()
	if c.conn != nil {
		_ = c.conn.Close()
	}
	c.connMu.Unlock()
	c.wg.Wait()
}

func (c *MicrostructureClient) run() {
	defer c.wg.Done()
	backoff := time.Second
	urlIndex := 0
	for {
		select {
		case <-c.stopChan:
			return
		default:
		}

		url := deepWSURLs[urlIndex%len(deepWSURLs)]
		urlIndex++
		conn, _, err := websocket.DefaultDialer.Dial(url, nil)
		if err != nil {
			c.markDesynced("DEEP_WS_CONNECT_FAILED")
			util.Logger.Warn("Binance deep-book websocket connection failed", zap.Error(err), zap.Duration("backoff", backoff))
			if !c.sleep(backoff) {
				return
			}
			backoff = nextMicroBackoff(backoff)
			continue
		}
		c.connMu.Lock()
		c.conn = conn
		c.connMu.Unlock()

		if err := c.loadSnapshot(); err != nil {
			_ = conn.Close()
			c.markDesynced("DEEP_SNAPSHOT_FAILED")
			util.Logger.Warn("Binance deep-book snapshot failed", zap.Error(err))
			if !c.sleep(backoff) {
				return
			}
			backoff = nextMicroBackoff(backoff)
			continue
		}
		backoff = time.Second
		util.Logger.Info("Binance deep microstructure feed initialized", zap.String("url", url), zap.Int64("lastUpdateId", c.LastUpdateID()))

		err = c.readLoop(conn)
		_ = conn.Close()
		c.connMu.Lock()
		if c.conn == conn {
			c.conn = nil
		}
		c.connMu.Unlock()
		c.markDesynced("DEEP_WS_DISCONNECTED")
		select {
		case <-c.stopChan:
			return
		default:
			util.Logger.Warn("Binance deep microstructure feed disconnected", zap.Error(err))
		}
		if !c.sleep(time.Second) {
			return
		}
	}
}

func nextMicroBackoff(d time.Duration) time.Duration {
	d = time.Duration(float64(d) * 1.5)
	if d > 30*time.Second {
		return 30 * time.Second
	}
	return d
}

func (c *MicrostructureClient) sleep(d time.Duration) bool {
	select {
	case <-c.stopChan:
		return false
	case <-time.After(d):
		return true
	}
}

type deepSnapshotResponse struct {
	LastUpdateID int64      `json:"lastUpdateId"`
	Bids         [][]string `json:"bids"`
	Asks         [][]string `json:"asks"`
}

func (c *MicrostructureClient) loadSnapshot() error {
	resp, err := c.httpClient.Get("https://api.binance.com/api/v3/depth?symbol=BTCUSDT&limit=5000")
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("deep snapshot http status %d", resp.StatusCode)
	}
	var snap deepSnapshotResponse
	if err := json.NewDecoder(resp.Body).Decode(&snap); err != nil {
		return err
	}
	if snap.LastUpdateID <= 0 || len(snap.Bids) == 0 || len(snap.Asks) == 0 {
		return fmt.Errorf("invalid deep snapshot")
	}
	now := time.Now().UTC()
	bids := parseLevels(snap.Bids)
	asks := parseLevels(snap.Asks)
	if len(bids) == 0 || len(asks) == 0 {
		return fmt.Errorf("empty parsed deep snapshot")
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.bids = bids
	c.asks = asks
	c.bidLife = make(map[float64]levelLife, len(bids))
	c.askLife = make(map[float64]levelLife, len(asks))
	for p, s := range bids {
		c.bidLife[p] = levelLife{FirstSeen: now, InitialSize: s, Size: s}
	}
	for p, s := range asks {
		c.askLife[p] = levelLife{FirstSeen: now, InitialSize: s, Size: s}
	}
	c.lastUpdateID = snap.LastUpdateID
	c.lastBookTime = now
	c.synchronized = false
	c.source = "BINANCE_DEEP_SNAPSHOT"
	return nil
}

type deepDiffEvent struct {
	EventTime int64      `json:"E"`
	FirstID   int64      `json:"U"`
	FinalID   int64      `json:"u"`
	Bids      [][]string `json:"b"`
	Asks      [][]string `json:"a"`
}

type aggTradeEvent struct {
	EventTime        int64  `json:"E"`
	AggregateTradeID int64  `json:"a"`
	Price            string `json:"p"`
	Quantity         string `json:"q"`
	TradeTime        int64  `json:"T"`
	BuyerIsMaker     bool   `json:"m"`
}

type aggTradeRESTEvent struct {
	AggregateTradeID int64  `json:"a"`
	Price            string `json:"p"`
	Quantity         string `json:"q"`
	TradeTime        int64  `json:"T"`
	BuyerIsMaker     bool   `json:"m"`
}

func (c *MicrostructureClient) readLoop(conn *websocket.Conn) error {
	for {
		_ = conn.SetReadDeadline(time.Now().Add(8 * time.Second))
		_, raw, err := conn.ReadMessage()
		if err != nil {
			return err
		}
		var payload CombinedStreamPayload
		if err := json.Unmarshal(raw, &payload); err != nil {
			continue
		}
		switch payload.Stream {
		case "btcusdt@depth@100ms":
			var ev deepDiffEvent
			if err := json.Unmarshal(payload.Data, &ev); err != nil {
				continue
			}
			if !c.applyDiff(ev) {
				return fmt.Errorf("deep book sequence gap detected")
			}
		case "btcusdt@aggTrade":
			var ev aggTradeEvent
			if err := json.Unmarshal(payload.Data, &ev); err != nil {
				continue
			}
			price, errP := strconv.ParseFloat(ev.Price, 64)
			qty, errQ := strconv.ParseFloat(ev.Quantity, 64)
			if errP == nil && errQ == nil && price > 0 && qty > 0 {
				ts := ev.TradeTime
				if ts <= 0 {
					ts = ev.EventTime
				}
				c.recordTradeWithID(price, qty, ev.BuyerIsMaker, time.UnixMilli(ts).UTC(), ev.AggregateTradeID)
			}
		}
	}
}

func (c *MicrostructureClient) applyDiff(ev deepDiffEvent) bool {
	if ev.FinalID <= 0 {
		return true
	}
	now := time.Now().UTC()
	c.mu.Lock()
	defer c.mu.Unlock()
	if ev.FinalID <= c.lastUpdateID {
		return true
	}
	expected := c.lastUpdateID + 1
	if ev.FirstID > expected || ev.FinalID < expected {
		return false
	}
	c.applySide(c.bids, c.bidLife, ev.Bids, now)
	c.applySide(c.asks, c.askLife, ev.Asks, now)
	c.lastUpdateID = ev.FinalID
	c.lastBookTime = now
	c.synchronized = true
	c.source = "BINANCE_DEEP_DIFF"
	return true
}

func (c *MicrostructureClient) applySide(book map[float64]float64, life map[float64]levelLife, rows [][]string, now time.Time) {
	for _, row := range rows {
		if len(row) < 2 {
			continue
		}
		p, errP := strconv.ParseFloat(row[0], 64)
		s, errS := strconv.ParseFloat(row[1], 64)
		if errP != nil || errS != nil || p <= 0 || s < 0 {
			continue
		}
		if s == 0 {
			delete(book, p)
			delete(life, p)
			continue
		}
		book[p] = s
		st, ok := life[p]
		if !ok {
			life[p] = levelLife{FirstSeen: now, InitialSize: s, Size: s}
		} else {
			st.Size = s
			life[p] = st
		}
	}
}

func (c *MicrostructureClient) recordTrade(price, qty float64, buyerIsMaker bool, ts time.Time) {
	c.recordTradeWithID(price, qty, buyerIsMaker, ts, 0)
}

func (c *MicrostructureClient) recordTradeWithID(price, qty float64, buyerIsMaker bool, ts time.Time, aggregateID int64) {
	now := time.Now().UTC()
	cutoff := now.Add(-tradeFlowRetention)
	if ts.Before(cutoff) {
		return
	}
	notional := price * qty
	tr := aggressiveTrade{Time: ts}
	if buyerIsMaker {
		tr.SellUSD = notional
	} else {
		tr.BuyUSD = notional
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if aggregateID > 0 {
		if _, seen := c.seenAggTradeIDs[aggregateID]; seen {
			return
		}
		c.seenAggTradeIDs[aggregateID] = ts
		if aggregateID > c.lastAggTradeID {
			c.lastAggTradeID = aggregateID
		}
	}
	c.trades = append(c.trades, tr)
	kept := c.trades[:0]
	for _, row := range c.trades {
		if !row.Time.Before(cutoff) {
			kept = append(kept, row)
		}
	}
	c.trades = kept
	for id, seenAt := range c.seenAggTradeIDs {
		if seenAt.Before(cutoff) {
			delete(c.seenAggTradeIDs, id)
		}
	}
	if c.lastTradeTime.IsZero() || ts.After(c.lastTradeTime) {
		c.lastTradeTime = ts
	}
}

func (c *MicrostructureClient) runRESTFallback() {
	defer c.wg.Done()
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	poll := func(now time.Time) {
		if c.bookNeedsREST(now) {
			if err := c.loadRESTBookFallback(); err != nil {
				util.Logger.Warn("Binance deep REST book fallback failed", zap.Error(err))
			}
		}
		// Always backfill aggregate trades from REST. WebSocket and REST can arrive
		// out of order; the ID seen-set deduplicates them without dropping valid
		// BUY trades that have a lower ID than a newer WS event.
		if err := c.loadRESTAggTradesFallback(); err != nil {
			util.Logger.Warn("Binance aggTrades REST backfill failed", zap.Error(err))
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

func (c *MicrostructureClient) markDesynced(source string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.synchronized = false
	c.source = source
}

func (c *MicrostructureClient) LastUpdateID() int64 {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.lastUpdateID
}

func (c *MicrostructureClient) Snapshot(currentPrice, priceToBeat float64, now time.Time) DeepMicroSnapshot {
	now = now.UTC()
	c.mu.RLock()
	defer c.mu.RUnlock()
	out := DeepMicroSnapshot{Synchronized: c.synchronized, Source: c.source, LastUpdateID: c.lastUpdateID, PTBPrice: priceToBeat}
	if c.lastBookTime.IsZero() {
		out.AgeMs = -1
		return out
	}
	age := now.Sub(c.lastBookTime)
	out.AgeMs = age.Milliseconds()
	out.BidLevels = len(c.bids)
	out.AskLevels = len(c.asks)
	out.BestBid, out.BestAsk = bestPrices(c.bids, c.asks)
	if out.BestBid <= 0 || out.BestAsk <= 0 {
		return out
	}
	out.MidPrice = 0.5 * (out.BestBid + out.BestAsk)
	if currentPrice <= 0 {
		currentPrice = out.MidPrice
	}
	out.BidRangeUSD, out.AskRangeUSD = fullRanges(c.bids, c.asks, out.BestBid, out.BestAsk)
	if priceToBeat > 0 {
		out.PTBDistanceUSD = math.Abs(priceToBeat - currentPrice)
		out.PTBCorridorCovered = out.PTBDistanceUSD == 0 || (out.BidRangeUSD >= out.PTBDistanceUSD && out.AskRangeUSD >= out.PTBDistanceUSD)
	}
	for _, d := range []float64{10, 25, 50, 75} {
		bid, ask := bandNotional(c.bids, c.asks, currentPrice, d)
		out.Bands = append(out.Bands, DeepBand{DistanceUSD: d, BidUSD: bid, AskUSD: ask, Imbalance: normalizedImbalance(bid, ask)})
	}
	for _, seconds := range []int{5, 15, 30, 60} {
		buy, sell := tradeWindow(c.trades, now.Add(-time.Duration(seconds)*time.Second))
		out.Trades = append(out.Trades, TradeWindow{Seconds: seconds, BuyUSD: buy, SellUSD: sell, Imbalance: normalizedImbalance(buy, sell)})
	}
	if len(out.Trades) >= 3 {
		out.TradeAcceleration = clamp(out.Trades[0].Imbalance-out.Trades[2].Imbalance, -1, 1)
	}
	out.BidWallScore, out.BidDepletionScore = wallMetrics(c.bids, c.bidLife, currentPrice, 75, now, true)
	out.AskWallScore, out.AskDepletionScore = wallMetrics(c.asks, c.askLife, currentPrice, 75, now, false)
	out.PTBPathBidUSD, out.PTBPathAskUSD, out.PTBBeyondUSD, out.PTBBarrierScore = ptbBarrier(c.bids, c.asks, currentPrice, priceToBeat)
	if !c.lastTradeTime.IsZero() {
		out.LastTradeAgeMs = now.Sub(c.lastTradeTime).Milliseconds()
		out.TradeFlowAvailable = out.LastTradeAgeMs >= 0 && out.LastTradeAgeMs <= 3000
	} else {
		out.LastTradeAgeMs = -1
	}
	out.Ready = c.synchronized && age >= 0 && age <= deepBookFreshAfter && out.BidRangeUSD >= 75 && out.AskRangeUSD >= 75
	return out
}

func bestPrices(bids, asks map[float64]float64) (float64, float64) {
	bestBid := 0.0
	bestAsk := math.MaxFloat64
	for p := range bids {
		if p > bestBid {
			bestBid = p
		}
	}
	for p := range asks {
		if p < bestAsk {
			bestAsk = p
		}
	}
	if bestAsk == math.MaxFloat64 {
		bestAsk = 0
	}
	return bestBid, bestAsk
}

func fullRanges(bids, asks map[float64]float64, bestBid, bestAsk float64) (float64, float64) {
	worstBid := bestBid
	worstAsk := bestAsk
	for p := range bids {
		if p < worstBid {
			worstBid = p
		}
	}
	for p := range asks {
		if p > worstAsk {
			worstAsk = p
		}
	}
	return math.Max(0, bestBid-worstBid), math.Max(0, worstAsk-bestAsk)
}

func bandNotional(bids, asks map[float64]float64, mid, distance float64) (float64, float64) {
	bid := 0.0
	ask := 0.0
	for p, s := range bids {
		if p <= mid && p >= mid-distance {
			bid += p * s
		}
	}
	for p, s := range asks {
		if p >= mid && p <= mid+distance {
			ask += p * s
		}
	}
	return bid, ask
}

func normalizedImbalance(a, b float64) float64 {
	if a+b <= 0 {
		return 0
	}
	return clamp((a-b)/(a+b), -1, 1)
}

func tradeWindow(rows []aggressiveTrade, cutoff time.Time) (float64, float64) {
	buy := 0.0
	sell := 0.0
	for i := len(rows) - 1; i >= 0; i-- {
		if rows[i].Time.Before(cutoff) {
			continue
		}
		buy += rows[i].BuyUSD
		sell += rows[i].SellUSD
	}
	return buy, sell
}

func wallMetrics(book map[float64]float64, life map[float64]levelLife, mid, distance float64, now time.Time, isBid bool) (float64, float64) {
	sizes := make([]float64, 0)
	for p, s := range book {
		if math.Abs(p-mid) <= distance && s > 0 {
			sizes = append(sizes, s)
		}
	}
	if len(sizes) < 3 {
		return 0, 0
	}
	sort.Float64s(sizes)
	median := sizes[len(sizes)/2]
	if median <= 0 {
		return 0, 0
	}
	wallScore := 0.0
	depletion := 0.0
	for p, st := range life {
		if math.Abs(p-mid) > distance || st.Size <= 0 {
			continue
		}
		age := now.Sub(st.FirstSeen).Seconds()
		persistence := clamp(age/5.0, 0, 1)
		ratio := st.Size / median
		distanceWeight := math.Exp(-math.Abs(p-mid) / math.Max(distance, 1))
		strength := clamp((ratio-3.0)/12.0, 0, 1) * persistence * distanceWeight
		if strength > wallScore {
			wallScore = strength
		}
		if st.InitialSize > 0 && age >= 1 {
			d := clamp((st.InitialSize-st.Size)/st.InitialSize, 0, 1) * distanceWeight
			if d > depletion {
				depletion = d
			}
		}
	}
	_ = isBid
	return wallScore, depletion
}

func ptbBarrier(bids, asks map[float64]float64, current, ptb float64) (float64, float64, float64, float64) {
	if current <= 0 || ptb <= 0 || current == ptb {
		return 0, 0, 0, 0
	}
	d := math.Abs(ptb - current)
	if d < 1 {
		d = 1
	}
	pathBid := 0.0
	pathAsk := 0.0
	beyond := 0.0
	if current < ptb {
		for p, s := range asks {
			if p >= current && p <= ptb {
				pathAsk += p * s
			} else if p > ptb && p <= ptb+25 {
				beyond += p * s
			}
		}
		for p, s := range bids {
			if p <= current && p >= current-d {
				pathBid += p * s
			}
		}
		return pathBid, pathAsk, beyond, normalizedImbalance(pathBid, pathAsk+0.35*beyond)
	}
	for p, s := range bids {
		if p >= ptb && p <= current {
			pathBid += p * s
		} else if p < ptb && p >= ptb-25 {
			beyond += p * s
		}
	}
	for p, s := range asks {
		if p >= current && p <= current+d {
			pathAsk += p * s
		}
	}
	return pathBid, pathAsk, beyond, normalizedImbalance(pathBid+0.35*beyond, pathAsk)
}

func clamp(v, lo, hi float64) float64 {
	if v < lo {
		return lo
	}
	if v > hi {
		return hi
	}
	return v
}
