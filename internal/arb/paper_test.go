package arb

import (
	"math"
	"testing"
	"time"

	"pm-edge/internal/polymarket"
)

func paperBook(token string, bid, ask float64, bidSize float64) polymarket.BookSnapshot {
	if bidSize <= 0 {
		bidSize = 100
	}
	return polymarket.BookSnapshot{TokenID: token, BestBid: bid, BestAsk: ask, TickSize: .01, MinOrderSize: 5,
		Bids: []polymarket.CLOBLevel{{Price: bid, Size: bidSize}}, Asks: []polymarket.CLOBLevel{{Price: ask, Size: 100}}}
}

func paperSnap() *Snapshot {
	return &Snapshot{Timestamp: "2026-08-12T00:00:00Z", Timeframe: "5m", MarketSlug: "btc-updown-5m-1", Status: StatusPaperCandidate,
		OrderSize: 5, FirstLeg: "UP", UpTokenID: "up", DownTokenID: "down", UpMakerPrice: .41, DownMakerPrice: .54,
		UpBestBid: .40, UpBestAsk: .44, DownBestBid: .53, DownBestAsk: .58, UpCompletionMax: .43, DownCompletionMax: .56,
		PTBReady: true, PTBPUp: .8, PTBPDown: .2, PTBDecision: "UP", NetEdge: .048, TargetEdge: .02, PaperMinEdge: .002,
		OperationalBuffer: .002, PaperEdgePass: true}
}

func sellTrade(seq int64, token string, price, size float64) polymarket.MarketTrade {
	return sellTradeAt(seq, token, price, size, time.Now().UTC())
}

func sellTradeAt(seq int64, token string, price, size float64, ts time.Time) polymarket.MarketTrade {
	return polymarket.MarketTrade{Seq: seq, TokenID: token, Price: price, Size: size, Side: "SELL", Timestamp: ts.UTC()}
}

func TestSafeFirstOnlyAndPartialFill(t *testing.T) {
	now := time.Date(2026, 8, 12, 0, 0, 0, 0, time.UTC)
	up := paperBook("up", .40, .44, 100)
	down := paperBook("down", .53, .58, 100)
	c := NewPaperCycle(paperSnap(), up, down, now, 10, 0)
	if c.Status != PaperStatusRestingFirst || c.FirstOrderSide != "UP" || c.SecondOrderSide != "DOWN" {
		t.Fatalf("bad start %+v", c)
	}
	if c.FirstQueueAhead != 0 {
		t.Fatalf("improved bid should have zero queue, got %.2f", c.FirstQueueAhead)
	}
	AdvancePaperCycle(c, up, down, []polymarket.MarketTrade{sellTrade(11, "up", .41, 2)}, 11, now.Add(time.Second), now.Add(time.Minute), DefaultPaperConfig())
	if c.Status != PaperStatusFirstPartial || math.Abs(c.FirstFilledShares-2) > 1e-9 || c.DownFilledShares != 0 {
		t.Fatalf("partial %+v", c)
	}
}

func TestLowerPrintProvesFullRestingOrderFilled(t *testing.T) {
	now := time.Date(2026, 8, 12, 0, 0, 0, 0, time.UTC)
	up := paperBook("up", .40, .44, 100)
	down := paperBook("down", .53, .58, 100)
	c := NewPaperCycle(paperSnap(), up, down, now, 0, 0)
	AdvancePaperCycle(c, up, down, []polymarket.MarketTrade{sellTrade(1, "up", .40, 1.25)}, 1, now.Add(time.Second), now.Add(time.Minute), DefaultPaperConfig())
	if math.Abs(c.FirstFilledShares-5) > 1e-9 || c.Status != PaperStatusCompleting {
		t.Fatalf("lower sweep must fill full resting order %+v", c)
	}
}

func TestQueueAheadRequiresSellVolume(t *testing.T) {
	now := time.Date(2026, 8, 12, 0, 0, 0, 0, time.UTC)
	s := paperSnap()
	s.UpMakerPrice = .40
	up := paperBook("up", .40, .44, 7)
	down := paperBook("down", .53, .58, 100)
	c := NewPaperCycle(s, up, down, now, 0, 0)
	if c.FirstQueueAhead != 7 {
		t.Fatalf("queue=%.2f", c.FirstQueueAhead)
	}
	AdvancePaperCycle(c, up, down, []polymarket.MarketTrade{sellTrade(1, "up", .40, 5)}, 1, now.Add(time.Second), now.Add(time.Minute), DefaultPaperConfig())
	if c.FirstFilledShares != 0 || math.Abs(c.FirstQueueAhead-2) > 1e-9 {
		t.Fatalf("queue consumption %+v", c)
	}
	AdvancePaperCycle(c, up, down, []polymarket.MarketTrade{sellTrade(2, "up", .40, 4)}, 2, now.Add(2*time.Second), now.Add(time.Minute), DefaultPaperConfig())
	if math.Abs(c.FirstFilledShares-2) > 1e-9 {
		t.Fatalf("expected 2 shares after queue %+v", c)
	}
}

