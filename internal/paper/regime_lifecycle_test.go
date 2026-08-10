package paper

import (
	"testing"
	"time"
)

func TestPreEntryReverseHistoryCannotTriggerImmediateHedge(t *testing.T) {
	db, pe, market, now := newHedgeTestEngine(t)
	defer db.Close()

	// Feed a full window of strong DOWN evidence before any original position.
	// None of it may count toward the hedge window.
	for i := 0; i < 8; i++ {
		res := reverseDownResult(market, 100-float64(i), -0.80)
		if _, opened, err := pe.MaybeHedge(res, market, now.Add(time.Duration(i)*time.Second), quoteFullHedge); err != nil {
			t.Fatal(err)
		} else if opened {
			t.Fatal("hedge opened without an original position")
		}
	}

	openUpTrade(t, pe, market, now.Add(10*time.Second))
	res := reverseDownResult(market, 75, -0.80)
	if _, opened, err := pe.MaybeHedge(res, market, now.Add(11*time.Second), quoteFullHedge); err != nil {
		t.Fatal(err)
	} else if opened {
		t.Fatal("pre-entry reverse history leaked into post-entry hedge regime")
	}

	persistence, consecutive, _, ready := pe.regimeMetrics(market.EventSlug, "DOWN")
	if ready || persistence != 0 || consecutive != 0 {
		t.Fatalf("one post-entry sample must not satisfy an 8-sample regime: ready=%v persistence=%f consecutive=%d", ready, persistence, consecutive)
	}
}
