package arb

import (
	"math"
	"strings"

	"pm-edge/internal/engine"
	"pm-edge/internal/polymarket"
)

const (
	StatusCandidate      = "CANDIDATE"
	StatusPaperCandidate = "PAPER_CANDIDATE"
	StatusBlocked        = "BLOCKED"
)

type Config struct {
	Timeframe          string
	Enabled            bool
	TargetEdge         float64
	PaperMinEdge       float64
	OperationalBuffer  float64
	UncertaintyPenalty float64
	MaxStrandedUnits   int
}

type Snapshot struct {
	Timestamp    string  `json:"timestamp"`
	Timeframe    string  `json:"timeframe"`
	MarketSlug   string  `json:"marketSlug"`
	Status       string  `json:"status"`
	Reason       string  `json:"reason"`
	ShadowOnly   bool    `json:"shadowOnly"`
	OrderMode    string  `json:"orderMode"`
	StrategyMode string  `json:"strategyMode"`
	MakerFeeRate float64 `json:"makerFeeRate"`

	UpTokenID         string  `json:"upTokenId"`
	DownTokenID       string  `json:"downTokenId"`
	UpBestBid         float64 `json:"upBestBid"`
	UpBestAsk         float64 `json:"upBestAsk"`
	DownBestBid       float64 `json:"downBestBid"`
	DownBestAsk       float64 `json:"downBestAsk"`
	UpTickSize        float64 `json:"upTickSize"`
	DownTickSize      float64 `json:"downTickSize"`
	UpMinOrderSize    float64 `json:"upMinOrderSize"`
	DownMinOrderSize  float64 `json:"downMinOrderSize"`
	OrderSize         float64 `json:"orderSize"`
	MaxStrandedShares float64 `json:"maxStrandedShares"`

	UpMakerPrice         float64 `json:"upMakerPrice"`
	DownMakerPrice       float64 `json:"downMakerPrice"`
	PairCost             float64 `json:"pairCost"`
	GrossEdge            float64 `json:"grossEdge"`
	NetEdge              float64 `json:"netEdge"`
	TargetEdge           float64 `json:"targetEdge"`
	PaperMinEdge         float64 `json:"paperMinEdge"`
	OperationalBuffer    float64 `json:"operationalBuffer"`
	PairEdgePass         bool    `json:"pairEdgePass"`
	PaperEdgePass        bool    `json:"paperEdgePass"`
	LiveEdgePass         bool    `json:"liveEdgePass"`
	ExpectedLockedProfit float64 `json:"expectedLockedProfit"`

	PTBReady           bool    `json:"ptbReady"`
	PTBDecision        string  `json:"ptbDecision"`
	PTBPUp             float64 `json:"ptbPUp"`
	PTBPDown           float64 `json:"ptbPDown"`
	PTBConfidence      float64 `json:"ptbConfidence"`
	UpExitRisk         float64 `json:"upExitRisk"`
	DownExitRisk       float64 `json:"downExitRisk"`
	UpStrandedEV       float64 `json:"upStrandedEv"`
	DownStrandedEV     float64 `json:"downStrandedEv"`
	UpStrandedRisk     float64 `json:"upStrandedRisk"`
	DownStrandedRisk   float64 `json:"downStrandedRisk"`
	FirstLeg           string  `json:"firstLeg"`
	QuoteSkew          string  `json:"quoteSkew"`
	FirstLegQueueAhead float64 `json:"firstLegQueueAhead"`

	DownCompletionMax     float64 `json:"downCompletionMax"`
	UpCompletionMax       float64 `json:"upCompletionMax"`
	LiveDownCompletionMax float64 `json:"liveDownCompletionMax"`
	LiveUpCompletionMax   float64 `json:"liveUpCompletionMax"`
	BookFetchMs           int64   `json:"bookFetchMs"`
}

type Engine struct{ cfg Config }

