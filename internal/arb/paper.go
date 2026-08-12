package arb

import (
	"math"
	"strings"
	"time"

	"pm-edge/internal/polymarket"
)

const (
	PaperStatusRestingFirst      = "RESTING_FIRST"
	PaperStatusFirstPartial      = "FIRST_PARTIAL"
	PaperStatusCompleting        = "COMPLETING"
	PaperStatusCompletionPartial = "COMPLETION_PARTIAL"
	PaperStatusCompleted         = "COMPLETED"
	PaperStatusExpiredNoFill     = "EXPIRED_NO_FILL"
	PaperStatusStrandedTimeout   = "STRANDED_TIMEOUT"
	PaperStatusDataGapInvalid    = "DATA_GAP_INVALID"

	// Legacy statuses are recognized so an older open shadow row cannot wedge
	// startup after an upgrade.
	PaperStatusRestingPair  = "RESTING_PAIR"
	PaperStatusOneLegFilled = "ONE_LEG_FILLED"
)

type PaperConfig struct {
	Enabled        bool
	OrderTTL       time.Duration
	SoftCompletion time.Duration
	MaxStranded    time.Duration
	StopBeforeEnd  time.Duration
}

type PaperCycle struct {
	ID                    int64   `json:"id"`
	Timeframe             string  `json:"timeframe"`
	MarketSlug            string  `json:"marketSlug"`
	CreatedAt             string  `json:"createdAt"`
	UpdatedAt             string  `json:"updatedAt"`
	Status                string  `json:"status"`
	Reason                string  `json:"reason"`
	FillModel             string  `json:"fillModel"`
	OrderMode             string  `json:"orderMode"`
	StrategyMode          string  `json:"strategyMode"`
	OrderSize             float64 `json:"orderSize"`
	PreferredFirstLeg     string  `json:"preferredFirstLeg"`
	ActualFirstLeg        string  `json:"actualFirstLeg"`
	PreferredFirstMatched bool    `json:"preferredFirstMatched"`

	UpTokenID   string `json:"upTokenId"`
	DownTokenID string `json:"downTokenId"`

	UpOrderPrice      float64 `json:"upOrderPrice"`
	DownOrderPrice    float64 `json:"downOrderPrice"`
	UpFillPrice       float64 `json:"upFillPrice"`
	DownFillPrice     float64 `json:"downFillPrice"`
	UpFilledShares    float64 `json:"upFilledShares"`
	DownFilledShares  float64 `json:"downFilledShares"`
	UpFillNotional    float64 `json:"upFillNotional"`
	DownFillNotional  float64 `json:"downFillNotional"`
	UpFilledAt        string  `json:"upFilledAt"`
	DownFilledAt      string  `json:"downFilledAt"`
	DownCompletionMax float64 `json:"downCompletionMax"`
	UpCompletionMax   float64 `json:"upCompletionMax"`
	Reprices          int     `json:"reprices"`

	FirstOrderSide     string  `json:"firstOrderSide"`
	FirstOrderPrice    float64 `json:"firstOrderPrice"`
	FirstFilledShares  float64 `json:"firstFilledShares"`
	FirstQueueAhead    float64 `json:"firstQueueAhead"`
	SecondOrderSide    string  `json:"secondOrderSide"`
	SecondOrderPrice   float64 `json:"secondOrderPrice"`
	SecondFilledShares float64 `json:"secondFilledShares"`
	SecondQueueAhead   float64 `json:"secondQueueAhead"`
	FirstPartialAt     string  `json:"firstPartialAt"`
	FirstFullAt        string  `json:"firstFullAt"`
	CompletionPostedAt string  `json:"completionPostedAt"`
	LastTradeSeq       int64   `json:"lastTradeSeq"`
	StreamGapCount     int64   `json:"streamGapCount"`

	EntryPTBPUp       float64 `json:"entryPtbPUp"`
	EntryPTBPDown     float64 `json:"entryPtbPDown"`
	EntryPTBDecision  string  `json:"entryPtbDecision"`
	EntryNetEdge      float64 `json:"entryNetEdge"`
	TargetEdge        float64 `json:"targetEdge"`
	PaperMinEdge      float64 `json:"paperMinEdge"`
	OperationalBuffer float64 `json:"operationalBuffer"`

	FirstFillAt      string  `json:"firstFillAt"`
	FirstFillMs      int64   `json:"firstFillMs"`
	StrandedSeconds  float64 `json:"strandedSeconds"`
	CompletionMs     int64   `json:"completionMs"`
	ExitMarkPrice    float64 `json:"exitMarkPrice"`
	LockedPnL        float64 `json:"lockedPnl"`
	PaperPnL         float64 `json:"paperPnl"`
	DeployedCost     float64 `json:"deployedCost"`
	ReservedPairCost float64 `json:"reservedPairCost"`

	LastUpBestBid   float64 `json:"lastUpBestBid"`
	LastUpBestAsk   float64 `json:"lastUpBestAsk"`
	LastDownBestBid float64 `json:"lastDownBestBid"`
	LastDownBestAsk float64 `json:"lastDownBestAsk"`
}

