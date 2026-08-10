package chainlink

import (
	"encoding/json"
	"fmt"
	"sync"
	"time"

	"github.com/gorilla/websocket"
	"go.uber.org/zap"
	"pm-edge/internal/util"
)

const (
	defaultRTDSURL = "wss://ws-live-data.polymarket.com"
	anchorGrace    = 3 * time.Second
	freshnessLimit = 3 * time.Second
	readTimeout    = 15 * time.Second
	anchorRetention = 24 * time.Hour
)

type tick struct {
	Price float64
	Time  time.Time
}

type Snapshot struct {
	CurrentPrice float64
	PriceToBeat  float64
	LastUpdate   time.Time
	Ready        bool
	Fresh        bool
}

type Client struct {
	mu           sync.RWMutex
	current      tick
	anchors      map[int64]tick
	lastObserved tick
	url          string
	stopChan     chan struct{}
	stopOnce     sync.Once
	wg           sync.WaitGroup
	connMu       sync.Mutex
	conn         *websocket.Conn
}

func NewClient() *Client { return NewClientWithURL(defaultRTDSURL) }

func NewClientWithURL(url string) *Client {
	return &Client{anchors: make(map[int64]tick), url: url, stopChan: make(chan struct{})}
}

func (c *Client) Start() {
	c.wg.Add(1)
	go c.run()
}

func (c *Client) Stop() {
	c.stopOnce.Do(func() { close(c.stopChan) })
	c.connMu.Lock()
	if c.conn != nil {
		_ = c.conn.Close()
	}
	c.connMu.Unlock()
	c.wg.Wait()
}

// Observe is intentionally public so deterministic tests/mock feeds can
// exercise the same boundary anchoring logic as the live RTDS stream.
func (c *Client) Observe(price float64, ts time.Time) {
	if price <= 0 || ts.IsZero() {
		return
	}
	ts = ts.UTC()
	c.mu.Lock()
	defer c.mu.Unlock()

	c.current = tick{Price: price, Time: ts}
	boundary := ts.Unix() - ts.Unix()%300
	boundaryTime := time.Unix(boundary, 0).UTC()

	if _, exists := c.anchors[boundary]; !exists {
		if !c.lastObserved.Time.IsZero() && c.lastObserved.Time.Before(boundaryTime) && !ts.Before(boundaryTime) {
			candidate := tick{Price: price, Time: ts}
			if boundaryTime.Sub(c.lastObserved.Time) <= ts.Sub(boundaryTime) {
				candidate = c.lastObserved
			}
			if absDuration(candidate.Time.Sub(boundaryTime)) <= anchorGrace {
				c.anchors[boundary] = candidate
			}
		} else if !ts.Before(boundaryTime) && ts.Sub(boundaryTime) <= anchorGrace {
			c.anchors[boundary] = tick{Price: price, Time: ts}
		}
	}
	c.lastObserved = tick{Price: price, Time: ts}

	retentionCutoff := ts.Add(-anchorRetention).Unix()
	for key := range c.anchors {
		if key < retentionCutoff {
			delete(c.anchors, key)
		}
	}
}

func (c *Client) Snapshot(windowStart, now time.Time) Snapshot {
	c.mu.RLock()
	defer c.mu.RUnlock()
	anchor, ok := c.anchors[windowStart.UTC().Unix()]
	age := now.UTC().Sub(c.current.Time)
	fresh := !c.current.Time.IsZero() && age >= 0 && age <= freshnessLimit
	return Snapshot{CurrentPrice: c.current.Price, PriceToBeat: anchor.Price, LastUpdate: c.current.Time, Ready: ok && anchor.Price > 0 && c.current.Price > 0, Fresh: fresh}
}

// BoundaryPrice returns the settlement-aligned Chainlink price captured around
// an exact 5-minute boundary. BTC 5m paper positions use the end boundary as
// their closing value and the start boundary as Price To Beat.
func (c *Client) BoundaryPrice(boundary time.Time) (float64, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	anchor, ok := c.anchors[boundary.UTC().Unix()]
	return anchor.Price, ok && anchor.Price > 0
}

