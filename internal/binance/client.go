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

	"go.uber.org/zap"
	"pm-edge/internal/util"
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

	Candles1m []Candle
	Candles5m []Candle

	CurrentPrice float64
	LogReturns   []float64

	returnSampleTime  time.Time
	returnSamplePrice float64

	LastBids map[float64]float64
	LastAsks map[float64]float64

	Snapshots []DepthSnapshot
	SeenBids  map[float64]*SeenOrder
	SeenAsks  map[float64]*SeenOrder

	isWsConnected bool
	wsFallback    bool
	DataSource    string
	DepthSource   string

	LastPriceUpdateTime time.Time
	LastDepthUpdateTime time.Time
}

func NewClient() *Client {
	return &Client{
		httpClient:  &http.Client{Timeout: 5 * time.Second},
		LogReturns:  make([]float64, 0, 60),
		LastBids:    make(map[float64]float64),
		LastAsks:    make(map[float64]float64),
		SeenBids:    make(map[float64]*SeenOrder),
		SeenAsks:    make(map[float64]*SeenOrder),
		Snapshots:   make([]DepthSnapshot, 0, 10),
		DataSource:  "UNINITIALIZED",
		DepthSource: "UNINITIALIZED",
		wsFallback:  true,
	}
}

func (c *Client) WarmupCandles() error {
	candles1m, err := c.fetchKlines("1m", 200)
	if err != nil {
		return fmt.Errorf("1m klines warmup failed: %w", err)
	}
	candles5m, err := c.fetchKlines("5m", 200)
	if err != nil {
		return fmt.Errorf("5m klines warmup failed: %w", err)
	}

	c.mu.Lock()
	defer c.mu.Unlock()
	c.Candles1m = candles1m
	c.Candles5m = candles5m
	if len(candles1m) > 0 {
		c.CurrentPrice = candles1m[len(candles1m)-1].Close
		now := time.Now().UTC()
		c.LastPriceUpdateTime = now
		c.returnSampleTime = now.Truncate(time.Second)
		c.returnSamplePrice = c.CurrentPrice
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
	endpoint := fmt.Sprintf("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=%s&limit=%d", interval, limit)
	resp, err := c.httpClient.Get(endpoint)
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
		openTimeMs, ok := raw[0].(float64)
		if !ok {
			continue
		}
		openPrice, err1 := strconv.ParseFloat(fmt.Sprint(raw[1]), 64)
		highPrice, err2 := strconv.ParseFloat(fmt.Sprint(raw[2]), 64)
		lowPrice, err3 := strconv.ParseFloat(fmt.Sprint(raw[3]), 64)
		closePrice, err4 := strconv.ParseFloat(fmt.Sprint(raw[4]), 64)
		volume, err5 := strconv.ParseFloat(fmt.Sprint(raw[5]), 64)
		if err1 != nil || err2 != nil || err3 != nil || err4 != nil || err5 != nil {
			continue
		}
		candles = append(candles, Candle{StartTime: time.UnixMilli(int64(openTimeMs)).UTC(), Open: openPrice, High: highPrice, Low: lowPrice, Close: closePrice, Volume: volume})
	}
	return candles, nil
}

func (c *Client) UpdateFromTrade(price float64, size float64, eventTime time.Time, isWS bool) {
	if price <= 0 {
		return
	}
	eventTime = eventTime.UTC()
	c.mu.Lock()
	defer c.mu.Unlock()
	c.CurrentPrice = price
	c.LastPriceUpdateTime = time.Now().UTC()
	if isWS {
		c.DataSource = "BINANCE_WS"
	} else {
		c.DataSource = "BINANCE_REST"
	}
	second := eventTime.Truncate(time.Second)
	switch {
	case c.returnSampleTime.IsZero() || c.returnSamplePrice <= 0:
		c.returnSampleTime = second
		c.returnSamplePrice = price
	case second.Equal(c.returnSampleTime):
		c.returnSamplePrice = price
	case second.After(c.returnSampleTime):
		deltaSeconds := second.Sub(c.returnSampleTime).Seconds()
		if deltaSeconds > 0 {
			oneSecondReturn := math.Log(price/c.returnSamplePrice) / deltaSeconds
			if len(c.LogReturns) >= 60 {
				c.LogReturns = c.LogReturns[1:]
			}
			c.LogReturns = append(c.LogReturns, oneSecondReturn)
		}
		c.returnSampleTime = second
		c.returnSamplePrice = price
	}
	c.aggregateCandle(price, size, eventTime, "1m")
	c.aggregateCandle(price, size, eventTime, "5m")
}

