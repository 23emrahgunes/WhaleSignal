package dual40

import (
	"fmt"
	"math"
	"strings"
	"time"

	"pm-edge/internal/polymarket"
)

const (
	StateSkipped        = "SKIPPED"
	StateResting        = "RESTING"
	StateOneLeg         = "ONE_LEG"
	StateCompleted      = "COMPLETED"
	StateHedged         = "HEDGED"
	StateExpiredNoFill  = "EXPIRED_NO_FILL"
	StatePartialPair    = "PARTIAL_PAIR"
	StateDataGapInvalid = "DATA_GAP_INVALID"
)

type Trial struct {
	ID          int64   `json:"id"`
	Timeframe   string  `json:"timeframe"`
	MarketSlug  string  `json:"marketSlug"`
	EntrySecond int     `json:"entrySecond"`
	CreatedAt   string  `json:"createdAt"`
	UpdatedAt   string  `json:"updatedAt"`
	State       string  `json:"state"`
	Reason      string  `json:"reason"`
	Eligible    bool    `json:"eligible"`
	Strategy    string  `json:"strategy"`
	Regime      string  `json:"regime"`
	Metrics     Metrics `json:"metrics"`

	EntryPrice float64 `json:"entryPrice"`
	Shares     float64 `json:"shares"`

	UpTokenID   string `json:"upTokenId"`
	DownTokenID string `json:"downTokenId"`

	UpQueueAhead    float64 `json:"upQueueAhead"`
	DownQueueAhead  float64 `json:"downQueueAhead"`
	UpMakerFilled   float64 `json:"upMakerFilled"`
	DownMakerFilled float64 `json:"downMakerFilled"`
	UpMakerCost     float64 `json:"upMakerCost"`
	DownMakerCost   float64 `json:"downMakerCost"`

	FirstLeg    string `json:"firstLeg"`
	FirstFillAt string `json:"firstFillAt"`

	// Ilk bacak DOLDUGU ANIN mikroyapisi (koşullu completion analizi icin):
	// dolan ilk bacak gurultuden mi (mean-revert -> ikinci dolar) yoksa yonlu
	// akistan mi (trend -> dolmaz) ayrimini besler.
	FirstFillFlow      float64 `json:"firstFillFlow"`
	FirstFillDriftBps  float64 `json:"firstFillDriftBps"`
	FirstFillRegime    string  `json:"firstFillRegime"`
	FirstFillChopScore float64 `json:"firstFillChopScore"`
	FirstFillSecond    float64 `json:"firstFillSecond"`

	HedgeSide         string  `json:"hedgeSide"`
	HedgeShares       float64 `json:"hedgeShares"`
	HedgeAvgPrice     float64 `json:"hedgeAvgPrice"`
	HedgeTotalCost    float64 `json:"hedgeTotalCost"`
	HedgeAt           string  `json:"hedgeAt"`
	HedgeTriggerPrice float64 `json:"hedgeTriggerPrice"`

	LockedPnL float64 `json:"lockedPnl"`
	PaperPnL  float64 `json:"paperPnl"`

	LastTradeSeq   int64 `json:"lastTradeSeq"`
	StreamGapCount int64 `json:"streamGapCount"`

	LastUpBestBid   float64 `json:"lastUpBestBid"`
	LastUpBestAsk   float64 `json:"lastUpBestAsk"`
	LastDownBestBid float64 `json:"lastDownBestBid"`
	LastDownBestAsk float64 `json:"lastDownBestAsk"`
}

type HedgeRequest struct {
	Needed       bool
	Side         string
	Shares       float64
	TriggerPrice float64
	BestAsk      float64
	Reason       string
}

func NewSkippedTrial(tf, marketSlug string, entrySecond int, metrics Metrics, reason string, now time.Time) *Trial {
	now = now.UTC()
	if reason == "" {
		reason = metrics.Reason
	}
	return &Trial{
		Timeframe: tf, MarketSlug: marketSlug, EntrySecond: entrySecond,
		CreatedAt: now.Format(time.RFC3339Nano), UpdatedAt: now.Format(time.RFC3339Nano),
		State: StateSkipped, Reason: reason, Eligible: false,
		Strategy: StrategyMode, Regime: metrics.Regime, Metrics: metrics,
	}
}