func NewEngine(cfg Config) *Engine {
	if strings.TrimSpace(cfg.Timeframe) == "" {
		cfg.Timeframe = "5m"
	}
	if cfg.TargetEdge <= 0 {
		cfg.TargetEdge = 0.02
	}
	if cfg.PaperMinEdge <= 0 {
		cfg.PaperMinEdge = 0.002
	}
	if cfg.OperationalBuffer <= 0 {
		cfg.OperationalBuffer = 0.002
	}
	if cfg.UncertaintyPenalty <= 0 {
		cfg.UncertaintyPenalty = 0.02
	}
	if cfg.MaxStrandedUnits < 1 {
		cfg.MaxStrandedUnits = 1
	}
	return &Engine{cfg: cfg}
}

func (e *Engine) Enabled() bool { return e != nil && e.cfg.Enabled }

func (e *Engine) Evaluate(res *engine.EvaluationResult, market *polymarket.Market, upBook, downBook polymarket.BookSnapshot) *Snapshot {
	if e == nil || !e.cfg.Enabled || res == nil || market == nil {
		return nil
	}
	snap := &Snapshot{
		Timestamp:         res.Timestamp,
		Timeframe:         e.cfg.Timeframe,
		MarketSlug:        market.Slug,
		Status:            StatusBlocked,
		Reason:            "BOOK_NOT_READY",
		ShadowOnly:        true,
		OrderMode:         "GTC_GTD_POST_ONLY",
		StrategyMode:      "SAFE_FIRST_SEQUENTIAL_MAKER",
		MakerFeeRate:      0,
		UpTokenID:         upBook.TokenID,
		DownTokenID:       downBook.TokenID,
		UpBestBid:         upBook.BestBid,
		UpBestAsk:         upBook.BestAsk,
		DownBestBid:       downBook.BestBid,
		DownBestAsk:       downBook.BestAsk,
		UpTickSize:        upBook.TickSize,
		DownTickSize:      downBook.TickSize,
		UpMinOrderSize:    upBook.MinOrderSize,
		DownMinOrderSize:  downBook.MinOrderSize,
		TargetEdge:        e.cfg.TargetEdge,
		PaperMinEdge:      e.cfg.PaperMinEdge,
		OperationalBuffer: e.cfg.OperationalBuffer,
		PTBReady:          res.PTBTerminal.Ready,
		PTBDecision:       res.PTBTerminal.Decision,
		PTBConfidence:     res.PTBTerminal.Confidence,
	}
	if !validBook(upBook) || !validBook(downBook) {
		return snap
	}

	snap.OrderSize = math.Max(upBook.MinOrderSize, downBook.MinOrderSize)
	snap.MaxStrandedShares = snap.OrderSize * float64(e.cfg.MaxStrandedUnits)
	pUp, pDown := res.PUp, res.PDown
	if res.PTBTerminal.Ready {
		pUp, pDown = res.PTBTerminal.PAbove, res.PTBTerminal.PBelow
	}
	snap.PTBPUp, snap.PTBPDown = pUp, pDown

	upFirst, okUp := MakerBuyPrice(upBook, true)
	downFirst, okDown := MakerBuyPrice(downBook, true)
	if !okUp {
		upFirst, okUp = MakerBuyPrice(upBook, false)
	}
	if !okDown {
		downFirst, okDown = MakerBuyPrice(downBook, false)
	}
	if !okUp || !okDown {
		return snap
	}

	upEV, upExit, _, upRisk := strandedMetrics(pUp, upFirst, upBook.BestBid, res.PTBTerminal.Confidence, e.cfg.UncertaintyPenalty)
	downEV, downExit, _, downRisk := strandedMetrics(pDown, downFirst, downBook.BestBid, res.PTBTerminal.Confidence, e.cfg.UncertaintyPenalty)

	downAfterUp, okDownAfterUp := completionStartPrice(upFirst, e.cfg.PaperMinEdge, e.cfg.OperationalBuffer, downBook)
	upAfterDown, okUpAfterDown := completionStartPrice(downFirst, e.cfg.PaperMinEdge, e.cfg.OperationalBuffer, upBook)
	upFirstNet := -1.0
	if okDownAfterUp {
		upFirstNet = 1 - upFirst - downAfterUp - e.cfg.OperationalBuffer
	}
	downFirstNet := -1.0
	if okUpAfterDown {
		downFirstNet = 1 - downFirst - upAfterDown - e.cfg.OperationalBuffer
	}
	upEligible := upFirstNet+1e-12 >= e.cfg.PaperMinEdge
	downEligible := downFirstNet+1e-12 >= e.cfg.PaperMinEdge

	if !upEligible && !downEligible {
		snap.NetEdge = -1
		snap.Reason = "NO_COMPETITIVE_COMPLETION_WITHIN_EDGE"
		return snap
	}

	first := "UP"
	if (!upEligible && downEligible) || (upEligible == downEligible && downRisk < upRisk) {
		first = "DOWN"
	}
	if first == "UP" {
		snap.UpMakerPrice = upFirst
		snap.DownMakerPrice = downAfterUp
		snap.NetEdge = upFirstNet
		snap.QuoteSkew = "SAFE_FIRST_UP_THEN_DOWN"
		snap.FirstLegQueueAhead = buyQueueAhead(upBook, upFirst)
	} else {
		snap.DownMakerPrice = downFirst
		snap.UpMakerPrice = upAfterDown
		snap.NetEdge = downFirstNet
		snap.QuoteSkew = "SAFE_FIRST_DOWN_THEN_UP"
		snap.FirstLegQueueAhead = buyQueueAhead(downBook, downFirst)
	}
	snap.FirstLeg = first
	snap.PairCost = snap.UpMakerPrice + snap.DownMakerPrice
	snap.GrossEdge = 1 - snap.PairCost
	snap.PaperEdgePass = snap.NetEdge+1e-12 >= e.cfg.PaperMinEdge
	snap.LiveEdgePass = snap.NetEdge+1e-12 >= e.cfg.TargetEdge
	snap.PairEdgePass = snap.LiveEdgePass
	if snap.PaperEdgePass {
		snap.ExpectedLockedProfit = snap.OrderSize * snap.NetEdge
	}

	snap.UpStrandedEV, snap.UpExitRisk, _, snap.UpStrandedRisk = strandedMetrics(pUp, upFirst, upBook.BestBid, res.PTBTerminal.Confidence, e.cfg.UncertaintyPenalty)
	snap.DownStrandedEV, snap.DownExitRisk, _, snap.DownStrandedRisk = strandedMetrics(pDown, downFirst, downBook.BestBid, res.PTBTerminal.Confidence, e.cfg.UncertaintyPenalty)
	_ = upEV
	_ = downEV
	_ = upExit
	_ = downExit

	snap.DownCompletionMax = completionMax(upFirst, e.cfg.PaperMinEdge, e.cfg.OperationalBuffer, downBook)
	snap.UpCompletionMax = completionMax(downFirst, e.cfg.PaperMinEdge, e.cfg.OperationalBuffer, upBook)
	snap.LiveDownCompletionMax = completionMax(upFirst, e.cfg.TargetEdge, e.cfg.OperationalBuffer, downBook)
	snap.LiveUpCompletionMax = completionMax(downFirst, e.cfg.TargetEdge, e.cfg.OperationalBuffer, upBook)

	if !res.PTBTerminal.Ready {
		snap.Reason = "PTB_TERMINAL_NOT_READY"
		return snap
	}
	if !snap.PaperEdgePass {
		snap.Reason = "PAIR_EDGE_BELOW_PAPER_MIN"
		return snap
	}
	if snap.LiveEdgePass {
		snap.Status = StatusCandidate
		snap.Reason = "READY"
		return snap
	}
	snap.Status = StatusPaperCandidate
	snap.Reason = "PAPER_READY_LIVE_EDGE_BELOW_TARGET"
	return snap
}

