package dual40

import (
	"math"
	"testing"
	"time"

	"pm-edge/internal/polymarket"
)

func dualBook(token string, queue, bestAsk float64) polymarket.BookSnapshot {
	return polymarket.BookSnapshot{
		TokenID: token, BestBid: 0.40, BestAsk: bestAsk, MinOrderSize: 5, TickSize: 0.01,
		Bids: []polymarket.CLOBLevel{{Price: 0.40, Size: queue}, {Price: 0.39, Size: 100}},
		Asks: []polymarket.CLOBLevel{{Price: bestAsk, Size: 100}},
	}
}

func TestNewRestingTrialGateMode(t *testing.T) {
	now := time.Date(2026, 8, 12, 20, 0, 0, 0, time.UTC)
	upBook := dualBook("UP", 5, 0.50)
	downBook := dualBook("DOWN", 0, 0.50)
	notEligible := Metrics{Eligible: false, Regime: "POLY_SKEWED", Reason: "POLYMARKET_SKEW"}

	// feature modu: eligible olmasa da POST eder (mekanik kitap gate gecince)
	featureCfg := NormalizeConfig(Config{GateMode: "feature"})
	if _, err := NewRestingTrial("5m", "m", 10, notEligible, "UP", "DOWN", upBook, downBook, featureCfg, now, 0, 0); err != nil {
		t.Fatalf("feature modu non-eligible'i POST etmeli, hata: %v", err)
	}

	// hard modu: eligible degilse reddeder (eski davranis)
	hardCfg := NormalizeConfig(Config{GateMode: "hard"})
	if _, err := NewRestingTrial("5m", "m", 10, notEligible, "UP", "DOWN", upBook, downBook, hardCfg, now, 0, 0); err == nil {
		t.Fatal("hard modu non-eligible'i reddetmeli")
	}

	// feature modda BILE mekanik post-only gate calisir: ask <= 0.40 -> red
	crossBook := dualBook("UP", 0, 0.40)
	if _, err := NewRestingTrial("5m", "m", 10, notEligible, "UP", "DOWN", crossBook, downBook, featureCfg, now, 0, 0); err == nil {
		t.Fatal("post-only gate: 0.40 ask'te POST etmemeli")
	}
}

func TestRecordFirstFillContext(t *testing.T) {
	tr := &Trial{}
	m := Metrics{MeanFlow: 0.3, DriftBps: 5.5, Regime: "TREND_UP", ChopScore: 42}
	RecordFirstFillContext(tr, m, 18.0)
	if tr.FirstFillRegime != "TREND_UP" || tr.FirstFillDriftBps != 5.5 || tr.FirstFillFlow != 0.3 || tr.FirstFillSecond != 18.0 {
		t.Fatalf("first-fill context yazilmadi: %+v", tr)
	}
}

func TestDual40QueueAwareBothFillLocksOneDollar(t *testing.T) {
	cfg := DefaultConfig()
	m := Metrics{Eligible: true, Regime: "CHOP", Reason: "ELIGIBLE_CHOP", ChopScore: 88}
	now := time.Date(2026, 8, 12, 20, 0, 0, 0, time.UTC)
	upBook := dualBook("UP", 5, 0.50)
	downBook := dualBook("DOWN", 0, 0.50)
	trial, err := NewRestingTrial("5m", "m1", 10, m, "UP", "DOWN", upBook, downBook, cfg, now, 0, 0)
	if err != nil {
		t.Fatal(err)
	}
	trades := []polymarket.MarketTrade{
		{Seq: 1, TokenID: "UP", Price: 0.40, Size: 5, Side: "SELL", Timestamp: now.Add(time.Second)},
		{Seq: 2, TokenID: "UP", Price: 0.40, Size: 5, Side: "SELL", Timestamp: now.Add(2 * time.Second)},
		{Seq: 3, TokenID: "DOWN", Price: 0.39, Size: 5, Side: "SELL", Timestamp: now.Add(3 * time.Second)},
	}
	if !Advance(trial, upBook, downBook, trades, 3, now.Add(4*time.Second), now.Add(5*time.Minute), cfg) {
		t.Fatal("expected trial change")
	}
	if trial.State != StateCompleted {
		t.Fatalf("expected completed, got %s up=%.2f down=%.2f", trial.State, trial.UpMakerFilled, trial.DownMakerFilled)
	}
	if math.Abs(trial.PaperPnL-1.0) > 1e-9 {
		t.Fatalf("expected $1 locked pnl, got %.6f", trial.PaperPnL)
	}
}

