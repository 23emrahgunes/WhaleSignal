package arb

import (
	"math"
	"strings"
	"time"

	"pm-edge/internal/polymarket"
)

const (
	PaperStatusRestingPair     = "RESTING_PAIR"
	PaperStatusOneLegFilled    = "ONE_LEG_FILLED"
	PaperStatusCompleted       = "COMPLETED"
	PaperStatusExpiredNoFill   = "EXPIRED_NO_FILL"
	PaperStatusStrandedTimeout = "STRANDED_TIMEOUT"
)

type PaperConfig struct {
	Enabled       bool
	OrderTTL      time.Duration
	MaxStranded   time.Duration
	StopBeforeEnd time.Duration
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
	OrderSize             float64 `json:"orderSize"`
	PreferredFirstLeg     string  `json:"preferredFirstLeg"`
	ActualFirstLeg        string  `json:"actualFirstLeg"`
	PreferredFirstMatched bool    `json:"preferredFirstMatched"`

	UpOrderPrice      float64 `json:"upOrderPrice"`
	DownOrderPrice    float64 `json:"downOrderPrice"`
	UpFillPrice       float64 `json:"upFillPrice"`
	DownFillPrice     float64 `json:"downFillPrice"`
	UpFilledAt        string  `json:"upFilledAt"`
	DownFilledAt      string  `json:"downFilledAt"`
	DownCompletionMax float64 `json:"downCompletionMax"`
	UpCompletionMax   float64 `json:"upCompletionMax"`
	Reprices          int     `json:"reprices"`

	EntryPTBPUp       float64 `json:"entryPtbPUp"`
	EntryPTBPDown     float64 `json:"entryPtbPDown"`
	EntryPTBDecision  string  `json:"entryPtbDecision"`
	EntryNetEdge      float64 `json:"entryNetEdge"`
	TargetEdge        float64 `json:"targetEdge"`
	OperationalBuffer float64 `json:"operationalBuffer"`

	FirstFillAt      string  `json:"firstFillAt"`
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
	return PaperConfig{Enabled: true, OrderTTL: 12 * time.Second, MaxStranded: 20 * time.Second, StopBeforeEnd: 12 * time.Second}
}

func NewPaperCycle(s *Snapshot, now time.Time) *PaperCycle {
	if s == nil || s.Status != StatusCandidate || s.OrderSize <= 0 || s.UpMakerPrice <= 0 || s.DownMakerPrice <= 0 {
		return nil
	}
	now = now.UTC()
	return &PaperCycle{
		Timeframe: s.Timeframe, MarketSlug: s.MarketSlug,
		CreatedAt: now.Format(time.RFC3339Nano), UpdatedAt: now.Format(time.RFC3339Nano),
		Status: PaperStatusRestingPair, Reason: "PAIR_POSTED_SHADOW",
		FillModel: "CONSERVATIVE_CROSS_THROUGH_FULL_SIZE", OrderMode: "GTC_GTD_POST_ONLY",
		OrderSize: s.OrderSize, PreferredFirstLeg: s.FirstLeg,
		UpOrderPrice: s.UpMakerPrice, DownOrderPrice: s.DownMakerPrice,
		DownCompletionMax: s.DownCompletionMax, UpCompletionMax: s.UpCompletionMax,
		EntryPTBPUp: s.PTBPUp, EntryPTBPDown: s.PTBPDown, EntryPTBDecision: s.PTBDecision,
		EntryNetEdge: s.NetEdge, TargetEdge: s.TargetEdge, OperationalBuffer: s.OperationalBuffer,
		ReservedPairCost: s.OrderSize * (s.UpMakerPrice + s.DownMakerPrice),
		LastUpBestBid:    s.UpBestBid, LastUpBestAsk: s.UpBestAsk,
		LastDownBestBid: s.DownBestBid, LastDownBestAsk: s.DownBestAsk,
	}
}

func (c *PaperCycle) IsOpen() bool {
	return c != nil && (c.Status == PaperStatusRestingPair || c.Status == PaperStatusOneLegFilled)
}

func (c *PaperCycle) IsTerminal() bool { return c != nil && !c.IsOpen() }