func TestFirstFullStartsCompletionOnlyNextBatch(t *testing.T) {
	now := time.Date(2026, 8, 12, 0, 0, 0, 0, time.UTC)
	up := paperBook("up", .40, .44, 100)
	down := paperBook("down", .53, .58, 100)
	c := NewPaperCycle(paperSnap(), up, down, now, 0, 0)
	trades := []polymarket.MarketTrade{sellTrade(1, "up", .41, 5), sellTrade(2, "down", .54, 5)}
	AdvancePaperCycle(c, up, down, trades, 2, now.Add(time.Second), now.Add(time.Minute), DefaultPaperConfig())
	if c.Status != PaperStatusCompleting || c.SecondFilledShares != 0 {
		t.Fatalf("same-batch second fill forbidden %+v", c)
	}
	AdvancePaperCycle(c, up, down, []polymarket.MarketTrade{sellTrade(3, "down", .54, 5)}, 3, now.Add(2*time.Second), now.Add(time.Minute), DefaultPaperConfig())
	if c.Status != PaperStatusCompleted {
		t.Fatalf("expected completion %+v", c)
	}
	want := 5 * (1 - .41 - .54)
	if math.Abs(c.PaperPnL-want) > 1e-9 {
		t.Fatalf("pnl %.4f want %.4f", c.PaperPnL, want)
	}
}

func TestPartialRiskStartsAtFirstPartialAndTimesOutVWAP(t *testing.T) {
	now := time.Date(2026, 8, 12, 0, 0, 0, 0, time.UTC)
	up := paperBook("up", .40, .44, 100)
	down := paperBook("down", .53, .58, 100)
	c := NewPaperCycle(paperSnap(), up, down, now, 0, 0)
	cfg := DefaultPaperConfig()
	cfg.MaxStranded = 2 * time.Second
	AdvancePaperCycle(c, up, down, []polymarket.MarketTrade{sellTrade(1, "up", .41, 2)}, 1, now.Add(time.Second), now.Add(time.Minute), cfg)
	upLater := paperBook("up", .38, .42, 100)
	AdvancePaperCycle(c, upLater, down, nil, 1, now.Add(4*time.Second), now.Add(time.Minute), cfg)
	if c.Status != PaperStatusStrandedTimeout {
		t.Fatalf("status %+v", c)
	}
	want := 2 * (.38 - .41)
	if math.Abs(c.PaperPnL-want) > 1e-9 {
		t.Fatalf("pnl %.4f want %.4f", c.PaperPnL, want)
	}
}

func TestDataGapInvalidatesWithoutInventingPnL(t *testing.T) {
	now := time.Date(2026, 8, 12, 0, 0, 0, 0, time.UTC)
	c := NewPaperCycle(paperSnap(), paperBook("up", .40, .44, 100), paperBook("down", .53, .58, 100), now, 0, 2)
	if !InvalidatePaperCycleDataGap(c, now.Add(time.Second)) || c.Status != PaperStatusDataGapInvalid || c.PaperPnL != 0 {
		t.Fatalf("gap %+v", c)
	}
}

func TestCompletionRepriceNeverBreaksCeilingOrPostOnly(t *testing.T) {
	book := paperBook("d", .55, .58, 100)
	got, ok := completionReprice(.54, .56, book)
	if !ok || got != .56 {
		t.Fatalf("got %.4f ok=%v", got, ok)
	}
	book = paperBook("d", .56, .57, 100)
	got, ok = completionReprice(.56, .56, book)
	if ok || got != .56 {
		t.Fatalf("ceiling %.4f %v", got, ok)
	}
}

