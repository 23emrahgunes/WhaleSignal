package arb

import (
	"math"
	"testing"
	"time"

	"pm-edge/internal/polymarket"
)

func paperBook(token string, bid, ask float64, askLevels ...polymarket.CLOBLevel) polymarket.BookSnapshot {
	if len(askLevels) == 0 {
		askLevels = []polymarket.CLOBLevel{{Price: ask, Size: 100}}
	}
	return polymarket.BookSnapshot{TokenID: token, BestBid: bid, BestAsk: ask, TickSize: .01, MinOrderSize: 5, Bids: []polymarket.CLOBLevel{{Price: bid, Size: 100}}, Asks: askLevels}
}

func paperSnap() *Snapshot {
	return &Snapshot{Timestamp: "2026-08-12T00:00:00Z", Timeframe: "5m", MarketSlug: "btc-updown-5m-1", Status: StatusCandidate, OrderSize: 5, FirstLeg: "UP", UpMakerPrice: .41, DownMakerPrice: .54, UpBestBid: .40, UpBestAsk: .44, DownBestBid: .54, DownBestAsk: .58, UpCompletionMax: .43, DownCompletionMax: .56, PTBPUp: .8, PTBPDown: .2, PTBDecision: "UP", NetEdge: .048, TargetEdge: .02, OperationalBuffer: .002}
}

func TestPaperCycleDoesNotFillOnTouch(t *testing.T) {
	now := time.Date(2026, 8, 12, 0, 0, 0, 0, time.UTC)
	c := NewPaperCycle(paperSnap(), now)
	up := paperBook("up", .40, .41, polymarket.CLOBLevel{Price: .41, Size: 100})
	down := paperBook("down", .53, .58)
	AdvancePaperCycle(c, up, down, now.Add(time.Second), now.Add(2*time.Minute), DefaultPaperConfig())
	if c.Status != PaperStatusRestingPair {
		t.Fatalf("touch must not fill: %+v", c)
	}
}

func TestPaperCycleFirstLegThenCompletion(t *testing.T) {
	now := time.Date(2026, 8, 12, 0, 0, 0, 0, time.UTC)
	c := NewPaperCycle(paperSnap(), now)
	upCross := paperBook("up", .39, .40, polymarket.CLOBLevel{Price: .40, Size: 10})
	down := paperBook("down", .54, .58)
	if !AdvancePaperCycle(c, upCross, down, now.Add(time.Second), now.Add(2*time.Minute), DefaultPaperConfig()) {
		t.Fatal("expected first fill")
	}
	if c.Status != PaperStatusOneLegFilled || c.ActualFirstLeg != "UP" || !c.PreferredFirstMatched {
		t.Fatalf("bad first fill %+v", c)
	}
	// Reprice the remaining DOWN maker order from .54 to .55, still post-only and below .58 ask.
	AdvancePaperCycle(c, upCross, down, now.Add(2*time.Second), now.Add(2*time.Minute), DefaultPaperConfig())
	if math.Abs(c.DownOrderPrice-.55) > 1e-9 || c.Reprices != 1 {
		t.Fatalf("expected .55 reprice %+v", c)
	}
	downCross := paperBook("down", .53, .54, polymarket.CLOBLevel{Price: .54, Size: 7})
	AdvancePaperCycle(c, upCross, downCross, now.Add(3*time.Second), now.Add(2*time.Minute), DefaultPaperConfig())
	if c.Status != PaperStatusCompleted {
		t.Fatalf("expected completed %+v", c)
	}
	want := 5 * (1 - .41 - .55)
	if math.Abs(c.PaperPnL-want) > 1e-9 {
		t.Fatalf("pnl got %.4f want %.4f", c.PaperPnL, want)
	}
	if c.CompletionMs != 2000 {
		t.Fatalf("completion ms=%d", c.CompletionMs)
	}
}

func TestPaperCycleRequiresFullCrossLiquidity(t *testing.T) {
	now := time.Date(2026, 8, 12, 0, 0, 0, 0, time.UTC)
	c := NewPaperCycle(paperSnap(), now)
	up := paperBook("up", .39, .40, polymarket.CLOBLevel{Price: .40, Size: 2})
	down := paperBook("down", .54, .58)
	AdvancePaperCycle(c, up, down, now.Add(time.Second), now.Add(time.Minute), DefaultPaperConfig())
	if c.Status != PaperStatusRestingPair {
		t.Fatalf("partial cross liquidity must not fake full fill %+v", c)
	}
}

func TestPaperNoFillTTL(t *testing.T) {
	now := time.Date(2026, 8, 12, 0, 0, 0, 0, time.UTC)
	c := NewPaperCycle(paperSnap(), now)
	cfg := DefaultPaperConfig()
	cfg.OrderTTL = 3 * time.Second
	AdvancePaperCycle(c, paperBook("up", .40, .44), paperBook("down", .54, .58), now.Add(4*time.Second), now.Add(time.Minute), cfg)
	if c.Status != PaperStatusExpiredNoFill || c.PaperPnL != 0 {
		t.Fatalf("unexpected %+v", c)
	}
}

func TestPaperStrandedTimeoutMarksToBid(t *testing.T) {
	now := time.Date(2026, 8, 12, 0, 0, 0, 0, time.UTC)
	c := NewPaperCycle(paperSnap(), now)
	upCross := paperBook("up", .39, .40, polymarket.CLOBLevel{Price: .40, Size: 10})
	down := paperBook("down", .54, .58)
	cfg := DefaultPaperConfig()
	cfg.MaxStranded = 2 * time.Second
	AdvancePaperCycle(c, upCross, down, now.Add(time.Second), now.Add(time.Minute), cfg)
	upLater := paperBook("up", .38, .42)
	AdvancePaperCycle(c, upLater, down, now.Add(4*time.Second), now.Add(time.Minute), cfg)
	if c.Status != PaperStatusStrandedTimeout {
		t.Fatalf("unexpected %+v", c)
	}
	want := 5 * (.38 - .41)
	if math.Abs(c.PaperPnL-want) > 1e-9 {
		t.Fatalf("pnl %.4f want %.4f", c.PaperPnL, want)
	}
}

func TestCompletionRepriceNeverBreaksCeilingOrPostOnly(t *testing.T) {
	book := paperBook("d", .55, .58)
	got, ok := completionReprice(.54, .56, book)
	if !ok || got != .56 {
		t.Fatalf("got %.4f ok=%v", got, ok)
	}
	book = paperBook("d", .56, .57)
	got, ok = completionReprice(.56, .56, book)
	if ok || got != .56 {
		t.Fatalf("must not exceed economic ceiling: %.4f %v", got, ok)
	}
}
