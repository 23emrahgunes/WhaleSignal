package binance

import (
	"encoding/json"
	"fmt"
	"math/rand"
	"strconv"
	"sync"
	"time"

	"github.com/gorilla/websocket"
	"go.uber.org/zap"
	"pm-edge/internal/util"
)

type WSManager struct {
	client     *Client
	stopChan   chan struct{}
	wg         sync.WaitGroup
	wsConn     *websocket.Conn
	mu         sync.Mutex
	lastTrade  time.Time
	IsMockMode bool
}

func NewWSManager(client *Client, isMockMode bool) *WSManager {
	return &WSManager{
		client:     client,
		stopChan:   make(chan struct{}),
		IsMockMode: isMockMode,
	}
}

func (w *WSManager) Start() {
	w.wg.Add(1)
	go w.run()
}

func (w *WSManager) Stop() {
	select {
	case <-w.stopChan:
		return
	default:
		close(w.stopChan)
	}
	w.mu.Lock()
	if w.wsConn != nil {
		_ = w.wsConn.Close()
	}
	w.mu.Unlock()
	w.wg.Wait()
}

func (w *WSManager) run() {
	defer w.wg.Done()
	url := "wss://stream.binance.com:9443/stream?streams=btcusdt@trade/btcusdt@depth20@100ms"
	backoff := time.Second

	for {
		select {
		case <-w.stopChan:
			return
		default:
		}

		util.Logger.Info("Connecting to Binance WS streams...", zap.String("url", url))
		conn, _, err := websocket.DefaultDialer.Dial(url, nil)
		if err != nil {
			w.client.SetWSState(false, true)
			util.Logger.Warn("Binance WS connection failed; REST watchdog active", zap.Error(err), zap.Duration("backoff", backoff))
			if !w.sleepWithContext(backoff) {
				return
			}
			backoff = nextWSBackoff(backoff)
			continue
		}

		backoff = time.Second
		w.mu.Lock()
		w.wsConn = conn
		w.lastTrade = time.Time{}
		w.mu.Unlock()
		w.client.SetWSState(true, false)
		util.Logger.Info("Connected to Binance WebSocket stream")

		err = w.readLoop(conn)
		w.client.SetWSState(false, true)
		if err != nil {
			util.Logger.Warn("Binance WebSocket connection lost", zap.Error(err))
		}
		_ = conn.Close()

		w.mu.Lock()
		if w.wsConn == conn {
			w.wsConn = nil
		}
		w.mu.Unlock()

		if !w.sleepWithContext(time.Second) {
			return
		}
	}
}

func (w *WSManager) sleepWithContext(d time.Duration) bool {
	select {
	case <-w.stopChan:
		return false
	case <-time.After(d):
		return true
	}
}

func nextWSBackoff(d time.Duration) time.Duration {
	d = time.Duration(float64(d) * 1.5)
	if d > 60*time.Second {
		return 60 * time.Second
	}
	return d
}

type CombinedStreamPayload struct {
	Stream string          `json:"stream"`
	Data   json.RawMessage `json:"data"`
}

type TradeEvent struct {
	EventTime int64  `json:"E"`
	Price     string `json:"p"`
	Quantity  string `json:"q"`
}

type DepthEvent struct {
	Bids [][]string `json:"b"`
	Asks [][]string `json:"a"`
}

func (w *WSManager) readLoop(conn *websocket.Conn) error {
	for {
		_, msg, err := conn.ReadMessage()
		if err != nil {
			return err
		}

		var payload CombinedStreamPayload
		if err := json.Unmarshal(msg, &payload); err != nil {
			continue
		}

		switch payload.Stream {
		case "btcusdt@trade":
			var ev TradeEvent
			if err := json.Unmarshal(payload.Data, &ev); err != nil {
				continue
			}
			price, errP := strconv.ParseFloat(ev.Price, 64)
			size, errQ := strconv.ParseFloat(ev.Quantity, 64)
			if errP != nil || errQ != nil || price <= 0 {
				continue
			}
			t := time.UnixMilli(ev.EventTime).UTC()
			w.client.UpdateFromTrade(price, size, t, true)
			w.mu.Lock()
			w.lastTrade = time.Now().UTC()
			w.mu.Unlock()
		case "btcusdt@depth20@100ms":
			var ev DepthEvent
			if err := json.Unmarshal(payload.Data, &ev); err == nil {
				w.client.UpdateDepth(ev.Bids, ev.Asks, time.Now().UTC())
			}
		}
	}
}

func (w *WSManager) tradeFeedFresh(now time.Time, maxAge time.Duration) bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.lastTrade.IsZero() {
		return false
	}
	age := now.UTC().Sub(w.lastTrade)
	return age >= 0 && age <= maxAge
}

// StartFallbackRESTPoller polls REST whenever the WS connection is absent OR
// connected-but-not-delivering trade ticks. A TCP-open zombie socket can no
// longer freeze the model on an old BTC price.
func (w *WSManager) StartFallbackRESTPoller() {
	w.wg.Add(1)
	go func() {
		defer w.wg.Done()
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()

		for {
			select {
			case <-w.stopChan:
				return
			case now := <-ticker.C:
				if w.IsMockMode {
					continue
				}
				connected, fallback := w.client.GetWSState()
				needREST := fallback || !connected || !w.tradeFeedFresh(now.UTC(), 3*time.Second)
				if !needREST {
					continue
				}
				price, err := w.client.FetchTickerPriceREST()
				if err != nil {
					util.Logger.Warn("Binance fallback REST poller failed", zap.Error(err))
					continue
				}
				w.client.UpdateFromTrade(price, 0, now.UTC(), false)
			}
		}
	}()
}

func (w *WSManager) StartMockDataInjector() {
	if !w.IsMockMode {
		return
	}

	w.wg.Add(1)
	go func() {
		defer w.wg.Done()
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()

		for {
			select {
			case <-w.stopChan:
				return
			case now := <-ticker.C:
				p := 98000 + rand.Float64()*1000
				w.client.UpdateFromTrade(p, 0.5, now.UTC(), false)
				w.client.mu.Lock()
				w.client.DataSource = "MOCK"
				w.client.mu.Unlock()

				bids := [][]string{
					{fmt.Sprintf("%f", p-10), "5.5"},
					{fmt.Sprintf("%f", p-20), "10.0"},
				}
				asks := [][]string{
					{fmt.Sprintf("%f", p+10), "4.8"},
					{fmt.Sprintf("%f", p+20), "12.0"},
				}
				w.client.UpdateDepth(bids, asks, now.UTC())
			}
		}
	}()
}
