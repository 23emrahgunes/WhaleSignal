package paper

import (
	"math"
	"path/filepath"
	"testing"
	"time"

	"pm-edge/internal/engine"
	"pm-edge/internal/polymarket"
	"pm-edge/internal/storage"
)

func newHedgeTestEngine(t *testing.T) (*storage.Database, *Engine, *polymarket.Market, time.Time) {
	t.Helper()
	db, err := storage.NewDatabase(filepath.Join(t.TempDir(), "hedge.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	if err := db.EnsurePaperHedgeSchema(); err != nil {
		t.Fatal(err)
	}
	now := time.Unix(1786390000, 0).UTC()
	market := &polymarket.Market{
		Question:  "Bitcoin Up or Down - 5 Minutes",
		EventSlug: "btc-updown-5m-1786389900",
		Active:    true,
		StartTime: now.Add(-3 * time.Minute),
		EndTime:   now.Add(90 * time.Second),
		Outcomes:  []string{"Up", "Down"},
		Tokens: []polymarket.Token{
			{Outcome: "Up", Price: 0.40, TokenID: "up-token"},
			{Outcome: "Down", Price: 0.60, TokenID: "down-token"},
		},
	}
	pe := NewEngine(db, Config{
		Enabled:              true,
		InitialBalance:       1000,
		Stake:                2.5,
		MinConfidence:        55,
		MinSecondsToEnd:      30,
		MaxSecondsToEnd:      240,
		HedgeEnabled:         true,
		HedgeWindow:          8,
		HedgeMinVotes:        6,
		HedgeMinConsecutive:  3,
		HedgeScoreThreshold:  0.35,
		HedgeMinProbability:  0.65,
		HedgeMinEdge:         0.03,
		HedgeMinAbsPTBZ:      0.50,
		HedgeMinSecondsToEnd: 20,
		HedgeMaxSecondsToEnd: 120,
	})
	return db, pe, market, now
}

func openUpTrade(t *testing.T, pe *Engine, market *polymarket.Market, now time.Time) *storage.PaperTrade {
	t.Helper()
	res := &engine.EvaluationResult{
		PriceToBeat:      64000,
		CurrentPrice:     64020,
		SecondsRemaining: 90,
		PUp:              0.75,
		PDown:            0.25,
		FinalScore:       0.60,
		Decision:         "UP",
		Confidence:       60,
		DataSource:       "CHAINLINK_RTDS+BINANCE_WS+BINANCE_WS_DEPTH20",
	}
	_, opened, err := pe.MaybeOpen(res, market, now)
	if err != nil || !opened {
		t.Fatalf("open original trade opened=%v err=%v", opened, err)
	}
	trade, err := pe.db.GetOpenPaperTradeByMarket(market.EventSlug)
	if err != nil || trade == nil {
		t.Fatalf("persisted original trade missing: %+v err=%v", trade, err)
	}
	return trade
}

func reverseDownResult(market *polymarket.Market, remaining float64, score float64) *engine.EvaluationResult {
	return &engine.EvaluationResult{
		Slug:             market.EventSlug,
		Question:         market.Question,
		PriceToBeat:      64000,
		CurrentPrice:     63950,
		SecondsRemaining: remaining,
		PUp:              0.20,
		PDown:            0.80,
		PTBZ:             -1.20,
		FinalScore:       score,
		Decision:         "DOWN",
		Confidence:       math.Abs(score) * 100,
		DataSource:       "CHAINLINK_RTDS+BINANCE_WS+BINANCE_WS_DEPTH20",
	}
}

func quoteFullHedge(tokenID string, shares float64) (polymarket.BuyQuote, error) {
	price := 0.40
	return polymarket.BuyQuote{
		TokenID:      tokenID,
		BestAsk:      price,
		AveragePrice: price,
		Shares:       shares,
		Notional:     shares * price,
		Fee:          0,
		TotalCost:    shares * price,
		MinOrderSize: 5,
		LevelsUsed:   1,
	}, nil
}

func TestHedgeDoesNotOpenOnSingleReverseFlip(t *testing.T) {
	db, pe, market, now := newHedgeTestEngine(t)
	defer db.Close()
	openUpTrade(t, pe, market, now)
	res := reverseDownResult(market, 80, -0.80)
	if _, opened, err := pe.MaybeHedge(res, market, now.Add(time.Second), quoteFullHedge); err != nil || opened {
		t.Fatalf("single reverse flip must not hedge, opened=%v err=%v", opened, err)
	}
}

func TestHedgeRejectsAlternatingSignalNoise(t *testing.T) {
	db, pe, market, now := newHedgeTestEngine(t)
	defer db.Close()
	openUpTrade(t, pe, market, now)
	for i := 0; i < 8; i++ {
		res := reverseDownResult(market, 80-float64(i), -0.60)
		if i%2 == 1 {
			res.Decision = "UP"
			res.FinalScore = 0.60
			res.PUp, res.PDown = 0.75, 0.25
			res.PTBZ = 1.0
		}
		if _, opened, err := pe.MaybeHedge(res, market, now.Add(time.Duration(i+1)*time.Second), quoteFullHedge); err != nil {
			t.Fatal(err)
		} else if opened {
			t.Fatalf("alternating noise opened hedge at sample %d", i)
		}
	}
}

func TestHedgeRequiresPersistentReverseRegimeAndPositiveEdge(t *testing.T) {
	db, pe, market, now := newHedgeTestEngine(t)
	defer db.Close()
	trade := openUpTrade(t, pe, market, now)
	for i := 0; i < 8; i++ {
		res := reverseDownResult(market, 80-float64(i), -0.70)
		if i < 2 {
			res.Decision = "UP"
			res.FinalScore = 0.50
			res.PUp, res.PDown = 0.70, 0.30
			res.PTBZ = 0.8
		}
		h, opened, err := pe.MaybeHedge(res, market, now.Add(time.Duration(i+1)*time.Second), quoteFullHedge)
		if err != nil {
			t.Fatal(err)
		}
		if i < 7 && opened {
			t.Fatalf("hedge opened too early at sample %d: %+v", i, h)
		}
		if i == 7 {
			if !opened || h == nil {
				t.Fatalf("expected persistent reverse hedge at sample %d", i)
			}
			if h.Side != "DOWN" || h.OriginalSide != "UP" {
				t.Fatalf("unexpected hedge sides %+v", h)
			}
			if h.Persistence < 0.75 || h.Edge < 0.03 || h.ExpectedImprovement <= 0 {
				t.Fatalf("invalid hedge economics %+v", h)
			}
			if math.Abs(h.Shares-trade.Shares) > 1e-9 {
				t.Fatalf("full-share hedge mismatch %.8f vs %.8f", h.Shares, trade.Shares)
			}
		}
	}
}

func TestHedgeRejectsNegativeCostAdjustedEdge(t *testing.T) {
	db, pe, market, now := newHedgeTestEngine(t)
	defer db.Close()
	openUpTrade(t, pe, market, now)
	expensiveQuote := func(tokenID string, shares float64) (polymarket.BuyQuote, error) {
		price := 0.82
		return polymarket.BuyQuote{TokenID: tokenID, BestAsk: price, AveragePrice: price, Shares: shares, Notional: shares * price, TotalCost: shares * price, MinOrderSize: 5}, nil
	}
	for i := 0; i < 8; i++ {
		res := reverseDownResult(market, 80-float64(i), -0.75)
		if _, opened, err := pe.MaybeHedge(res, market, now.Add(time.Duration(i+1)*time.Second), expensiveQuote); err != nil {
			t.Fatal(err)
		} else if opened {
			t.Fatal("negative cost-adjusted edge must not hedge")
		}
	}
}

func TestHedgeSettlementPreservesOriginalABPnL(t *testing.T) {
	db, pe, market, now := newHedgeTestEngine(t)
	defer db.Close()
	trade := openUpTrade(t, pe, market, now)
	hedgeOpened := false
	for i := 0; i < 8; i++ {
		res := reverseDownResult(market, 80-float64(i), -0.70)
		if i < 2 {
			res.Decision = "UP"
			res.FinalScore = 0.50
			res.PUp, res.PDown = 0.70, 0.30
			res.PTBZ = 0.8
		}
		h, opened, err := pe.MaybeHedge(res, market, now.Add(time.Duration(i+1)*time.Second), quoteFullHedge)
		if err != nil {
			t.Fatal(err)
		}
		if opened {
			hedgeOpened = true
			if i != 7 || h == nil {
				t.Fatalf("hedge opened at wrong point i=%d h=%+v", i, h)
			}
		}
	}
	if !hedgeOpened {
		t.Fatal("persistent reverse regime did not create hedge before settlement")
	}
	settled, err := pe.SettleReady(market.EndTime.Add(time.Second), func(boundary time.Time) (float64, bool) {
		return 63950, true
	})
	if err != nil || settled != 1 {
		t.Fatalf("settled=%d err=%v", settled, err)
	}
	h, err := db.GetPaperHedgeByTradeID(trade.ID)
	if err != nil || h == nil || h.Status != "SETTLED" {
		t.Fatalf("hedge settlement missing: %+v err=%v", h, err)
	}
	if h.PnL <= 0 {
		t.Fatalf("winning hedge should have positive leg pnl: %+v", h)
	}
	trades, err := db.GetPaperTrades(10)
	if err != nil || len(trades) != 1 {
		t.Fatal(err)
	}
	if trades[0].PnL >= 0 {
		t.Fatalf("original A/B hold leg should remain a loss: %+v", trades[0])
	}
	if math.Abs(h.CombinedPnL-(trades[0].PnL+h.PnL)) > 1e-9 {
		t.Fatalf("combined pnl mismatch hedge=%+v trade=%+v", h, trades[0])
	}
	stats, err := db.GetPaperHedgeStats()
	if err != nil {
		t.Fatal(err)
	}
	if stats.TotalHedges != 1 || stats.SettledHedges != 1 || stats.HedgeContribution <= 0 {
		t.Fatalf("unexpected hedge stats %+v", stats)
	}
}

func TestPaperOpenWithCLOBQuoteUsesExecutionCost(t *testing.T) {
	db, pe, market, now := newHedgeTestEngine(t)
	defer db.Close()
	res := &engine.EvaluationResult{
		PriceToBeat: 64000, CurrentPrice: 64010, SecondsRemaining: 90,
		PUp: 0.70, PDown: 0.30, FinalScore: 0.60, Decision: "UP", Confidence: 60,
		DataSource:  "CHAINLINK_RTDS+BINANCE_WS+BINANCE_WS_DEPTH20",
		PTBTerminal: engine.PTBTerminalEstimate{Ready: true, Decision: "UP", PAbove: 0.80, PBelow: 0.20},
	}
	quote := func(tokenID string, budget float64) (polymarket.BuyQuote, error) {
		return polymarket.BuyQuote{TokenID: tokenID, BestAsk: 0.45, AveragePrice: 0.455, Shares: 5.2, Notional: 2.366, Fee: 0.10, TotalCost: 2.466, MinOrderSize: 5, LevelsUsed: 2}, nil
	}
	trade, opened, err := pe.MaybeOpenWithQuote(res, market, now, quote)
	if err != nil || !opened {
		t.Fatalf("CLOB quote open failed opened=%v err=%v", opened, err)
	}
	if math.Abs(trade.EntryPrice-0.455) > 1e-12 || math.Abs(trade.Stake-2.466) > 1e-12 || math.Abs(trade.Shares-5.2) > 1e-12 {
		t.Fatalf("paper fill did not use CLOB economics: %+v", trade)
	}
}
