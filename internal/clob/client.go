package clob

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"go.uber.org/zap"
)

// Client: DRY veya CANLI CLOB emir yurutucu. shadow modda hic olusturulmaz (nil).
// live bayragi CALISMA-ZAMANINDA cevrilebilir (tek-tik gecis); false=DRY (POST yok).
type Client struct {
	wallet       *Wallet
	host         string
	chainID      int
	exchangeAddr string
	apiKey       string
	apiSecret    string
	apiPass      string
	live         atomic.Bool
	http         *http.Client
	log          *zap.Logger
}

// Config: Client kurulum parametreleri (secrets — loglanmaz).
type Config struct {
	PrivateKey   string
	Host         string
	ChainID      int
	ExchangeAddr string
	APIKey       string
	APISecret    string
	APIPass      string
	DryRun       bool
}

// New: dry|live executor kurar. live'de creds zorunlu. shadow'da bu cagrilmaz.
func New(cfg Config, log *zap.Logger) (*Client, error) {
	w, err := NewWallet(cfg.PrivateKey)
	if err != nil {
		return nil, err
	}
	if !cfg.DryRun && (cfg.APIKey == "" || cfg.APISecret == "" || cfg.APIPass == "") {
		return nil, fmt.Errorf("live mod icin CLOB_API_KEY/SECRET/PASSPHRASE gerekli")
	}
	c := &Client{
		wallet: w, host: strings.TrimRight(cfg.Host, "/"), chainID: cfg.ChainID,
		exchangeAddr: cfg.ExchangeAddr, apiKey: cfg.APIKey, apiSecret: cfg.APISecret,
		apiPass: cfg.APIPass, http: &http.Client{Timeout: 10 * time.Second}, log: log,
	}
	c.live.Store(!cfg.DryRun)
	mode := "DRY"
	if !cfg.DryRun {
		mode = "LIVE"
	}
	log.Warn("CLOB EXECUTOR KURULDU", zap.String("baslangic", mode), zap.String("address", w.Address.Hex()), zap.String("exchange", cfg.ExchangeAddr))
	return c, nil
}

// SetLive: calisma-zamaninda DRY<->CANLI cevir (butondan). false=DRY (POST yok).
func (c *Client) SetLive(v bool) {
	if c == nil {
		return
	}
	c.live.Store(v)
	m := "DRY"
	if v {
		m = "LIVE"
	}
	c.log.Warn("CLOB MOD DEGISTI", zap.String("mode", m))
}

func (c *Client) IsLive() bool { return c != nil && c.live.Load() }
func (c *Client) DryRun() bool { return c == nil || !c.live.Load() }

// PlaceLimit: dinlenen (GTC) limit emir. DRY'de imzalar+loglar, POST etmez.
func (c *Client) PlaceLimit(tokenID string, side Side, size, price float64) (string, error) {
	return c.place(tokenID, side, size, price, "GTC")
}

// PlaceMarketable: marketable (FAK) emir — hedge icin karsi tarafi capraz al.
func (c *Client) PlaceMarketable(tokenID string, side Side, size, price float64) (string, error) {
	return c.place(tokenID, side, size, price, "FAK")
}

func (c *Client) place(tokenID string, side Side, size, price float64, orderType string) (string, error) {
	so, err := c.wallet.buildAndSign(c.exchangeAddr, c.chainID, tokenID, side, size, price)
	if err != nil {
		return "", err
	}
	if !c.live.Load() {
		c.log.Info("CLOB DRY: emir imzalandi (POST YOK)", zap.String("type", orderType), zap.String("side", so.Side),
			zap.String("tokenId", short(tokenID)), zap.Float64("size", size), zap.Float64("price", price),
			zap.String("makerAmount", so.MakerAmount), zap.String("takerAmount", so.TakerAmount))
		return fmt.Sprintf("dry-%s-%d", strings.ToLower(so.Side), time.Now().UnixNano()), nil
	}
	body, _ := json.Marshal(map[string]any{"order": so, "owner": c.apiKey, "orderType": orderType})
	resp, err := c.doL2("POST", "/order", body)
	if err != nil {
		return "", err
	}
	var r struct {
		Success bool   `json:"success"`
		OrderID string `json:"orderID"`
		ErrMsg  string `json:"errorMsg"`
	}
	_ = json.Unmarshal(resp, &r)
	if !r.Success && r.OrderID == "" {
		return "", fmt.Errorf("CLOB emir reddedildi: %s", strings.TrimSpace(r.ErrMsg+" "+string(resp)))
	}
	c.log.Info("CLOB CANLI EMIR", zap.String("orderId", r.OrderID), zap.String("type", orderType), zap.String("side", so.Side), zap.Float64("price", price), zap.Float64("size", size))
	return r.OrderID, nil
}