func DefaultPaperConfig() PaperConfig {
	return PaperConfig{Enabled: true, OrderTTL: 12 * time.Second, SoftCompletion: 2 * time.Second, MaxStranded: 5 * time.Second, StopBeforeEnd: 12 * time.Second}
}

func NewPaperCycle(s *Snapshot, upBook, downBook polymarket.BookSnapshot, now time.Time, lastTradeSeq, streamGapCount int64) *PaperCycle {
	if s == nil || !s.PaperEdgePass || s.OrderSize <= 0 || s.UpMakerPrice <= 0 || s.DownMakerPrice <= 0 || !validBook(upBook) || !validBook(downBook) {
		return nil
	}
	now = now.UTC()
	c := &PaperCycle{
		Timeframe: s.Timeframe, MarketSlug: s.MarketSlug,
		CreatedAt: now.Format(time.RFC3339Nano), UpdatedAt: now.Format(time.RFC3339Nano),
		Status: PaperStatusRestingFirst, Reason: "SAFE_FIRST_POSTED_SHADOW",
		FillModel: "WS_SELL_TRADES_PRICE_TIME_QUEUE_PARTIAL", OrderMode: "GTC_GTD_POST_ONLY", StrategyMode: s.StrategyMode,
		OrderSize: s.OrderSize, PreferredFirstLeg: s.FirstLeg,
		UpTokenID: s.UpTokenID, DownTokenID: s.DownTokenID,
		UpOrderPrice: s.UpMakerPrice, DownOrderPrice: s.DownMakerPrice,
		DownCompletionMax: s.DownCompletionMax, UpCompletionMax: s.UpCompletionMax,
		EntryPTBPUp: s.PTBPUp, EntryPTBPDown: s.PTBPDown, EntryPTBDecision: s.PTBDecision,
		EntryNetEdge: s.NetEdge, TargetEdge: s.TargetEdge, PaperMinEdge: s.PaperMinEdge, OperationalBuffer: s.OperationalBuffer,
		ReservedPairCost: s.OrderSize * (s.UpMakerPrice + s.DownMakerPrice),
		LastUpBestBid:    upBook.BestBid, LastUpBestAsk: upBook.BestAsk,
		LastDownBestBid: downBook.BestBid, LastDownBestAsk: downBook.BestAsk,
		LastTradeSeq: lastTradeSeq, StreamGapCount: streamGapCount,
	}
	if strings.EqualFold(s.FirstLeg, "DOWN") {
		c.FirstOrderSide, c.FirstOrderPrice = "DOWN", s.DownMakerPrice
		c.SecondOrderSide, c.SecondOrderPrice = "UP", s.UpMakerPrice
		c.FirstQueueAhead = buyQueueAhead(downBook, c.FirstOrderPrice)
	} else {
		c.FirstOrderSide, c.FirstOrderPrice = "UP", s.UpMakerPrice
		c.SecondOrderSide, c.SecondOrderPrice = "DOWN", s.DownMakerPrice
		c.FirstQueueAhead = buyQueueAhead(upBook, c.FirstOrderPrice)
	}
	return c
}