func NewRestingTrial(tf, marketSlug string, entrySecond int, metrics Metrics, upTokenID, downTokenID string, upBook, downBook polymarket.BookSnapshot, cfg Config, now time.Time, lastTradeSeq, gapCount int64) (*Trial, error) {
	cfg = NormalizeConfig(cfg)
	// "hard" modda regime veto uygulanir; "feature" modda ChopScore/skew VETO
	// DEGIL (yalnizca feature) — trial mekanik kitap-gate gecince POST edilir.
	if cfg.GateMode == "hard" && !metrics.Eligible {
		return nil, fmt.Errorf("regime not eligible: %s", metrics.Reason)
	}
	if !validBook(upBook) || !validBook(downBook) {
		return nil, fmt.Errorf("invalid pair book")
	}
	if cfg.Shares+1e-9 < upBook.MinOrderSize || cfg.Shares+1e-9 < downBook.MinOrderSize {
		return nil, fmt.Errorf("min order size exceeds %.4f shares", cfg.Shares)
	}
	if cfg.EntryPrice >= upBook.BestAsk-1e-12 || cfg.EntryPrice >= downBook.BestAsk-1e-12 {
		return nil, fmt.Errorf("0.40 order would not be post-only")
	}
	now = now.UTC()
	return &Trial{
		Timeframe: tf, MarketSlug: marketSlug, EntrySecond: entrySecond,
		CreatedAt: now.Format(time.RFC3339Nano), UpdatedAt: now.Format(time.RFC3339Nano),
		State: StateResting, Reason: "DUAL_40_POSTED_SHADOW", Eligible: true,
		Strategy: StrategyMode, Regime: metrics.Regime, Metrics: metrics,
		EntryPrice: cfg.EntryPrice, Shares: cfg.Shares,
		UpTokenID: upTokenID, DownTokenID: downTokenID,
		UpQueueAhead: buyQueueAhead(upBook, cfg.EntryPrice), DownQueueAhead: buyQueueAhead(downBook, cfg.EntryPrice),
		LastTradeSeq: lastTradeSeq, StreamGapCount: gapCount,
		LastUpBestBid: upBook.BestBid, LastUpBestAsk: upBook.BestAsk,
		LastDownBestBid: downBook.BestBid, LastDownBestAsk: downBook.BestAsk,
	}, nil
}

func (t *Trial) IsOpen() bool {
	return t != nil && (t.State == StateResting || t.State == StateOneLeg)
}

func (t *Trial) IsTerminal() bool { return t != nil && !t.IsOpen() }

func Advance(t *Trial, upBook, downBook polymarket.BookSnapshot, trades []polymarket.MarketTrade, latestSeq int64, now, marketEnd time.Time, cfg Config) bool {
	if t == nil || !t.IsOpen() || !validBook(upBook) || !validBook(downBook) {
		return false
	}
	cfg = NormalizeConfig(cfg)
	now = now.UTC()
	changed := updateBook(t, upBook, downBook)

	up := makerFillFromTrades(t.UpTokenID, t.EntryPrice, t.UpMakerFilled, t.Shares, t.UpQueueAhead, trades)
	down := makerFillFromTrades(t.DownTokenID, t.EntryPrice, t.DownMakerFilled, t.Shares, t.DownQueueAhead, trades)
	t.UpQueueAhead, t.DownQueueAhead = up.QueueAhead, down.QueueAhead

	if up.Filled > 0 {
		t.UpMakerFilled += up.Filled
		if t.UpMakerFilled > t.Shares {
			t.UpMakerFilled = t.Shares
		}
		t.UpMakerCost += up.Filled * t.EntryPrice
		changed = true
	}
	if down.Filled > 0 {
		t.DownMakerFilled += down.Filled
		if t.DownMakerFilled > t.Shares {
			t.DownMakerFilled = t.Shares
		}
		t.DownMakerCost += down.Filled * t.EntryPrice
		changed = true
	}
	if t.FirstFillAt == "" && (up.Filled > 0 || down.Filled > 0) {
		upAt, downAt := eventOrNow(up.FirstAt, now), eventOrNow(down.FirstAt, now)
		switch {
		case up.Filled > 0 && down.Filled > 0 && upAt.Equal(downAt):
			t.FirstLeg = "BOTH"
			t.FirstFillAt = upAt.Format(time.RFC3339Nano)
		case up.Filled > 0 && (down.Filled <= 0 || upAt.Before(downAt)):
			t.FirstLeg = "UP"
			t.FirstFillAt = upAt.Format(time.RFC3339Nano)
		default:
			t.FirstLeg = "DOWN"
			t.FirstFillAt = downAt.Format(time.RFC3339Nano)
		}
	}
	t.LastTradeSeq = latestSeq

	if t.UpMakerFilled+1e-9 >= t.Shares && t.DownMakerFilled+1e-9 >= t.Shares {
		t.UpMakerFilled, t.DownMakerFilled = t.Shares, t.Shares
		t.State = StateCompleted
		t.Reason = "DUAL_40_BOTH_FILLED"
		t.LockedPnL = t.Shares - t.UpMakerCost - t.DownMakerCost
		t.PaperPnL = t.LockedPnL
		t.UpdatedAt = now.Format(time.RFC3339Nano)
		return true
	}

	if math.Abs(t.UpMakerFilled-t.DownMakerFilled) > 1e-9 {
		t.State = StateOneLeg
		t.Reason = "UNMATCHED_MAKER_INVENTORY"
	} else if t.UpMakerFilled > 0 {
		t.State = StateResting
		t.Reason = "PARTIAL_MATCHED_PAIR_RESTING"
	}

	created, _ := parseTime(t.CreatedAt)
	if !created.IsZero() && now.Sub(created) >= time.Duration(cfg.OrderTTLSec)*time.Second {
		matched := math.Min(t.UpMakerFilled, t.DownMakerFilled)
		unmatched := math.Abs(t.UpMakerFilled - t.DownMakerFilled)
		if unmatched <= 1e-9 {
			if matched > 0 {
				t.State = StatePartialPair
				t.Reason = "ORDER_TTL_PARTIAL_PAIR"
				t.LockedPnL = matched - matched*2*t.EntryPrice
				t.PaperPnL = t.LockedPnL
			} else {
				t.State = StateExpiredNoFill
				t.Reason = "ORDER_TTL_NO_FILL"
			}
			t.UpdatedAt = now.Format(time.RFC3339Nano)
			return true
		}
	}
	if !marketEnd.IsZero() && marketEnd.Sub(now) <= time.Duration(cfg.StopBeforeEndSec)*time.Second && t.UpMakerFilled <= 1e-9 && t.DownMakerFilled <= 1e-9 {
		t.State = StateExpiredNoFill
		t.Reason = "MARKET_END_GUARD_NO_FILL"
		t.UpdatedAt = now.Format(time.RFC3339Nano)
		return true
	}
	if changed {
		t.UpdatedAt = now.Format(time.RFC3339Nano)
	}
	return changed
}

