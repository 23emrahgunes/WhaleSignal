package binance

import (
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"sort"
	"strconv"
	"sync"
	"time"

	"pm-edge/internal/util"
	"go.uber.org/zap"
)

type Candle struct {
	StartTime time.Time `json:"startTime"`
	Open      float64   `json:"open"`
	High      float64   `json:"high"`
	Low       float64   `json:"low"`
	Close     float64   `json:"close"`
	Volume    float64   `json:"volume"`
}

type OrderBookLevel struct {
	Price float64
	Size  float64
}

type OrderBook struct {
	Bids []OrderBookLevel
	Asks []OrderBookLevel
}

type DepthSnapshot struct {
	Timestamp time.Time
	Bids      map[float64]float64
	Asks      map[float64]float64
}

type SeenOrder struct {
	FirstSeen time.Time
	LastSeen  time.Time
	Size      float64
}

type Client struct {
	httpClient *http.Client
	mu         sync.RWMutex

	// Candlesticks (In-memory)
	Candles1m []Candle
	Candles5m []Candle

	// Current Price and returns
	CurrentPrice float64
	LogReturns   []float64 // last 60 seconds returns

	// Order book mapping for Bid/Ask metrics
	LastBids map[float64]float64
	LastAsks map[float64]float64

	// Spoofing analysis
	Snapshots []DepthSnapshot
	SeenBids  map[float64]*SeenOrder
	SeenAsks  map[float64]*SeenOrder

	// Reconnection states, flags, data source tracking
	IsWsConnected bool
	WSFallback    bool
	DataSource    string // "BINANCE_WS", "BINANCE_REST", "MOCK"

	LastPriceUpdateTime time.Time
}

func NewClient() *Client {
	return &Client{
		httpClient: &http.Client{Timeout: 5 * time.Second},
		LogReturns: make([]float64, 0, 60),
		LastBids:   make(map[float64]float64),
		LastAsks:   make(map[float64]float64),
		SeenBids:   make(map[float64]*SeenOrder),
		SeenAsks:   make(map[float64]*SeenOrder),
		Snapshots:  make([]DepthSnapshot, 0, 10),
		DataSource: "MOCK", // default initial source before real ticks arrive
	}
}

// WarmupCandles loads last 200 bars for 1m and 5m klines from Binance REST.
func (c *Client) WarmupCandles() error {
	c.mu.Lock()
	defer c.mu.Unlock()

	candles1, err := c.fetchKlines("1m", 200)
	if err != nil {
		return fmt.Errorf("1m klines warmup failed: %w", err)
	}
	c.Candles1m = candles1

	candles5, err := c.fetchKlines("5m", 200)
	if err != nil {
		return fmt.Errorf("5m klines warmup failed: %w", err)
	}
	c.Candles5m = candles5

	if len(candles1) > 0 {
		c.CurrentPrice = candles1[len(candles1)-1].Close
		c.LastPriceUpdateTime = time.Now().UTC()
		c.DataSource = "BINANCE_REST"
	}

	util.Logger.Info("Warmup completed successfully",
		zap.Int("candles1m", len(c.Candles1m)),
		zap.Int("candles5m", len(c.Candles5m)),
		zap.Float64("currentPrice", c.CurrentPrice),
	)
	return nil
}

func (c *Client) fetchKlines(interval string, limit int) ([]Candle, error) {
	url := fmt.Sprintf("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=%s&limit=%d", interval, limit)
	resp, err := c.httpClient.Get(url)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("http status error: %d", resp.StatusCode)
	}

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}

	var data [][]interface{}
	if err := json.Unmarshal(body, &data); err != nil {
		return nil, err
	}

	candles := make([]Candle, 0, len(data))
	for _, raw := range data {
		if len(raw) < 6 {
			continue
		}
		openTimeMs, _ := raw[0].(float64)
		openPrice, _ := strconv.ParseFloat(raw[1].(string), 64)
		highPrice, _ := strconv.ParseFloat(raw[2].(string), 64)
		lowPrice, _ := strconv.ParseFloat(raw[3].(string), 64)
		closePrice, _ := strconv.ParseFloat(raw[4].(string), 64)
		vol, _ := strconv.ParseFloat(raw[5].(string), 64)

		candles = append(candles, Candle{
			StartTime: time.UnixMilli(int64(openTimeMs)).UTC(),
			Open:      openPrice,
			High:      highPrice,
			Low:       lowPrice,
			Close:     closePrice,
			Volume:    vol,
		})
	}

	return candles, nil
}

// UpdateFromTrade updates real-time BTC price and processes returning log-returns.
func (c *Client) UpdateFromTrade(price float64, size float64, t time.Time, isWS bool) {
	c.mu.Lock()
	defer c.mu.Unlock()

	oldPrice := c.CurrentPrice
	c.CurrentPrice = price
	c.LastPriceUpdateTime = time.Now().UTC()
	if isWS {
		c.DataSource = "BINANCE_WS"
	} else {
		c.DataSource = "BINANCE_REST"
	}

	if oldPrice > 0 {
		ret := math.Log(price / oldPrice)
		if len(c.LogReturns) >= 60 {
			c.LogReturns = c.LogReturns[1:]
		}
		c.LogReturns = append(c.LogReturns, ret)
	}

	// Update the latest candle
	c.aggregateCandle(price, size, t, "1m")
	c.aggregateCandle(price, size, t, "5m")
}