func (c *PaperCycle) IsOpen() bool {
	if c == nil {
		return false
	}
	switch c.Status {
	case PaperStatusRestingFirst, PaperStatusFirstPartial, PaperStatusCompleting, PaperStatusCompletionPartial, PaperStatusRestingPair, PaperStatusOneLegFilled:
		return true
	default:
		return false
	}
}

func (c *PaperCycle) IsTerminal() bool { return c != nil && !c.IsOpen() }

func AdvancePaperCycle(c *PaperCycle, upBook, downBook polymarket.BookSnapshot, trades []polymarket.MarketTrade, latestSeq int64, now, marketEnd time.Time, cfg PaperConfig) bool {
	if c == nil || !c.IsOpen() || !validBook(upBook) || !validBook(downBook) {
		return false
	}
	if cfg.OrderTTL <= 0 {
		cfg.OrderTTL = 12 * time.Second
	}
	if cfg.SoftCompletion <= 0 {
		cfg.SoftCompletion = 2 * time.Second
	}
	if cfg.MaxStranded <= 0 {
		cfg.MaxStranded = 5 * time.Second
	}
	if cfg.StopBeforeEnd <= 0 {
		cfg.StopBeforeEnd = 12 * time.Second
	}
	now = now.UTC()
	changed := updateLastBook(c, upBook, downBook)

	// Old executor rows are not replay-compatible with the queue model.
	if c.Status == PaperStatusRestingPair || c.Status == PaperStatusOneLegFilled {
		c.Status = PaperStatusDataGapInvalid
		c.Reason = "LEGACY_FILL_MODEL_INVALIDATED"
		c.PaperPnL = 0
		c.UpdatedAt = now.Format(time.RFC3339Nano)
		return true
	}

	if c.Status == PaperStatusRestingFirst || c.Status == PaperStatusFirstPartial {
		fill := makerBuyFillFromTradesDetailed(tokenForSide(c.FirstOrderSide, c), c.FirstOrderPrice, c.FirstFilledShares, c.OrderSize, c.FirstQueueAhead, trades)
		c.FirstQueueAhead = fill.QueueAhead
		if fill.Filled > 0 {
			fillAt := eventOrNow(fill.LastAt, now)
			addFill(c, c.FirstOrderSide, fill.Filled, c.FirstOrderPrice, fillAt)
			c.FirstFilledShares += fill.Filled
			if c.FirstPartialAt == "" {
				firstAt := eventOrNow(fill.FirstAt, now)
				c.FirstPartialAt = firstAt.Format(time.RFC3339Nano)
				c.FirstFillAt = c.FirstPartialAt
				if created, ok := parseTime(c.CreatedAt); ok {
					c.FirstFillMs = maxInt64(0, firstAt.Sub(created).Milliseconds())
				}
				c.ActualFirstLeg = c.FirstOrderSide
				c.PreferredFirstMatched = strings.EqualFold(c.PreferredFirstLeg, c.ActualFirstLeg)
			}
			changed = true
		}
		if c.FirstFilledShares+1e-9 >= c.OrderSize {
			c.FirstFilledShares = c.OrderSize
			fullAt := eventOrNow(fill.LastAt, now)
			c.FirstFullAt = fullAt.Format(time.RFC3339Nano)
			c.Status = PaperStatusCompleting
			// A real completion order can only be posted after our process observes
			// the first-leg fill, so the completion clock starts at detection time.
			c.CompletionPostedAt = now.Format(time.RFC3339Nano)
			secondBook := bookForSide(c.SecondOrderSide, upBook, downBook)
			c.SecondOrderPrice = 0
			if activateCompletion(c, secondBook) {
				c.Reason = "FIRST_LEG_FULL_COMPLETION_POSTED"
			} else {
				c.Reason = "COMPLETION_WAITING_POST_ONLY_PRICE"
			}
			c.LastTradeSeq = latestSeq
			c.UpdatedAt = now.Format(time.RFC3339Nano)
			// The completion order did not exist during the trade batch that filled
			// the first leg. Start evaluating it only from the next batch.
			return true
		} else if c.FirstFilledShares > 0 {
			c.Status = PaperStatusFirstPartial
			c.Reason = "FIRST_LEG_PARTIAL"
		}
		c.LastTradeSeq = latestSeq

		if c.Status == PaperStatusRestingFirst {
			created, ok := parseTime(c.CreatedAt)
			if ok && now.Sub(created) >= cfg.OrderTTL {
				c.Status = PaperStatusExpiredNoFill
				c.Reason = "SAFE_FIRST_ORDER_TTL_EXPIRED"
				c.UpdatedAt = now.Format(time.RFC3339Nano)
				return true
			}
			if !marketEnd.IsZero() && marketEnd.Sub(now) <= cfg.StopBeforeEnd {
				c.Status = PaperStatusExpiredNoFill
				c.Reason = "TOO_CLOSE_TO_MARKET_END_NO_FILL"
				c.UpdatedAt = now.Format(time.RFC3339Nano)
				return true
			}
		}
		if c.Status == PaperStatusFirstPartial && strandedExpired(c, now, marketEnd, cfg) {
			timeoutStranded(c, upBook, downBook, now)
			return true
		}
		if c.Status != PaperStatusCompleting {
			if changed {
				c.UpdatedAt = now.Format(time.RFC3339Nano)
			}
			return changed
		}
	}

	if c.Status == PaperStatusCompleting || c.Status == PaperStatusCompletionPartial {
		secondBook := bookForSide(c.SecondOrderSide, upBook, downBook)
		if c.SecondOrderPrice <= 0 {
			if !activateCompletion(c, secondBook) {
				if strandedExpired(c, now, marketEnd, cfg) {
					timeoutStranded(c, upBook, downBook, now)
					return true
				}
				c.Reason = "COMPLETION_WAITING_POST_ONLY_PRICE"
				c.LastTradeSeq = latestSeq
				c.UpdatedAt = now.Format(time.RFC3339Nano)
				return true
			}
			c.Reason = "COMPLETION_POSTED_FROM_CURRENT_BOOK"
			c.LastTradeSeq = latestSeq
			c.UpdatedAt = now.Format(time.RFC3339Nano)
			// It was not resting during this batch; start fill accounting next batch.
			return true
		}
		fill := makerBuyFillFromTradesDetailed(tokenForSide(c.SecondOrderSide, c), c.SecondOrderPrice, c.SecondFilledShares, c.OrderSize, c.SecondQueueAhead, trades)
		c.SecondQueueAhead = fill.QueueAhead
		if fill.Filled > 0 {
			fillAt := eventOrNow(fill.LastAt, now)
			addFill(c, c.SecondOrderSide, fill.Filled, c.SecondOrderPrice, fillAt)
			c.SecondFilledShares += fill.Filled
			changed = true
		}
		c.LastTradeSeq = latestSeq
		if c.SecondFilledShares+1e-9 >= c.OrderSize {
			c.SecondFilledShares = c.OrderSize
			postedAt, _ := parseTime(c.CompletionPostedAt)
			fillAt := eventOrNow(fill.LastAt, now)
			if !postedAt.IsZero() {
				c.CompletionMs = maxInt64(0, fillAt.Sub(postedAt).Milliseconds())
			}
			completeCycle(c, now)
			return true
		}
		if c.SecondFilledShares > 0 {
			c.Status = PaperStatusCompletionPartial
			c.Reason = "COMPLETION_PARTIAL"
		}
		if strandedExpired(c, now, marketEnd, cfg) {
			timeoutStranded(c, upBook, downBook, now)
			return true
		}

		ceiling := c.DownCompletionMax
		if strings.EqualFold(c.SecondOrderSide, "UP") {
			ceiling = c.UpCompletionMax
		}
		urgent := false
		if postedAt, ok := parseTime(c.CompletionPostedAt); ok && now.Sub(postedAt) >= cfg.SoftCompletion {
			urgent = true
		}
		p, ok := completionRepriceWithUrgency(c.SecondOrderPrice, ceiling, secondBook, urgent)
		if ok && p > c.SecondOrderPrice+1e-12 {
			c.SecondOrderPrice = p
			if strings.EqualFold(c.SecondOrderSide, "UP") {
				c.UpOrderPrice = p
			} else {
				c.DownOrderPrice = p
			}
			c.SecondQueueAhead = buyQueueAhead(secondBook, p)
			c.Reprices++
			changed = true
		}
	}

	if firstAt, ok := parseTime(c.FirstPartialAt); ok {
		c.StrandedSeconds = math.Max(0, now.Sub(firstAt).Seconds())
	}
	if changed {
		c.UpdatedAt = now.Format(time.RFC3339Nano)
	}
	return changed
}