func validBook(b polymarket.BookSnapshot) bool {
	return b.TokenID != "" && b.BestBid > 0 && b.BestAsk > b.BestBid && b.BestAsk < 1 && b.TickSize > 0 && b.MinOrderSize > 0
}

// MakerBuyPrice returns a post-only BUY price. improve=true attempts one tick
// above best bid, but never crosses/touches the best ask. If the spread is only
// one tick, it joins the current best bid.
func MakerBuyPrice(book polymarket.BookSnapshot, improve bool) (float64, bool) {
	if !validBook(book) {
		return 0, false
	}
	price := floorToTick(book.BestBid, book.TickSize)
	if improve {
		candidate := floorToTick(book.BestBid+book.TickSize, book.TickSize)
		if candidate < book.BestAsk-1e-12 {
			price = candidate
		}
	}
	if price <= 0 || price >= book.BestAsk-1e-12 {
		return 0, false
	}
	return price, true
}

func completionStartPrice(filledPrice, edgeFloor, operationalBuffer float64, opposite polymarket.BookSnapshot) (float64, bool) {
	if !validBook(opposite) {
		return 0, false
	}
	ceiling := completionMax(filledPrice, edgeFloor, operationalBuffer, opposite)
	if ceiling <= 0 {
		return 0, false
	}
	price, ok := MakerBuyPrice(opposite, true)
	if !ok {
		price, ok = MakerBuyPrice(opposite, false)
	}
	if !ok {
		return 0, false
	}
	if price > ceiling+1e-12 {
		return 0, false
	}
	postOnlyCeiling := floorToTick(opposite.BestAsk-opposite.TickSize, opposite.TickSize)
	if price > postOnlyCeiling {
		price = postOnlyCeiling
	}
	if price <= 0 || price >= opposite.BestAsk-1e-12 {
		return 0, false
	}
	return price, true
}