func (c *Client) aggregateCandle(price float64, size float64, t time.Time, interval string) {
	var list *[]Candle
	var duration time.Duration
	if interval == "1m" {
		list = &c.Candles1m
		duration = time.Minute
	} else {
		list = &c.Candles5m
		duration = 5 * time.Minute
	}

	if len(*list) == 0 {
		return
	}

	roundedTime := t.Truncate(duration)
	lastIdx := len(*list) - 1
	lastCandle := (*list)[lastIdx]

	if roundedTime.Equal(lastCandle.StartTime) {
		if price > lastCandle.High {
			(*list)[lastIdx].High = price
		}
		if price < lastCandle.Low {
			(*list)[lastIdx].Low = price
		}
		(*list)[lastIdx].Close = price
		(*list)[lastIdx].Volume += size
	} else if roundedTime.After(lastCandle.StartTime) {
		newCandle := Candle{
			StartTime: roundedTime,
			Open:      price,
			High:      price,
			Low:       price,
			Close:     price,
			Volume:    size,
		}
		*list = append(*list, newCandle)
		if len(*list) > 300 {
			*list = (*list)[1:]
		}
	}
}

// UpdateDepth updates the real-time order flow and implements the spoof filter logic.
func (c *Client) UpdateDepth(bidsRaw [][]string, asksRaw [][]string, t time.Time) {
	c.mu.Lock()
	defer c.mu.Unlock()

	bids := parseLevels(bidsRaw)
	asks := parseLevels(asksRaw)

	c.LastBids = bids
	c.LastAsks = asks

	snap := DepthSnapshot{
		Timestamp: t,
		Bids:      bids,
		Asks:      asks,
	}
	if len(c.Snapshots) >= 10 {
		c.Snapshots = c.Snapshots[1:]
	}
	c.Snapshots = append(c.Snapshots, snap)

	c.trackOrderLife(bids, t, true)
	c.trackOrderLife(asks, t, false)
}

func parseLevels(raw [][]string) map[float64]float64 {
	m := make(map[float64]float64)
	for _, r := range raw {
		if len(r) < 2 {
			continue
		}
		p, _ := strconv.ParseFloat(r[0], 64)
		s, _ := strconv.ParseFloat(r[1], 64)
		m[p] = s
	}
	return m
}

func (c *Client) trackOrderLife(current map[float64]float64, t time.Time, isBid bool) {
	seenMap := c.SeenBids
	if !isBid {
		seenMap = c.SeenAsks
	}

	for p, size := range current {
		if size == 0 {
			if ord, ok := seenMap[p]; ok {
				ord.LastSeen = t
			}
		} else {
			if ord, ok := seenMap[p]; ok {
				ord.LastSeen = t
				ord.Size = size
			} else {
				seenMap[p] = &SeenOrder{
					FirstSeen: t,
					LastSeen:  t,
					Size:      size,
				}
			}
		}
	}

	for p, ord := range seenMap {
		if t.Sub(ord.LastSeen) > 10*time.Second {
			delete(seenMap, p)
		}
	}
}

// IsSpoofing candidate checker
func (c *Client) IsSpoofing(price float64, size float64, isBid bool) bool {
	c.mu.RLock()
	defer c.mu.RUnlock()

	seenMap := c.SeenBids
	if !isBid {
		seenMap = c.SeenAsks
	}

	order, ok := seenMap[price]
	if !ok {
		return false
	}

	duration := order.LastSeen.Sub(order.FirstSeen)
	if duration > time.Second {
		return false
	}

	var currentSize float64
	if isBid {
		currentSize = c.LastBids[price]
	} else {
		currentSize = c.LastAsks[price]
	}

	if currentSize > 0 {
		return false
	}

	medianSize := c.getMedianDepthSize(isBid)
	threshold := math.Max(2.0, medianSize*3.0)

	return size >= threshold
}

func (c *Client) getMedianDepthSize(isBid bool) float64 {
	var list []float64
	if isBid {
		for _, s := range c.LastBids {
			if s > 0 {
				list = append(list, s)
			}
		}
	} else {
		for _, s := range c.LastAsks {
			if s > 0 {
				list = append(list, s)
			}
		}
	}

	if len(list) == 0 {
		return 0.0
	}

	sort.Float64s(list)
	n := len(list)
	if n%2 == 1 {
		return list[n/2]
	}
	return (list[n/2-1] + list[n/2]) / 2.0
}

func (c *Client) GetLastBidsAndAsks() (map[float64]float64, map[float64]float64) {
	c.mu.RLock()
	defer c.mu.RUnlock()

	bidsCopy := make(map[float64]float64, len(c.LastBids))
	for k, v := range c.LastBids {
		bidsCopy[k] = v
	}

	asksCopy := make(map[float64]float64, len(c.LastAsks))
	for k, v := range c.LastAsks {
		asksCopy[k] = v
	}

	return bidsCopy, asksCopy
}

func (c *Client) GetCandles(interval string) []Candle {
	c.mu.RLock()
	defer c.mu.RUnlock()

	if interval == "1m" {
		ret := make([]Candle, len(c.Candles1m))
		copy(ret, c.Candles1m)
		return ret
	}
	ret := make([]Candle, len(c.Candles5m))
	copy(ret, c.Candles5m)
	return ret
}

func (c *Client) GetPrice() float64 {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.CurrentPrice
}

func (c *Client) GetLogReturns() []float64 {
	c.mu.RLock()
	defer c.mu.RUnlock()
	ret := make([]float64, len(c.LogReturns))
	copy(ret, c.LogReturns)
	return ret
}

func (c *Client) GetDataSource() string {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.DataSource
}

// FetchTickerPriceREST fallbacks on REST if WebSocket fails
func (c *Client) FetchTickerPriceREST() (float64, error) {
	resp, err := c.httpClient.Get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return 0, fmt.Errorf("unexpected status: %d", resp.StatusCode)
	}

	var data struct {
		Price string `json:"price"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&data); err != nil {
		return 0, err
	}

	return strconv.ParseFloat(data.Price, 64)
}