func (c *Client) aggregateCandle(price float64, size float64, eventTime time.Time, interval string) {
	var candles *[]Candle
	var duration time.Duration
	if interval == "1m" {
		candles = &c.Candles1m
		duration = time.Minute
	} else {
		candles = &c.Candles5m
		duration = 5 * time.Minute
	}
	if len(*candles) == 0 {
		return
	}
	bucket := eventTime.Truncate(duration)
	lastIndex := len(*candles) - 1
	last := (*candles)[lastIndex]
	if bucket.Equal(last.StartTime) {
		if price > last.High {
			(*candles)[lastIndex].High = price
		}
		if price < last.Low {
			(*candles)[lastIndex].Low = price
		}
		(*candles)[lastIndex].Close = price
		(*candles)[lastIndex].Volume += size
		return
	}
	if bucket.After(last.StartTime) {
		*candles = append(*candles, Candle{StartTime: bucket, Open: price, High: price, Low: price, Close: price, Volume: size})
		if len(*candles) > 300 {
			*candles = (*candles)[1:]
		}
	}
}

func (c *Client) UpdateDepth(bidsRaw [][]string, asksRaw [][]string, timestamp time.Time) {
	c.UpdateDepthWithSource(bidsRaw, asksRaw, timestamp, "BINANCE_DEPTH")
}

func (c *Client) UpdateDepthWithSource(bidsRaw [][]string, asksRaw [][]string, timestamp time.Time, source string) {
	timestamp = timestamp.UTC()
	c.mu.Lock()
	defer c.mu.Unlock()
	bids := parseLevels(bidsRaw)
	asks := parseLevels(asksRaw)
	if len(bids) == 0 || len(asks) == 0 {
		return
	}
	c.LastBids = bids
	c.LastAsks = asks
	c.LastDepthUpdateTime = time.Now().UTC()
	if source != "" {
		c.DepthSource = source
	}
	if len(c.Snapshots) >= 10 {
		c.Snapshots = c.Snapshots[1:]
	}
	c.Snapshots = append(c.Snapshots, DepthSnapshot{Timestamp: timestamp, Bids: cloneDepth(bids), Asks: cloneDepth(asks)})
	c.trackOrderLife(bids, timestamp, true)
	c.trackOrderLife(asks, timestamp, false)
}

func parseLevels(raw [][]string) map[float64]float64 {
	levels := make(map[float64]float64)
	for _, row := range raw {
		if len(row) < 2 {
			continue
		}
		price, errPrice := strconv.ParseFloat(row[0], 64)
		size, errSize := strconv.ParseFloat(row[1], 64)
		if errPrice != nil || errSize != nil || price <= 0 || size <= 0 {
			continue
		}
		levels[price] = size
	}
	return levels
}

func cloneDepth(src map[float64]float64) map[float64]float64 {
	dst := make(map[float64]float64, len(src))
	for price, size := range src {
		dst[price] = size
	}
	return dst
}

func (c *Client) trackOrderLife(current map[float64]float64, timestamp time.Time, isBid bool) {
	seen := c.SeenBids
	if !isBid {
		seen = c.SeenAsks
	}
	for price, size := range current {
		if order, ok := seen[price]; ok {
			order.LastSeen = timestamp
			order.Size = size
		} else {
			seen[price] = &SeenOrder{FirstSeen: timestamp, LastSeen: timestamp, Size: size}
		}
	}
	for price, order := range seen {
		if _, stillPresent := current[price]; !stillPresent && timestamp.Sub(order.LastSeen) > 10*time.Second {
			delete(seen, price)
		}
	}
}

func (c *Client) IsSpoofing(price float64, size float64, isBid bool) bool {
	c.mu.RLock()
	defer c.mu.RUnlock()
	seen := c.SeenBids
	if !isBid {
		seen = c.SeenAsks
	}
	order, ok := seen[price]
	if !ok {
		return false
	}
	if order.LastSeen.Sub(order.FirstSeen) >= time.Second {
		return false
	}
	medianSize := c.getMedianDepthSizeLocked(isBid)
	threshold := math.Max(2.0, medianSize*3.0)
	return size >= threshold
}

func (c *Client) getMedianDepthSize(isBid bool) float64 {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.getMedianDepthSizeLocked(isBid)
}