func (c *Client) run() {
	defer c.wg.Done()
	backoff := time.Second
	for {
		select {
		case <-c.stopChan:
			return
		default:
		}
		conn, _, err := websocket.DefaultDialer.Dial(c.url, nil)
		if err != nil {
			util.Logger.Warn("Chainlink RTDS connection failed", zap.Error(err), zap.Duration("backoff", backoff))
			if !c.sleep(backoff) {
				return
			}
			backoff = nextBackoff(backoff)
			continue
		}
		c.connMu.Lock()
		c.conn = conn
		c.connMu.Unlock()
		if err := c.subscribe(conn); err != nil {
			_ = conn.Close()
			util.Logger.Warn("Chainlink RTDS subscribe failed", zap.Error(err))
			if !c.sleep(backoff) {
				return
			}
			backoff = nextBackoff(backoff)
			continue
		}
		util.Logger.Info("Connected to Polymarket Chainlink RTDS", zap.String("symbol", "btc/usd"))
		backoff = time.Second
		pingDone := make(chan struct{})
		go c.pingLoop(conn, pingDone)
		err = c.readLoop(conn)
		close(pingDone)
		_ = conn.Close()
		c.connMu.Lock()
		if c.conn == conn {
			c.conn = nil
		}
		c.connMu.Unlock()
		select {
		case <-c.stopChan:
			return
		default:
			util.Logger.Warn("Chainlink RTDS disconnected", zap.Error(err))
		}
		if !c.sleep(time.Second) {
			return
		}
	}
}

func (c *Client) subscribe(conn *websocket.Conn) error {
	return conn.WriteJSON(map[string]interface{}{
		"action":        "subscribe",
		"subscriptions": []map[string]string{{"topic": "crypto_prices_chainlink", "type": "*", "filters": `{"symbol":"btc/usd"}`}},
	})
}

type rtdsMessage struct {
	Topic   string          `json:"topic"`
	Type    string          `json:"type"`
	Payload json.RawMessage `json:"payload"`
}

type pricePayload struct {
	Symbol    string  `json:"symbol"`
	Timestamp int64   `json:"timestamp"`
	Value     float64 `json:"value"`
}

func (c *Client) readLoop(conn *websocket.Conn) error {
	for {
		_ = conn.SetReadDeadline(time.Now().Add(readTimeout))
		_, raw, err := conn.ReadMessage()
		if err != nil {
			return err
		}
		var msg rtdsMessage
		if err := json.Unmarshal(raw, &msg); err != nil || msg.Topic != "crypto_prices_chainlink" || msg.Type != "update" {
			continue
		}
		var payload pricePayload
		if err := json.Unmarshal(msg.Payload, &payload); err != nil {
			continue
		}
		if payload.Symbol != "btc/usd" || payload.Value <= 0 || payload.Timestamp <= 0 {
			continue
		}
		c.Observe(payload.Value, time.UnixMilli(payload.Timestamp).UTC())
	}
}

func (c *Client) pingLoop(conn *websocket.Conn, done <-chan struct{}) {
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-done:
			return
		case <-c.stopChan:
			return
		case <-ticker.C:
			c.connMu.Lock()
			if c.conn == conn {
				_ = conn.WriteMessage(websocket.TextMessage, []byte("PING"))
			}
			c.connMu.Unlock()
		}
	}
}

func (c *Client) sleep(d time.Duration) bool {
	select {
	case <-c.stopChan:
		return false
	case <-time.After(d):
		return true
	}
}

func nextBackoff(d time.Duration) time.Duration {
	d = time.Duration(float64(d) * 1.5)
	if d > 30*time.Second {
		return 30 * time.Second
	}
	return d
}

func absDuration(d time.Duration) time.Duration {
	if d < 0 {
		return -d
	}
	return d
}

func (s Snapshot) String() string {
	return fmt.Sprintf("current=%.2f ptb=%.2f ready=%t fresh=%t", s.CurrentPrice, s.PriceToBeat, s.Ready, s.Fresh)
}