func HedgeNeeded(t *Trial, current Metrics, upBook, downBook polymarket.BookSnapshot, now, marketEnd time.Time, cfg Config) HedgeRequest {
	if t == nil || !t.IsOpen() {
		return HedgeRequest{}
	}
	cfg = NormalizeConfig(cfg)
	unmatchedUp := math.Max(0, t.UpMakerFilled-t.DownMakerFilled)
	unmatchedDown := math.Max(0, t.DownMakerFilled-t.UpMakerFilled)
	if unmatchedUp <= 1e-9 && unmatchedDown <= 1e-9 {
		return HedgeRequest{}
	}
	req := HedgeRequest{TriggerPrice: AdaptiveHedgeTrigger(current, cfg)}
	if unmatchedUp > 0 {
		req.Side, req.Shares, req.BestAsk = "DOWN", unmatchedUp, downBook.BestAsk
	} else {
		req.Side, req.Shares, req.BestAsk = "UP", unmatchedDown, upBook.BestAsk
	}
	if req.BestAsk >= req.TriggerPrice-1e-12 {
		req.Needed = true
		req.Reason = "ADAPTIVE_HEDGE_PRICE_TRIGGER"
		return req
	}
	firstAt, _ := parseTime(t.FirstFillAt)
	if !firstAt.IsZero() && now.UTC().Sub(firstAt) >= time.Duration(cfg.HedgeMaxWaitSec)*time.Second {
		req.Needed = true
		req.Reason = "HEDGE_MAX_WAIT"
		return req
	}
	if !marketEnd.IsZero() && marketEnd.Sub(now.UTC()) <= time.Duration(cfg.StopBeforeEndSec)*time.Second {
		req.Needed = true
		req.Reason = "MARKET_END_GUARD_HEDGE"
		return req
	}
	trendThreshold := 0.60 * cfg.MaxAbsDriftBps
	if req.Side == "UP" && current.DriftBps >= trendThreshold {
		req.Needed = true
		req.Reason = "ADVERSE_UP_TREND"
		return req
	}
	if req.Side == "DOWN" && current.DriftBps <= -trendThreshold {
		req.Needed = true
		req.Reason = "ADVERSE_DOWN_TREND"
		return req
	}
	return req
}