func AdvancePaperCycle(c *PaperCycle, upBook, downBook polymarket.BookSnapshot, now, marketEnd time.Time, cfg PaperConfig) bool {
	if c == nil || !c.IsOpen() {
		return false
	}
	if cfg.OrderTTL <= 0 {
		cfg.OrderTTL = 12 * time.Second
	}
	if cfg.MaxStranded <= 0 {
		cfg.MaxStranded = 20 * time.Second
	}
	if cfg.StopBeforeEnd <= 0 {
		cfg.StopBeforeEnd = 12 * time.Second
	}
	if !validBook(upBook) || !validBook(downBook) {
		return false
	}
	now = now.UTC()
	changed := updateLastBook(c, upBook, downBook)

	if c.Status == PaperStatusRestingPair {
		upFill := makerCrossFill(upBook, c.UpOrderPrice, c.OrderSize)
		downFill := makerCrossFill(downBook, c.DownOrderPrice, c.OrderSize)
		switch {
		case upFill && downFill:
			fillBoth(c, now)
			c.ActualFirstLeg = "SIMULTANEOUS"
			c.PreferredFirstMatched = false
			c.CompletionMs = 0
			completeCycle(c, now)
			return true
		case upFill:
			fillFirst(c, "UP", now)
			changed = true
		case downFill:
			fillFirst(c, "DOWN", now)
			changed = true
		default:
			created, ok := parseTime(c.CreatedAt)
			if ok && now.Sub(created) >= cfg.OrderTTL {
				c.Status = PaperStatusExpiredNoFill
				c.Reason = "PAIR_ORDER_TTL_EXPIRED"
				c.UpdatedAt = now.Format(time.RFC3339Nano)
				return true
			}
			if !marketEnd.IsZero() && marketEnd.Sub(now) <= cfg.StopBeforeEnd {
				c.Status = PaperStatusExpiredNoFill
				c.Reason = "TOO_CLOSE_TO_MARKET_END_NO_FILL"
				c.UpdatedAt = now.Format(time.RFC3339Nano)
				return true
			}
			if changed {
				c.UpdatedAt = now.Format(time.RFC3339Nano)
			}
			return changed
		}
	}

	if c.Status != PaperStatusOneLegFilled {
		return changed
	}

	firstAt, _ := parseTime(c.FirstFillAt)
	if !firstAt.IsZero() {
		c.StrandedSeconds = math.Max(0, now.Sub(firstAt).Seconds())
	}

	if c.ActualFirstLeg == "UP" {
		if makerCrossFill(downBook, c.DownOrderPrice, c.OrderSize) {
			c.DownFillPrice = c.DownOrderPrice
			c.DownFilledAt = now.Format(time.RFC3339Nano)
			c.DeployedCost += c.OrderSize * c.DownFillPrice
			if !firstAt.IsZero() {
				c.CompletionMs = now.Sub(firstAt).Milliseconds()
			}
			completeCycle(c, now)
			return true
		}
	} else if c.ActualFirstLeg == "DOWN" {
		if makerCrossFill(upBook, c.UpOrderPrice, c.OrderSize) {
			c.UpFillPrice = c.UpOrderPrice
			c.UpFilledAt = now.Format(time.RFC3339Nano)
			c.DeployedCost += c.OrderSize * c.UpFillPrice
			if !firstAt.IsZero() {
				c.CompletionMs = now.Sub(firstAt).Milliseconds()
			}
			completeCycle(c, now)
			return true
		}
	}

	if (!marketEnd.IsZero() && marketEnd.Sub(now) <= cfg.StopBeforeEnd) || (!firstAt.IsZero() && now.Sub(firstAt) >= cfg.MaxStranded) {
		timeoutStranded(c, upBook, downBook, now)
		return true
	}

	if c.ActualFirstLeg == "UP" {
		if p, ok := completionReprice(c.DownOrderPrice, c.DownCompletionMax, downBook); ok && p > c.DownOrderPrice+1e-12 {
			c.DownOrderPrice = p
			c.Reprices++
			changed = true
		}
	} else if c.ActualFirstLeg == "DOWN" {
		if p, ok := completionReprice(c.UpOrderPrice, c.UpCompletionMax, upBook); ok && p > c.UpOrderPrice+1e-12 {
			c.UpOrderPrice = p
			c.Reprices++
			changed = true
		}
	}
	if changed {
		c.UpdatedAt = now.Format(time.RFC3339Nano)
	}
	return changed
}

func ClosePaperCycleForMarketChange(c *PaperCycle, now time.Time) bool {
	if c == nil || !c.IsOpen() {
		return false
	}
	now = now.UTC()
	if c.Status == PaperStatusRestingPair {
		c.Status = PaperStatusExpiredNoFill
		c.Reason = "MARKET_CHANGED_NO_FILL"
		c.UpdatedAt = now.Format(time.RFC3339Nano)
		return true
	}
	if c.ActualFirstLeg == "UP" {
		c.ExitMarkPrice = c.LastUpBestBid
		c.PaperPnL = c.OrderSize * (c.ExitMarkPrice - c.UpFillPrice)
	} else if c.ActualFirstLeg == "DOWN" {
		c.ExitMarkPrice = c.LastDownBestBid
		c.PaperPnL = c.OrderSize * (c.ExitMarkPrice - c.DownFillPrice)
	}
	c.Status = PaperStatusStrandedTimeout
	c.Reason = "MARKET_CHANGED_MARK_TO_LAST_BID"
	c.UpdatedAt = now.Format(time.RFC3339Nano)
	return true
}