func InvalidatePaperCycleDataGap(c *PaperCycle, now time.Time) bool {
	if c == nil || !c.IsOpen() {
		return false
	}
	c.Status = PaperStatusDataGapInvalid
	c.Reason = "MARKET_TRADE_STREAM_GAP"
	c.PaperPnL = 0
	c.LockedPnL = 0
	c.UpdatedAt = now.UTC().Format(time.RFC3339Nano)
	return true
}

func ClosePaperCycleForMarketChange(c *PaperCycle, now time.Time) bool {
	if c == nil || !c.IsOpen() {
		return false
	}
	now = now.UTC()
	if c.UpFilledShares <= 0 && c.DownFilledShares <= 0 {
		c.Status = PaperStatusExpiredNoFill
		c.Reason = "MARKET_CHANGED_NO_FILL"
		c.UpdatedAt = now.Format(time.RFC3339Nano)
		return true
	}
	markPaperPnLWithPrices(c, c.LastUpBestBid, c.LastDownBestBid)
	c.Status = PaperStatusStrandedTimeout
	c.Reason = "MARKET_CHANGED_MARK_TO_LAST_BID"
	c.UpdatedAt = now.Format(time.RFC3339Nano)
	return true
}

type makerFillResult struct {
	Filled     float64
	QueueAhead float64
	FirstAt    time.Time
	LastAt     time.Time
}

