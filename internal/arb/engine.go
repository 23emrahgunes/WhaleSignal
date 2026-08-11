package arb

import (
	"math"
	"strings"

	"pm-edge/internal/engine"
	"pm-edge/internal/polymarket"
)

const (
	StatusCandidate = "CANDIDATE"
	StatusBlocked   = "BLOCKED"
)

type Config struct {
	Timeframe          string
	Enabled            bool
	TargetEdge         float64
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
	OperationalBuffer    float64 `json:"operationalBuffer"`
	PairEdgePass         bool    `json:"pairEdgePass"`
	ExpectedLockedProfit float64 `json:"expectedLockedProfit"`

	PTBReady       bool    `json:"ptbReady"`
	PTBDecision    string  `json:"ptbDecision"`
	PTBPUp         float64 `json:"ptbPUp"`
	PTBPDown       float64 `json:"ptbPDown"`
	PTBConfidence  float64 `json:"ptbConfidence"`
	UpExitRisk     float64 `json:"upExitRisk"`
	DownExitRisk   float64 `json:"downExitRisk"`
	UpStrandedEV   float64 `json:"upStrandedEv"`
	DownStrandedEV float64 `json:"downStrandedEv"`
	FirstLeg       string  `json:"firstLeg"`
	QuoteSkew      string  `json:"quoteSkew"`

	DownCompletionMax float64 `json:"downCompletionMax"`
	UpCompletionMax   float64 `json:"upCompletionMax"`
}

type Engine struct{ cfg Config }

func NewEngine(cfg Config) *Engine {
	if strings.TrimSpace(cfg.Timeframe) == "" {
		cfg.Timeframe = "5m"
	}
	if cfg.TargetEdge <= 0 {
		cfg.TargetEdge = 0.02
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

	upPassive, okUpPassive := MakerBuyPrice(upBook, false)
	downPassive, okDownPassive := MakerBuyPrice(downBook, false)
	upAggressive, okUpAggressive := MakerBuyPrice(upBook, true)
	downAggressive, okDownAggressive := MakerBuyPrice(downBook, true)
	if !okUpPassive || !okDownPassive {
		return snap
	}
	if !okUpAggressive {
		upAggressive = upPassive
	}
	if !okDownAggressive {
		downAggressive = downPassive
	}

	upAggEV, _, _ := strandedEV(pUp, upAggressive, upBook.BestBid, res.PTBTerminal.Confidence, e.cfg.UncertaintyPenalty)
	downAggEV, _, _ := strandedEV(pDown, downAggressive, downBook.BestBid, res.PTBTerminal.Confidence, e.cfg.UncertaintyPenalty)
	safeLeg := "UP"
	if downAggEV > upAggEV {
		safeLeg = "DOWN"
	}

	upQuote, downQuote := upPassive, downPassive
	if safeLeg == "UP" {
		upQuote = upAggressive
		snap.QuoteSkew = "UP_AGGRESSIVE_DOWN_PASSIVE"
	} else {
		downQuote = downAggressive
		snap.QuoteSkew = "DOWN_AGGRESSIVE_UP_PASSIVE"
	}

	// If the queue-jump tick consumes too much edge, fall back to both passive
	// maker quotes instead of inventing edge below the target.
	net := 1 - upQuote - downQuote - e.cfg.OperationalBuffer
	if net+1e-12 < e.cfg.TargetEdge {
		upQuote, downQuote = upPassive, downPassive
		snap.QuoteSkew = "BOTH_PASSIVE"
		net = 1 - upQuote - downQuote - e.cfg.OperationalBuffer
	}

	snap.UpMakerPrice, snap.DownMakerPrice = upQuote, downQuote
	snap.PairCost = upQuote + downQuote
	snap.GrossEdge = 1 - snap.PairCost
	snap.NetEdge = net
	snap.PairEdgePass = net+1e-12 >= e.cfg.TargetEdge
	if snap.PairEdgePass {
		snap.ExpectedLockedProfit = snap.OrderSize * snap.NetEdge
	}

	snap.UpStrandedEV, snap.UpExitRisk, _ = strandedEV(pUp, upQuote, upBook.BestBid, res.PTBTerminal.Confidence, e.cfg.UncertaintyPenalty)
	snap.DownStrandedEV, snap.DownExitRisk, _ = strandedEV(pDown, downQuote, downBook.BestBid, res.PTBTerminal.Confidence, e.cfg.UncertaintyPenalty)
	snap.FirstLeg = "UP"
	if snap.DownStrandedEV > snap.UpStrandedEV {
		snap.FirstLeg = "DOWN"
	}

	snap.DownCompletionMax = completionMax(upQuote, e.cfg.TargetEdge, e.cfg.OperationalBuffer, downBook)
	snap.UpCompletionMax = completionMax(downQuote, e.cfg.TargetEdge, e.cfg.OperationalBuffer, upBook)

	if !snap.PairEdgePass {
		snap.Reason = "PAIR_EDGE_BELOW_TARGET"
		return snap
	}
	if !res.PTBTerminal.Ready {
		snap.Reason = "PTB_TERMINAL_NOT_READY"
		return snap
	}
	snap.Status = StatusCandidate
	snap.Reason = "READY"
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

func completionMax(filledPrice, targetEdge, operationalBuffer float64, opposite polymarket.BookSnapshot) float64 {
	if !validBook(opposite) {
		return 0
	}
	arbCeiling := floorToTick(1-filledPrice-targetEdge-operationalBuffer, opposite.TickSize)
	postOnlyCeiling := floorToTick(opposite.BestAsk-opposite.TickSize, opposite.TickSize)
	if postOnlyCeiling < opposite.BestBid {
		postOnlyCeiling = floorToTick(opposite.BestBid, opposite.TickSize)
	}
	if arbCeiling > postOnlyCeiling {
		arbCeiling = postOnlyCeiling
	}
	if arbCeiling < 0 {
		return 0
	}
	return arbCeiling
}

func strandedEV(prob, makerPrice, bestBid, confidence, uncertaintyPenalty float64) (ev, exitRisk, modelRisk float64) {
	prob = clamp(prob, 0, 1)
	exitRisk = math.Max(0, makerPrice-bestBid)
	conf := clamp(confidence/100, 0, 1)
	modelRisk = uncertaintyPenalty * (1 - conf)
	ev = prob - makerPrice - exitRisk - modelRisk
	return ev, exitRisk, modelRisk
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