func (c *Client) getMedianDepthSizeLocked(isBid bool) float64 {
	var source map[float64]float64
	if isBid {
		source = c.LastBids
	} else {
		source = c.LastAsks
	}
	values := make([]float64, 0, len(source))
	for _, size := range source {
		if size > 0 {
			values = append(values, size)
		}
	}
	if len(values) == 0 {
		return 0
	}
	sort.Float64s(values)
	middle := len(values) / 2
	if len(values)%2 == 1 {
		return values[middle]
	}
	return (values[middle-1] + values[middle]) / 2
}

func (c *Client) GetLastBidsAndAsks() (map[float64]float64, map[float64]float64) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return cloneDepth(c.LastBids), cloneDepth(c.LastAsks)
}

func (c *Client) GetCandles(interval string) []Candle {
	c.mu.RLock()
	defer c.mu.RUnlock()
	var source []Candle
	if interval == "1m" {
		source = c.Candles1m
	} else {
		source = c.Candles5m
	}
	result := make([]Candle, len(source))
	copy(result, source)
	return result
}

func (c *Client) GetPrice() float64 {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.CurrentPrice
}

func (c *Client) GetLogReturns() []float64 {
	c.mu.RLock()
	defer c.mu.RUnlock()
	result := make([]float64, len(c.LogReturns))
	copy(result, c.LogReturns)
	return result
}

func (c *Client) GetDataSource() string {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.DataSource
}

func (c *Client) GetDepthDataSource() string {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.DepthSource
}

func (c *Client) IsPriceFresh(maxAge time.Duration) bool {
	return c.IsPriceFreshAt(time.Now().UTC(), maxAge)
}

func (c *Client) IsPriceFreshAt(now time.Time, maxAge time.Duration) bool {
	c.mu.RLock()
	defer c.mu.RUnlock()
	if c.LastPriceUpdateTime.IsZero() {
		return false
	}
	age := now.UTC().Sub(c.LastPriceUpdateTime)
	return age >= 0 && age <= maxAge
}

func (c *Client) IsDepthFresh(maxAge time.Duration) bool {
	return c.IsDepthFreshAt(time.Now().UTC(), maxAge)
}

func (c *Client) IsDepthFreshAt(now time.Time, maxAge time.Duration) bool {
	c.mu.RLock()
	defer c.mu.RUnlock()
	if c.LastDepthUpdateTime.IsZero() || len(c.LastBids) == 0 || len(c.LastAsks) == 0 {
		return false
	}
	age := now.UTC().Sub(c.LastDepthUpdateTime)
	return age >= 0 && age <= maxAge
}

func (c *Client) DepthAge(now time.Time) time.Duration {
	c.mu.RLock()
	defer c.mu.RUnlock()
	if c.LastDepthUpdateTime.IsZero() {
		return -1
	}
	return now.UTC().Sub(c.LastDepthUpdateTime)
}

func (c *Client) SetWSState(connected bool, fallback bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.isWsConnected = connected
	c.wsFallback = fallback
}

func (c *Client) GetWSState() (bool, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.isWsConnected, c.wsFallback
}

func (c *Client) ShouldRESTFallback(now time.Time, staleAfter time.Duration) bool {
	c.mu.RLock()
	defer c.mu.RUnlock()
	if c.wsFallback || !c.isWsConnected || c.LastPriceUpdateTime.IsZero() {
		return true
	}
	age := now.UTC().Sub(c.LastPriceUpdateTime)
	return age < 0 || age > staleAfter
}

func (c *Client) FetchTickerPriceREST() (float64, error) {
	resp, err := c.httpClient.Get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return 0, fmt.Errorf("unexpected status: %d", resp.StatusCode)
	}
	var payload struct {
		Price string `json:"price"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return 0, err
	}
	return strconv.ParseFloat(payload.Price, 64)
}

func (c *Client) FetchDepthREST() ([][]string, [][]string, error) {
	resp, err := c.httpClient.Get("https://api.binance.com/api/v3/depth?symbol=BTCUSDT&limit=20")
	if err != nil {
		return nil, nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, nil, fmt.Errorf("depth status: %d", resp.StatusCode)
	}
	var payload struct {
		Bids [][]string `json:"bids"`
		Asks [][]string `json:"asks"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return nil, nil, err
	}
	if len(payload.Bids) == 0 || len(payload.Asks) == 0 {
		return nil, nil, fmt.Errorf("empty depth snapshot")
	}
	return payload.Bids, payload.Asks, nil
}