func makerBuyFillFromTrades(tokenID string, orderPrice, alreadyFilled, orderSize, queueAhead float64, trades []polymarket.MarketTrade) (float64, float64) {
	r := makerBuyFillFromTradesDetailed(tokenID, orderPrice, alreadyFilled, orderSize, queueAhead, trades)
	return r.Filled, r.QueueAhead
}

func makerBuyFillFromTradesDetailed(tokenID string, orderPrice, alreadyFilled, orderSize, queueAhead float64, trades []polymarket.MarketTrade) makerFillResult {
	remaining := math.Max(0, orderSize-alreadyFilled)
	filled := 0.0
	q := math.Max(0, queueAhead)
	var firstAt, lastAt time.Time
	for _, tr := range trades {
		if remaining <= 1e-9 || tr.TokenID != tokenID || !strings.EqualFold(tr.Side, "SELL") || tr.Size <= 0 {
			continue
		}
		if tr.Price > orderPrice+1e-9 {
			// This execution occurred at a better bid. It says nothing about the
			// FIFO volume already ahead of us at our own price.
			continue
		}
		available := tr.Size
		if math.Abs(tr.Price-orderPrice) <= 1e-9 {
			consume := math.Min(q, available)
			q -= consume
			available -= consume
		} else if tr.Price < orderPrice-1e-9 {
			// A lower SELL print cannot occur while our higher resting BUY is
			// still unfilled. The sweep necessarily consumed our full remainder.
			q = 0
			if remaining > 0 {
				if firstAt.IsZero() {
					firstAt = tr.Timestamp
				}
				lastAt = tr.Timestamp
			}
			filled += remaining
			remaining = 0
			continue
		}
		if available > 0 && q <= 1e-9 {
			f := math.Min(remaining, available)
			if f > 0 {
				if firstAt.IsZero() {
					firstAt = tr.Timestamp
				}
				lastAt = tr.Timestamp
			}
			filled += f
			remaining -= f
		}
	}
	return makerFillResult{Filled: filled, QueueAhead: q, FirstAt: firstAt, LastAt: lastAt}
}

