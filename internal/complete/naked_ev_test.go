package complete

import "testing"

func TestEvaluateNakedHoldWhenLikelyWin(t *testing.T) {
	// naked UP, P(win)=0.9, maliyet 2.00 (5@0.40). HOLD EV = 0.9*5-2 = +2.5
	// SELL bid 0.55: 5*0.55-fee-2 = ~0.7. HEDGE ask 0.60: 5-2-(5*0.6+fee)= ~-0.05
	r := EvaluateNaked(NakedInput{Shares: 5, Cost: 2.0, PWin: 0.9,
		OutcomeBid: 0.55, OppositeAsk: 0.60, TakerFee: 0.05, Slippage: 0.01})
	if r.Action != ActHold {
		t.Fatalf("yuksek P(win) -> HOLD olmali: %+v", r)
	}
}

func TestEvaluateNakedHedgeWhenLosingButBoxCheap(t *testing.T) {
	// naked UP kaybediyor (P=0.1). SELL bid dusuk (0.15). HEDGE ask ucuz (0.45):
	// HEDGE EV = 5-2-(5*0.45+fee) = 5-2-2.30 = +0.70 ; HOLD = 0.1*5-2 = -1.5 ;
	// SELL = 5*0.15-fee-2 = -1.30. -> HEDGE en iyi.
	r := EvaluateNaked(NakedInput{Shares: 5, Cost: 2.0, PWin: 0.1,
		OutcomeBid: 0.15, OppositeAsk: 0.45, TakerFee: 0.05, Slippage: 0.0})
	if r.Action != ActHedge {
		t.Fatalf("kaybeden + ucuz kutu -> HEDGE olmali: %+v (hold %.2f sell %.2f hedge %.2f)",
			r.Action, r.EVHold, r.EVSell, r.EVHedge)
	}
}

func TestEvaluateNakedSellWhenBidHighHedgeExpensive(t *testing.T) {
	// naked UP, orta P(win)=0.3, SELL bid yuksek (0.70) -> SELL = 5*0.70-fee-2=+1.45
	// HOLD = 0.3*5-2 = -0.5 ; HEDGE ask pahali (0.95): 5-2-(5*0.95+fee) = -1.80.
	r := EvaluateNaked(NakedInput{Shares: 5, Cost: 2.0, PWin: 0.3,
		OutcomeBid: 0.70, OppositeAsk: 0.95, TakerFee: 0.05, Slippage: 0.0})
	if r.Action != ActSell {
		t.Fatalf("yuksek bid + pahali hedge -> SELL olmali: %+v", r)
	}
}