func completionMax(filledPrice, targetEdge, operationalBuffer float64, opposite polymarket.BookSnapshot) float64 {
	if !validBook(opposite) {
		return 0
	}
	arbCeiling := floorToTick(1-filledPrice-targetEdge-operationalBuffer, opposite.TickSize)
	postOnlyCeiling := floorToTick(opposite.BestAsk-opposite.TickSize, opposite.TickSize)
	if arbCeiling > postOnlyCeiling {
		arbCeiling = postOnlyCeiling
	}
	if arbCeiling < 0 {
		return 0
	}
	return arbCeiling
}

func strandedMetrics(prob, makerPrice, bestBid, confidence, uncertaintyPenalty float64) (ev, exitRisk, modelRisk, risk float64) {
	prob = clamp(prob, 0, 1)
	exitRisk = math.Max(0, makerPrice-bestBid)
	conf := clamp(confidence/100, 0, 1)
	modelRisk = uncertaintyPenalty * (1 - conf) * makerPrice
	ev = prob - makerPrice - exitRisk - modelRisk
	// This is a risk score, not a forecast edge. It penalizes capital exposed to
	// the losing outcome, immediate unwind spread, and model uncertainty.
	risk = (1-prob)*makerPrice + exitRisk + modelRisk
	return ev, exitRisk, modelRisk, risk
}

func buyQueueAhead(book polymarket.BookSnapshot, price float64) float64 {
	if price <= 0 {
		return 0
	}
	q := 0.0
	for _, level := range book.Bids {
		if math.Abs(level.Price-price) <= 1e-9 {
			q += level.Size
		}
	}
	return q
}

func floorToTick(v, tick float64) float64 {
	if tick <= 0 {
		return v
	}
	units := math.Floor((v + 1e-10) / tick)
	return math.Round(units*tick*1e8) / 1e8
}

func clamp(v, lo, hi float64) float64 {
	if v < lo {
		return lo
	}
	if v > hi {
		return hi
	}
	return v
}