func TestAdaptiveHedgeUsesOppositeSideAndQuoteCost(t *testing.T) {
	cfg := DefaultConfig()
	cfg.HedgeMode = "adaptive" // bu test fiyat-tetigini (erken hedge) sinar
	m := Metrics{Eligible: true, Regime: "CHOP", ChopScore: 80}
	now := time.Date(2026, 8, 12, 20, 0, 0, 0, time.UTC)
	upBook := dualBook("UP", 0, 0.50)
	downBook := dualBook("DOWN", 0, 0.71)
	trial, err := NewRestingTrial("5m", "m2", 10, m, "UP", "DOWN", upBook, dualBook("DOWN", 0, 0.50), cfg, now, 0, 0)
	if err != nil {
		t.Fatal(err)
	}
	trades := []polymarket.MarketTrade{{Seq: 1, TokenID: "UP", Price: 0.39, Size: 5, Side: "SELL", Timestamp: now.Add(time.Second)}}
	Advance(trial, upBook, downBook, trades, 1, now.Add(2*time.Second), now.Add(5*time.Minute), cfg)
	req := HedgeNeeded(trial, m, upBook, downBook, now.Add(2*time.Second), now.Add(5*time.Minute), cfg)
	if !req.Needed || req.Side != "DOWN" || math.Abs(req.Shares-5) > 1e-9 {
		t.Fatalf("unexpected hedge request: %+v", req)
	}
	q := polymarket.BuyQuote{TokenID: "DOWN", AveragePrice: 0.71, Shares: 5, Notional: 3.55, Fee: 0.05, TotalCost: 3.60}
	if err := ApplyHedge(trial, req.Side, q, req.TriggerPrice, req.Reason, now.Add(3*time.Second)); err != nil {
		t.Fatal(err)
	}
	if trial.State != StateHedged {
		t.Fatalf("expected hedged, got %s", trial.State)
	}
	if math.Abs(trial.PaperPnL-(-0.60)) > 1e-9 {
		t.Fatalf("expected -0.60 pnl including hedge fee, got %.6f", trial.PaperPnL)
	}
}

func TestSettleAtOutcomeSingleLeg(t *testing.T) {
	now := time.Date(2026, 8, 13, 20, 0, 0, 0, time.UTC)
	// Tek bacak: 5 UP @ 0.40 dolmus (maliyet 2.00), DOWN bos.
	mk := func() *Trial {
		return &Trial{State: StateOneLeg, Shares: 5, EntryPrice: 0.40,
			UpMakerFilled: 5, UpMakerCost: 2.00}
	}
	// UP kazanir (close>open): payout 5*1=5, maliyet 2 -> +3.00
	up := mk()
	if !SettleAtOutcome(up, 63000, 63100, now) {
		t.Fatal("settle up-win basarisiz")
	}
	if up.State != StateSettled || math.Abs(up.PaperPnL-3.0) > 1e-9 {
		t.Fatalf("UP kazanir beklenen +3.00, alinan %.4f (state %s)", up.PaperPnL, up.State)
	}
	// DOWN kazanir (close<open): payout 0, maliyet 2 -> -2.00 (kayip DURUSTCE sayilir)
	dn := mk()
	if !SettleAtOutcome(dn, 63100, 63000, now) {
		t.Fatal("settle down-win basarisiz")
	}
	if dn.State != StateSettled || math.Abs(dn.PaperPnL-(-2.0)) > 1e-9 {
		t.Fatalf("DOWN kazanir beklenen -2.00, alinan %.4f", dn.PaperPnL)
	}
	// Sonuc fiyati yoksa settle etme (cagiran void'e dusmeli)
	if SettleAtOutcome(mk(), 0, 63000, now) {
		t.Fatal("fiyat yokken settle etmemeli")
	}
}

func TestRealisticPartialFill(t *testing.T) {
	trades := []polymarket.MarketTrade{{Seq: 1, TokenID: "UP", Price: 0.39, Size: 1, Side: "SELL", Timestamp: time.Now().UTC()}}
	// realistic: 1 hisselik satis SADECE 1 hisse doldurur (iyimserlik yok)
	r := makerFillFromTrades("UP", 0.40, 0, 5, 0, trades, true)
	if math.Abs(r.Filled-1) > 1e-9 {
		t.Fatalf("realistic: 1 hisselik trade 1 hisse doldurmali, alinan %.2f", r.Filled)
	}
	// legacy: ayni through-trade tum 5 hisseyi doldurur (eski iyimser davranis)
	l := makerFillFromTrades("UP", 0.40, 0, 5, 0, trades, false)
	if math.Abs(l.Filled-5) > 1e-9 {
		t.Fatalf("legacy: through-trade tum emri doldurmali, alinan %.2f", l.Filled)
	}
	// realistic: 5 hisselik akis 5 hisse doldurur (yeterli akis varsa tamamlanir)
	big := []polymarket.MarketTrade{{Seq: 1, TokenID: "UP", Price: 0.40, Size: 5, Side: "SELL", Timestamp: time.Now().UTC()}}
	r2 := makerFillFromTrades("UP", 0.40, 0, 5, 0, big, true)
	if math.Abs(r2.Filled-5) > 1e-9 {
		t.Fatalf("realistic: 5 hisselik akis 5 doldurmali, alinan %.2f", r2.Filled)
	}
}
