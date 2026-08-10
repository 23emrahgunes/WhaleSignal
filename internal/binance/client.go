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
	LogReturns   []float64 // one-second-equivalent log returns, max 60 samples

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
		DataSource: "UNINITIALIZED",
		wsFallback: true,
	}
}

func (c *Client) WarmupCandles() error {
	candles1, err := c.fetchKlines("1m", 200)
	if err != nil {
		return fmt.Errorf("1m klines warmup failed: %w", err)
	}
	candles5, err := c.fetchKlines("5m", 200)
	if err != nil {
		return fmt.Errorf("5m klines warmup failed: %w", err)
	}

	c.mu.Lock()
	defer c.mu.Unlock()
	c.Candles1m = candles1
	c.Candles5m = candles5
	if len(candles1) > 0 {
		c.CurrentPrice = candles1[len(candles1)-1].Close
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
		openTimeMs, ok := raw[0].(float64)
		if !ok {
			continue
		}
		openPrice, err1 := strconv.ParseFloat(fmt.Sprint(raw[1]), 64)
		highPrice, err2 := strconv.ParseFloat(fmt.Sprint(raw[2]), 64)
		lowPrice, err3 := strconv.ParseFloat(fmt.Sprint(raw[3]), 64)
		closePrice, err4 := strconv.ParseFloat(fmt.Sprint(raw[4]), 64)
		vol, err5 := strconv.ParseFloat(fmt.Sprint(raw[5]), 64)
		if err1 != nil || err2 != nil || err3 != nil || err4 != nil || err5 != nil {
			continue
		}

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

// UpdateFromTrade updates price and produces at most one return sample per second.
// Gaps are normalized to a one-second equivalent so the annualization in the
// probability model is not accidentally based on raw trade-event frequency.
func (c *Client) UpdateFromTrade(price float64, size float64, t time.Time, isWS bool) {
	if price <= 0 {
		return
	}
	t = t.UTC()

	c.mu.Lock()
	defer c.mu.Unlock()

	c.CurrentPrice = price
	c.LastPriceUpdateTime = time.Now().UTC()
	if isWS {
		c.DataSource = "BINANCE_WS"
	} else {
		c.DataSource = "BINANCE_REST"
	}

	sec := t.Truncate(time.Second)
	if c.returnSampleTime.IsZero() || c.returnSamplePrice <= 0 {
		c.returnSampleTime = sec
		c.returnSamplePrice = price
	} else if sec.Equal(c.returnSampleTime) {
		c.returnSamplePrice = price
	} else if sec.After(c.returnSampleTime) {
		deltaSec := sec.Sub(c.returnSampleTime).Seconds()
		if deltaSec > 0 {
			ret := math.Log(price/c.returnSamplePrice) / deltaSec
			if len(c.LogReturns) >= 60 {
				c.LogReturns = c.LogReturns[1:]
			}
			c.LogReturns = append(c.LogReturns, ret)
		}
		c.returnSampleTime = sec
		c.returnSamplePrice = price
	}

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
		*list = append(*list, Candle{
			StartTime: roundedTime,
			Open:      price,
			High:      price,
			Low:       price,
			Close:     price,
			Volume:    size,
		})
		if len(*list) > 300 {
			*list = (*list)[1:]
		}
	}
}

func (c *Client) UpdateDepth(bidsRaw [][]string, asksRaw [][]string, t time.Time) {
	c.mu.Lock()
	defer c.mu.Unlock()

	bids := parseLevels(bidsRaw)
	asks := parseLevels(asksRaw)
	c.LastBids = bids
	c.LastAsks = asks

	if len(c.Snapshots) >= 10 {
		c.Snapshots = c.Snapshots[1:]
	}
	c.Snapshots = append(c.Snapshots, DepthSnapshot{Timestamp: t, Bids: bids, Asks: asks})

	c.trackOrderLife(bids, t, true)
	c.trackOrderLife(asks, t, false)
}

func parseLevels(raw [][]string) map[float64]float64 {
	m := make(map[float64]float64)
	for _, r := range raw {
		if len(r) < 2 {
			continue
		}
		p, errP := strconv.ParseFloat(r[0], 64)
		s, errS := strconv.ParseFloat(r[1], 64)
		if errP != nil || errS != nil || p <= 0 || s <= 0 {
			continue
		}
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
		if ord, ok := seenMap[p]; ok {
			ord.LastSeen = t
			ord.Size = size
		} else {
			seenMap[p] = &SeenOrder{FirstSeen: t, LastSeen: t, Size: size}
		}
	}

	for p, ord := range seenMap {
		if _, stillPresent := current[p]; !stillPresent && t.Sub(ord.LastSeen) > 10*time.Second {
			delete(seenMap, p)
		}
	}
}

// IsSpoofing treats a very large newly appeared level as untrusted until it
// persists for at least one second. This is intentionally conservative: a
// vanished order cannot affect current imbalance because it is not in LastBids/LastAsks.
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
		return 0
	}
	sort.Float64s(list)
	n := len(list)
	if n%2 == 1 {
		return list[n/2]
	}
	return (list[n/2-1] + list[n/2]) / 2
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

func (c *Client) SetWSState(connected, fallback bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.isWsConnected = connected
	c.wsFallback = fallback
}

func (c *Client) GetWSState() (connected, fallback bool) {
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
	var data struct {
		Price string `json:"price"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&data); err != nil {
		return 0, err
	}
	return strconv.ParseFloat(data.Price, 64)
}