func ApplyHedge(t *Trial, side string, quote polymarket.BuyQuote, trigger float64, reason string, now time.Time) error {
	if t == nil || !t.IsOpen() {
		return fmt.Errorf("trial not open")
	}
	if quote.Shares <= 0 || quote.TotalCost <= 0 || quote.AveragePrice <= 0 {
		return fmt.Errorf("invalid hedge quote")
	}
	side = strings.ToUpper(strings.TrimSpace(side))
	if side != "UP" && side != "DOWN" {
		return fmt.Errorf("invalid hedge side")
	}
	t.HedgeSide = side
	t.HedgeShares = quote.Shares
	t.HedgeAvgPrice = quote.AveragePrice
	t.HedgeTotalCost = quote.TotalCost
	t.HedgeTriggerPrice = trigger
	t.HedgeAt = now.UTC().Format(time.RFC3339Nano)

	upTotal, downTotal := t.UpMakerFilled, t.DownMakerFilled
	if side == "UP" {
		upTotal += quote.Shares
	} else {
		downTotal += quote.Shares
	}
	matched := math.Min(upTotal, downTotal)
	t.LockedPnL = matched - t.UpMakerCost - t.DownMakerCost - quote.TotalCost
	t.PaperPnL = t.LockedPnL
	t.State = StateHedged
	if reason == "" {
		reason = "ADAPTIVE_HEDGE_EXECUTED"
	}
	t.Reason = reason
	t.UpdatedAt = now.UTC().Format(time.RFC3339Nano)
	return nil
}

func InvalidateDataGap(t *Trial, now time.Time, reason string) bool {
	if t == nil || !t.IsOpen() {
		return false
	}
	if reason == "" {
		reason = "TRADE_STREAM_DATA_GAP"
	}
	t.State = StateDataGapInvalid
	t.Reason = reason
	t.PaperPnL = 0
	t.LockedPnL = 0
	t.UpdatedAt = now.UTC().Format(time.RFC3339Nano)
	return true
}

func CloseForMarketChange(t *Trial, now time.Time) bool {
	return InvalidateDataGap(t, now, "MARKET_CHANGED_BEFORE_RESOLUTION")
}

// RecordFirstFillContext, ilk bacak dolar dolmaz o anin mikroyapisini trial'e
// yazar. P(second fill | first fill, first-fill features) analizinin girdisi.
// Yalnizca bir kez (ilk dolumda) cagrilmalidir.
func RecordFirstFillContext(t *Trial, m Metrics, fillElapsedSec float64) {
	if t == nil {
		return
	}
	t.FirstFillFlow = m.MeanFlow
	t.FirstFillDriftBps = m.DriftBps
	t.FirstFillRegime = m.Regime
	t.FirstFillChopScore = m.ChopScore
	t.FirstFillSecond = fillElapsedSec
}

type makerFillResult struct {
	Filled     float64
	QueueAhead float64
	FirstAt    time.Time
}

func makerFillFromTrades(tokenID string, orderPrice, alreadyFilled, orderSize, queueAhead float64, trades []polymarket.MarketTrade) makerFillResult {
	remaining := math.Max(0, orderSize-alreadyFilled)
	q := math.Max(0, queueAhead)
	out := makerFillResult{QueueAhead: q}
	for _, tr := range trades {
		if remaining <= 1e-9 || tr.TokenID != tokenID || !strings.EqualFold(tr.Side, "SELL") || tr.Size <= 0 {
			continue
		}
		if tr.Price > orderPrice+1e-9 {
			continue
		}
		available := tr.Size
		if math.Abs(tr.Price-orderPrice) <= 1e-9 {
			consume := math.Min(q, available)
			q -= consume
			available -= consume
		} else {
			q = 0
			available = remaining
		}
		if available > 0 && q <= 1e-9 {
			fill := math.Min(remaining, available)
			if fill > 0 && out.FirstAt.IsZero() {
				out.FirstAt = tr.Timestamp
			}
			out.Filled += fill
			remaining -= fill
		}
	}
	out.QueueAhead = q
	return out
}

func buyQueueAhead(book polymarket.BookSnapshot, price float64) float64 {
	q := 0.0
	for _, level := range book.Bids {
		if math.Abs(level.Price-price) <= 1e-9 {
			q += level.Size
		}
	}
	return q
}

func validBook(book polymarket.BookSnapshot) bool {
	return book.TokenID != "" && book.BestBid > 0 && book.BestAsk > 0 && book.BestBid < book.BestAsk && book.MinOrderSize > 0 && book.TickSize > 0
}

func updateBook(t *Trial, upBook, downBook polymarket.BookSnapshot) bool {
	changed := t.LastUpBestBid != upBook.BestBid || t.LastUpBestAsk != upBook.BestAsk || t.LastDownBestBid != downBook.BestBid || t.LastDownBestAsk != downBook.BestAsk
	t.LastUpBestBid, t.LastUpBestAsk = upBook.BestBid, upBook.BestAsk
	t.LastDownBestBid, t.LastDownBestAsk = downBook.BestBid, downBook.BestAsk
	return changed
}

func eventOrNow(t, now time.Time) time.Time {
	now = now.UTC()
	if t.IsZero() || t.After(now.Add(time.Second)) {
		return now
	}
	return t.UTC()
}

func parseTime(v string) (time.Time, bool) {
	t, err := time.Parse(time.RFC3339Nano, v)
	if err != nil {
		return time.Time{}, false
	}
	return t, true
}