func TestBetterPriceTradeDoesNotConsumeOurSamePriceQueue(t *testing.T) {
	now := time.Date(2026, 8, 12, 0, 0, 0, 0, time.UTC)
	s := paperSnap()
	s.UpMakerPrice = .40
	up := paperBook("up", .40, .44, 7)
	down := paperBook("down", .53, .58, 100)
	c := NewPaperCycle(s, up, down, now, 0, 0)
	if c.FirstQueueAhead != 7 {
		t.Fatalf("queue %.2f", c.FirstQueueAhead)
	}
	AdvancePaperCycle(c, up, down, []polymarket.MarketTrade{sellTrade(1, "up", .41, 50)}, 1, now.Add(time.Second), now.Add(time.Minute), DefaultPaperConfig())
	if c.FirstQueueAhead != 7 || c.FirstFilledShares != 0 {
		t.Fatalf("better-price print changed FIFO %+v", c)
	}
	AdvancePaperCycle(c, up, down, []polymarket.MarketTrade{sellTrade(2, "up", .40, 8)}, 2, now.Add(2*time.Second), now.Add(time.Minute), DefaultPaperConfig())
	if math.Abs(c.FirstFilledShares-1) > 1e-9 {
		t.Fatalf("expected 1 share after 7 ahead %+v", c)
	}
}

func TestCompletionActivationRepricesFromCurrentBookPostOnly(t *testing.T) {
	now := time.Date(2026, 8, 12, 0, 0, 0, 0, time.UTC)
	up := paperBook("up", .40, .44, 100)
	down := paperBook("down", .53, .58, 100)
	c := NewPaperCycle(paperSnap(), up, down, now, 0, 0)
	// The entry-time planned DOWN price is .54. Before UP fills, DOWN moves to
	// .50/.53. Reusing .54 would cross the ask and a real post-only order would
	// be rejected. Activation must recompute .51 from the current book.
	downNow := paperBook("down", .50, .53, 100)
	AdvancePaperCycle(c, up, downNow, []polymarket.MarketTrade{sellTrade(1, "up", .41, 5)}, 1, now.Add(time.Second), now.Add(time.Minute), DefaultPaperConfig())
	if c.Status != PaperStatusCompleting || math.Abs(c.SecondOrderPrice-.51) > 1e-9 {
		t.Fatalf("stale completion price %+v", c)
	}
	if c.SecondOrderPrice >= downNow.BestAsk {
		t.Fatalf("completion must remain post-only %+v", c)
	}
}

func TestCompletionActivationCanRestAtEconomicCeilingBehindBestBid(t *testing.T) {
	now := time.Date(2026, 8, 12, 0, 0, 0, 0, time.UTC)
	up := paperBook("up", .40, .44, 100)
	down := paperBook("down", .53, .58, 100)
	c := NewPaperCycle(paperSnap(), up, down, now, 0, 0)
	// Competitive maker is now .58 but our paper economic ceiling is .56.
	// We may rest at .56; we must not manufacture a competitive .58 completion.
	downNow := paperBook("down", .57, .60, 100)
	AdvancePaperCycle(c, up, downNow, []polymarket.MarketTrade{sellTrade(1, "up", .41, 5)}, 1, now.Add(time.Second), now.Add(time.Minute), DefaultPaperConfig())
	if math.Abs(c.SecondOrderPrice-.56) > 1e-9 {
		t.Fatalf("expected economic-ceiling order %+v", c)
	}
	if c.SecondOrderPrice >= downNow.BestAsk {
		t.Fatalf("not post-only %+v", c)
	}
}

func TestCompletionMsUsesExecutionTimestampNotPollingInterval(t *testing.T) {
	now := time.Date(2026, 8, 12, 0, 0, 0, 0, time.UTC)
	up := paperBook("up", .40, .44, 100)
	down := paperBook("down", .53, .58, 100)
	c := NewPaperCycle(paperSnap(), up, down, now, 0, 0)
	AdvancePaperCycle(c, up, down, []polymarket.MarketTrade{sellTradeAt(1, "up", .41, 5, now.Add(100*time.Millisecond))}, 1, now.Add(time.Second), now.Add(time.Minute), DefaultPaperConfig())
	if c.Status != PaperStatusCompleting {
		t.Fatalf("first %+v", c)
	}
	AdvancePaperCycle(c, up, down, []polymarket.MarketTrade{sellTradeAt(2, "down", .54, 5, now.Add(1180*time.Millisecond))}, 2, now.Add(2*time.Second), now.Add(time.Minute), DefaultPaperConfig())
	if c.Status != PaperStatusCompleted || c.CompletionMs != 180 {
		t.Fatalf("completionMs=%d %+v", c.CompletionMs, c)
	}
}

func TestSoftCompletionJumpsToEconomicCeiling(t *testing.T) {
	book := paperBook("d", .53, .58, 100)
	if got, ok := completionRepriceWithUrgency(.54, .56, book, false); !ok || got != .54 { // bestBid+tick equals current; no move
		if ok || got != .54 {
			t.Fatalf("soft pre-window %.4f %v", got, ok)
		}
	}
	got, ok := completionRepriceWithUrgency(.54, .56, book, true)
	if !ok || got != .56 {
		t.Fatalf("urgent %.4f %v", got, ok)
	}
}
