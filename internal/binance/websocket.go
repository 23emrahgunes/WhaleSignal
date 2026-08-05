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
	client       *Client
	stopChan     chan struct{}
	wg           sync.WaitGroup
	wsConn       *websocket.Conn
	mu           sync.Mutex
	reconnecting bool
}

func NewWSManager(client *Client) *WSManager {
	return &WSManager{
		client:   client,
		stopChan: make(chan struct{}),
	}
}

func (w *WSManager) Start() {
	w.wg.Add(1)
	go w.run()
}

func (w *WSManager) Stop() {
	close(w.stopChan)
	w.mu.Lock()
	if w.wsConn != nil {
		_ = w.wsConn.Close()
	}
	w.mu.Unlock()
	w.wg.Wait()
}

func (w *WSManager) run() {
	defer w.wg.Done()

	// Connect to Combined Streams (Trade + Depth)
	// URL format: wss://stream.binance.com:9443/ws/btcusdt@trade/btcusdt@depth20@100ms
	url := "wss://stream.binance.com:9443/ws/btcusdt@trade/btcusdt@depth20@100ms"

	backoff := 1 * time.Second

	for {
		select {
		case <-w.stopChan:
			return
		default:
		}

		util.Logger.Info("Connecting to Binance WS streams...", zap.String("url", url))
		conn, _, err := websocket.DefaultDialer.Dial(url, nil)
		if err != nil {
			util.Logger.Error("Binance WS Connection failed, retrying...", zap.Error(err), zap.Duration("backoff", backoff))
			w.client.WSFallback = true
			w.client.IsWsConnected = false
			w.sleepWithContext(backoff)
			backoff = time.Duration(float64(backoff) * 1.5)
			if backoff > 60*time.Second {
				backoff = 60 * time.Second
			}
			continue
		}

		backoff = 1 * time.Second // reset backoff
		w.mu.Lock()
		w.wsConn = conn
		w.mu.Unlock()

		w.client.IsWsConnected = true
		w.client.WSFallback = false
		util.Logger.Info("Connected to Binance WebSocket stream")

		err = w.readLoop(conn)
		if err != nil {
			util.Logger.Error("WebSocket connection lost", zap.Error(err))
		}

		w.client.IsWsConnected = false
		w.client.WSFallback = true
		w.sleepWithContext(1 * time.Second)
	}
}

func (w *WSManager) sleepWithContext(d time.Duration) {
	select {
	case <-w.stopChan:
	case <-time.After(d):
	}
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

		if payload.Stream == "btcusdt@trade" {
			var ev TradeEvent
			if err := json.Unmarshal(payload.Data, &ev); err == nil {
				price, _ := strconv.ParseFloat(ev.Price, 64)
				size, _ := strconv.ParseFloat(ev.Quantity, 64)
				t := time.UnixMilli(ev.EventTime).UTC()
				w.client.UpdateFromTrade(price, size, t)
			}
		} else if payload.Stream == "btcusdt@depth20@100ms" {
			var ev DepthEvent
			if err := json.Unmarshal(payload.Data, &ev); err == nil {
				w.client.UpdateDepth(ev.Bids, ev.Asks, time.Now().UTC())
			}
		}
	}
}

// StartFallbackRESTPoller starts polling REST ticker in the background if WebSocket fails.
func (w *WSManager) StartFallbackRESTPoller() {
	w.wg.Add(1)
	go func() {
		defer w.wg.Done()
		ticker := time.NewTicker(1 * time.Second)
		defer ticker.Stop()

		for {
			select {
			case <-w.stopChan:
				return
			case <-ticker.C:
				if w.client.WSFallback {
					price, err := w.client.FetchTickerPriceREST()
					if err == nil {
						// Update client current price using artificial trade update
						w.client.UpdateFromTrade(price, 0.0, time.Now().UTC())
					} else {
						util.Logger.Warn("Binance fallback REST poller failed", zap.Error(err))
					}
				}
			}
		}
	}()
}

// MockDataInjector populates mock Binance and Polymarket values to keep calculations active for headless envs.
func (w *WSManager) StartMockDataInjector() {
	w.wg.Add(1)
	go func() {
		defer w.wg.Done()
		ticker := time.NewTicker(1 * time.Second)
		defer ticker.Stop()

		for {
			select {
			case <-w.stopChan:
				return
			case <-ticker.C:
				if !w.client.IsWsConnected && w.client.GetPrice() == 0 {
					// Inject synthetic price changes
					p := 98000.0 + rand.Float64()*1000.0
					w.client.UpdateFromTrade(p, 0.5, time.Now().UTC())
					bids := [][]string{
						{fmt.Sprintf("%f", p-10), "5.5"},
						{fmt.Sprintf("%f", p-20), "10.0"},
					}
					asks := [][]string{
						{fmt.Sprintf("%f", p+10), "4.8"},
						{fmt.Sprintf("%f", p+20), "12.0"},
					}
					w.client.UpdateDepth(bids, asks, time.Now().UTC())
				}
			}
		}
	}()
}