func addFill(c *PaperCycle, side string, qty, price float64, now time.Time) {
	if qty <= 0 {
		return
	}
	ts := now.UTC().Format(time.RFC3339Nano)
	if strings.EqualFold(side, "UP") {
		c.UpFilledShares += qty
		c.UpFillNotional += qty * price
		c.UpFillPrice = c.UpFillNotional / c.UpFilledShares
		c.UpFilledAt = ts
	} else {
		c.DownFilledShares += qty
		c.DownFillNotional += qty * price
		c.DownFillPrice = c.DownFillNotional / c.DownFilledShares
		c.DownFilledAt = ts
	}
	c.DeployedCost += qty * price
}

func completeCycle(c *PaperCycle, now time.Time) {
	c.Status = PaperStatusCompleted
	c.Reason = "PAIR_COMPLETED_LOCKED"
	matched := math.Min(c.UpFilledShares, c.DownFilledShares)
	c.LockedPnL = matched * (1 - c.UpFillPrice - c.DownFillPrice)
	c.PaperPnL = c.LockedPnL
	if firstAt, ok := parseTime(c.FirstPartialAt); ok {
		c.StrandedSeconds = now.UTC().Sub(firstAt).Seconds()
	}
	c.UpdatedAt = now.UTC().Format(time.RFC3339Nano)
}

func timeoutStranded(c *PaperCycle, upBook, downBook polymarket.BookSnapshot, now time.Time) {
	upExit, upAvg := bidProceeds(upBook, math.Max(0, c.UpFilledShares-math.Min(c.UpFilledShares, c.DownFilledShares)))
	downExit, downAvg := bidProceeds(downBook, math.Max(0, c.DownFilledShares-math.Min(c.UpFilledShares, c.DownFilledShares)))
	matched := math.Min(c.UpFilledShares, c.DownFilledShares)
	c.LockedPnL = matched * (1 - c.UpFillPrice - c.DownFillPrice)
	unmatchedUp := math.Max(0, c.UpFilledShares-matched)
	unmatchedDown := math.Max(0, c.DownFilledShares-matched)
	c.PaperPnL = c.LockedPnL + upExit - unmatchedUp*c.UpFillPrice + downExit - unmatchedDown*c.DownFillPrice
	if unmatchedUp > 0 {
		c.ExitMarkPrice = upAvg
	} else if unmatchedDown > 0 {
		c.ExitMarkPrice = downAvg
	}
	c.Status = PaperStatusStrandedTimeout
	c.Reason = "STRANDED_TIMEOUT_MARK_TO_BID_VWAP"
	if firstAt, ok := parseTime(c.FirstPartialAt); ok {
		c.StrandedSeconds = now.UTC().Sub(firstAt).Seconds()
	}
	c.UpdatedAt = now.UTC().Format(time.RFC3339Nano)
}

func markPaperPnLWithPrices(c *PaperCycle, upBid, downBid float64) {
	matched := math.Min(c.UpFilledShares, c.DownFilledShares)
	c.LockedPnL = matched * (1 - c.UpFillPrice - c.DownFillPrice)
	unmatchedUp := math.Max(0, c.UpFilledShares-matched)
	unmatchedDown := math.Max(0, c.DownFilledShares-matched)
	c.PaperPnL = c.LockedPnL + unmatchedUp*(upBid-c.UpFillPrice) + unmatchedDown*(downBid-c.DownFillPrice)
	if unmatchedUp > 0 {
		c.ExitMarkPrice = upBid
	} else if unmatchedDown > 0 {
		c.ExitMarkPrice = downBid
	}
}

func bidProceeds(book polymarket.BookSnapshot, shares float64) (float64, float64) {
	if shares <= 0 {
		return 0, 0
	}
	remaining := shares
	proceeds := 0.0
	filled := 0.0
	for _, level := range book.Bids {
		if remaining <= 1e-9 {
			break
		}
		q := math.Min(remaining, level.Size)
		if q <= 0 {
			continue
		}
		proceeds += q * level.Price
		filled += q
		remaining -= q
	}
	// Missing bid depth is conservatively valued at zero.
	avg := 0.0
	if shares > 0 {
		avg = proceeds / shares
	}
	_ = filled
	return proceeds, avg
}