func fillFirst(c *PaperCycle, side string, now time.Time) {
	c.Status = PaperStatusOneLegFilled
	c.Reason = "FIRST_LEG_FILLED"
	c.ActualFirstLeg = side
	c.PreferredFirstMatched = strings.EqualFold(c.PreferredFirstLeg, side)
	c.FirstFillAt = now.Format(time.RFC3339Nano)
	if side == "UP" {
		c.UpFillPrice = c.UpOrderPrice
		c.UpFilledAt = c.FirstFillAt
		c.DeployedCost = c.OrderSize * c.UpFillPrice
	} else {
		c.DownFillPrice = c.DownOrderPrice
		c.DownFilledAt = c.FirstFillAt
		c.DeployedCost = c.OrderSize * c.DownFillPrice
	}
	c.UpdatedAt = c.FirstFillAt
}

func fillBoth(c *PaperCycle, now time.Time) {
	ts := now.Format(time.RFC3339Nano)
	c.UpFillPrice, c.DownFillPrice = c.UpOrderPrice, c.DownOrderPrice
	c.UpFilledAt, c.DownFilledAt = ts, ts
	c.FirstFillAt = ts
	c.DeployedCost = c.OrderSize * (c.UpFillPrice + c.DownFillPrice)
}

func completeCycle(c *PaperCycle, now time.Time) {
	c.Status = PaperStatusCompleted
	c.Reason = "PAIR_COMPLETED_LOCKED"
	c.LockedPnL = c.OrderSize * (1 - c.UpFillPrice - c.DownFillPrice)
	c.PaperPnL = c.LockedPnL
	c.StrandedSeconds = float64(c.CompletionMs) / 1000
	c.UpdatedAt = now.UTC().Format(time.RFC3339Nano)
}

func timeoutStranded(c *PaperCycle, upBook, downBook polymarket.BookSnapshot, now time.Time) {
	if c.ActualFirstLeg == "UP" {
		c.ExitMarkPrice = upBook.BestBid
		c.PaperPnL = c.OrderSize * (c.ExitMarkPrice - c.UpFillPrice)
	} else {
		c.ExitMarkPrice = downBook.BestBid
		c.PaperPnL = c.OrderSize * (c.ExitMarkPrice - c.DownFillPrice)
	}
	c.Status = PaperStatusStrandedTimeout
	c.Reason = "STRANDED_TIMEOUT_MARK_TO_BID"
	c.UpdatedAt = now.UTC().Format(time.RFC3339Nano)
}

func makerCrossFill(book polymarket.BookSnapshot, orderPrice, orderSize float64) bool {
	if orderPrice <= 0 || orderSize <= 0 || !validBook(book) {
		return false
	}
	// Conservative: merely touching our hypothetical maker limit is not enough.
	// We require the public ask book to move strictly THROUGH the resting limit
	// and show at least one full order unit of sell liquidity below that limit.
	qty := 0.0
	for _, level := range book.Asks {
		if level.Price >= orderPrice-1e-12 {
			break
		}
		qty += level.Size
		if qty+1e-9 >= orderSize {
			return true
		}
	}
	return false
}

func completionReprice(current, economicCeiling float64, book polymarket.BookSnapshot) (float64, bool) {
	if current <= 0 || economicCeiling <= 0 || !validBook(book) {
		return 0, false
	}
	candidate := floorToTick(book.BestBid+book.TickSize, book.TickSize)
	postOnlyCeiling := floorToTick(book.BestAsk-book.TickSize, book.TickSize)
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

func updateLastBook(c *PaperCycle, upBook, downBook polymarket.BookSnapshot) bool {
	changed := c.LastUpBestBid != upBook.BestBid || c.LastUpBestAsk != upBook.BestAsk || c.LastDownBestBid != downBook.BestBid || c.LastDownBestAsk != downBook.BestAsk
	c.LastUpBestBid, c.LastUpBestAsk = upBook.BestBid, upBook.BestAsk
	c.LastDownBestBid, c.LastDownBestAsk = downBook.BestBid, downBook.BestAsk
	return changed
}

func parseTime(v string) (time.Time, bool) {
	t, err := time.Parse(time.RFC3339Nano, v)
	if err != nil {
		return time.Time{}, false
	}
	return t, true
}