// Cancel: acik emri iptal eder. DRY'de no-op (dry- id).
func (c *Client) Cancel(orderID string) error {
	if !c.live.Load() || strings.HasPrefix(orderID, "dry-") {
		c.log.Info("CLOB DRY: iptal (no-op)", zap.String("orderId", orderID))
		return nil
	}
	body, _ := json.Marshal(map[string]string{"orderID": orderID})
	_, err := c.doL2("DELETE", "/order", body)
	if err != nil {
		return err
	}
	c.log.Info("CLOB CANLI IPTAL", zap.String("orderId", orderID))
	return nil
}

// GetFilledShares: emrin dolan hisse miktari. DRY'de 0 (dolum simulasyonu ust katmanda).
func (c *Client) GetFilledShares(orderID string) (float64, error) {
	if !c.live.Load() || strings.HasPrefix(orderID, "dry-") {
		return 0, nil
	}
	resp, err := c.doL2("GET", "/data/order/"+orderID, nil)
	if err != nil {
		return 0, err
	}
	var r struct {
		SizeMatched any `json:"size_matched"`
	}
	_ = json.Unmarshal(resp, &r)
	switch v := r.SizeMatched.(type) {
	case float64:
		return v, nil
	case string:
		f, _ := strconv.ParseFloat(v, 64)
		return f, nil
	}
	return 0, nil
}

// doL2: L2 HMAC-imzali kimlikli REST istegi (py-clob-client build_hmac_signature ile ayni).
func (c *Client) doL2(method, path string, body []byte) ([]byte, error) {
	ts := strconv.FormatInt(time.Now().Unix(), 10)
	sig, err := c.hmacSig(ts, method, path, body)
	if err != nil {
		return nil, err
	}
	var rdr io.Reader
	if body != nil {
		rdr = bytes.NewReader(body)
	}
	req, err := http.NewRequest(method, c.host+path, rdr)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("POLY_ADDRESS", c.wallet.Address.Hex())
	req.Header.Set("POLY_SIGNATURE", sig)
	req.Header.Set("POLY_TIMESTAMP", ts)
	req.Header.Set("POLY_API_KEY", c.apiKey)
	req.Header.Set("POLY_PASSPHRASE", c.apiPass)
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	out, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 300 {
		return out, fmt.Errorf("CLOB %s %s -> %d: %s", method, path, resp.StatusCode, strings.TrimSpace(string(out)))
	}
	return out, nil
}

// hmacSig: base64url(HMAC-SHA256(secret, ts+method+path+body)). secret base64url'dir.
func (c *Client) hmacSig(ts, method, path string, body []byte) (string, error) {
	secret, err := base64.URLEncoding.DecodeString(c.apiSecret)
	if err != nil {
		return "", fmt.Errorf("CLOB_API_SECRET base64url degil: %w", err)
	}
	msg := ts + method + path
	if body != nil {
		msg += string(body)
	}
	mac := hmac.New(sha256.New, secret)
	mac.Write([]byte(msg))
	return base64.URLEncoding.EncodeToString(mac.Sum(nil)), nil
}

func short(s string) string {
	if len(s) <= 12 {
		return s
	}
	return s[:6] + ".." + s[len(s)-4:]
}