func strandedExpired(c *PaperCycle, now, marketEnd time.Time, cfg PaperConfig) bool {
	firstAt, ok := parseTime(c.FirstPartialAt)
	if !ok {
		return false
	}
	if now.Sub(firstAt) >= cfg.MaxStranded {
		return true
	}
	return !marketEnd.IsZero() && marketEnd.Sub(now) <= cfg.StopBeforeEnd
}

func activateCompletion(c *PaperCycle, book polymarket.BookSnapshot) bool {
	if c == nil || !validBook(book) {
		return false
	}
	ceiling := c.DownCompletionMax
	if strings.EqualFold(c.SecondOrderSide, "UP") {
		ceiling = c.UpCompletionMax
	}
	if ceiling <= 0 {
		return false
	}
	postOnlyCeiling := floorToTick(book.BestAsk-book.TickSize, book.TickSize)
	if postOnlyCeiling <= 0 {
		return false
	}
	price, ok := MakerBuyPrice(book, true)
	if !ok {
		price, ok = MakerBuyPrice(book, false)
	}
	if !ok || price > ceiling+1e-12 {
		price = floorToTick(math.Min(ceiling, postOnlyCeiling), book.TickSize)
	}
	if price <= 0 || price >= book.BestAsk-1e-12 || price > ceiling+1e-12 {
		return false
	}
	c.SecondOrderPrice = price
	if strings.EqualFold(c.SecondOrderSide, "UP") {
		c.UpOrderPrice = price
	} else {
		c.DownOrderPrice = price
	}
	c.SecondQueueAhead = buyQueueAhead(book, price)
	return true
}

func completionReprice(current, economicCeiling float64, book polymarket.BookSnapshot) (float64, bool) {
	return completionRepriceWithUrgency(current, economicCeiling, book, false)
}

func completionRepriceWithUrgency(current, economicCeiling float64, book polymarket.BookSnapshot, urgent bool) (float64, bool) {
	if current <= 0 || economicCeiling <= 0 || !validBook(book) {
		return 0, false
	}
	candidate := floorToTick(book.BestBid+book.TickSize, book.TickSize)
	postOnlyCeiling := floorToTick(book.BestAsk-book.TickSize, book.TickSize)
	if urgent {
		candidate = floorToTick(math.Min(economicCeiling, postOnlyCeiling), book.TickSize)
	}
	if candidate > postOnlyCeiling {
		candidate = postOnlyCeiling
	}
	if candidate > economicCeiling {
		candidate = floorToTick(economicCeiling, book.TickSize)
	}
	if candidate <= current+1e-12 || candidate >= book.BestAsk-1e-12 {
		return current, false
	}
	return candidate, true
}

func bookForSide(side string, upBook, downBook polymarket.BookSnapshot) polymarket.BookSnapshot {
	if strings.EqualFold(side, "DOWN") {
		return downBook
	}
	return upBook
}

func tokenForSide(side string, c *PaperCycle) string {
	if strings.EqualFold(side, "DOWN") {
		return c.DownTokenID
	}
	return c.UpTokenID
}

func updateLastBook(c *PaperCycle, upBook, downBook polymarket.BookSnapshot) bool {
	changed := c.LastUpBestBid != upBook.BestBid || c.LastUpBestAsk != upBook.BestAsk || c.LastDownBestBid != downBook.BestBid || c.LastDownBestAsk != downBook.BestAsk
	c.LastUpBestBid, c.LastUpBestAsk = upBook.BestBid, upBook.BestAsk
	c.LastDownBestBid, c.LastDownBestAsk = downBook.BestBid, downBook.BestAsk
	return changed
}

func eventOrNow(t, now time.Time) time.Time {
	now = now.UTC()
	if t.IsZero() || t.After(now.Add(time.Second)) {
		return now
	}
	return t.UTC()
}

func maxInt64(a, b int64) int64 {
	if a > b {
		return a
	}
	return b
}

func parseTime(v string) (time.Time, bool) {
	t, err := time.Parse(time.RFC3339Nano, v)
	if err != nil {
		return time.Time{}, false
	}
	return t, true
}
