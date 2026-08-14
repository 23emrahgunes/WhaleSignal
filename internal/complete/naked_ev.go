package complete

import "math"

// Naked pozisyon aksiyonlari.
const (
	ActHold  = "HOLD"
	ActSell  = "SELL"
	ActHedge = "HEDGE"
)

// NakedInput: tek-bacak (naked) pozisyonun EV degerlendirmesi icin girdiler.
// Sabit $15 stop / $0.65 hedge KURAL DEGIL — karar bu degerlerden EV ile cikar.
type NakedInput struct {
	Shares      float64 // dolan naked bacak hisse (5)
	Cost        float64 // bu bacak icin odenen ($ = shares*0.40)
	PWin        float64 // P(naked tarafin kazanmasi) [0,1] (Direction Engine'den)
	OutcomeBid  float64 // naked outcome'un guncel bid'i (SELL icin)
	OppositeAsk float64 // karsi outcome ask'i (HEDGE ile kutuyu tamamlamak)
	TakerFee    float64 // taker emri $ ucreti
	Slippage    float64 // orani (0..1)
}

// NakedResult: EV'ler + onerilen aksiyon.
type NakedResult struct {
	EVHold  float64 `json:"evHold"`
	EVSell  float64 `json:"evSell"`
	EVHedge float64 `json:"evHedge"`
	Action  string  `json:"action"`
}

// EvaluateNaked: HOLD/SELL/HEDGE beklenen PnL'ini hesaplar, argmax dondurur.
//
//	HOLD  = P(win)*shares - cost           (kazanirsa shares*$1, kaybederse 0)
//	SELL  = shares*bid*(1-slip) - fee - cost (pozisyonu simdi kapat)
//	HEDGE = shares - cost - hedgeCost        (kutu tamamlanir; biri $1 oder)
//	        hedgeCost = shares*ask*(1+slip) + fee
func EvaluateNaked(in NakedInput) NakedResult {
	evHold := clampProb(in.PWin)*in.Shares - in.Cost
	evSell := in.Shares*in.OutcomeBid*(1-in.Slippage) - in.TakerFee - in.Cost
	hedgeCost := in.Shares*in.OppositeAsk*(1+in.Slippage) + in.TakerFee
	evHedge := in.Shares - in.Cost - hedgeCost

	action := ActHold
	best := evHold
	if evSell > best {
		best, action = evSell, ActSell
	}
	if evHedge > best {
		action = ActHedge
	}
	return NakedResult{EVHold: evHold, EVSell: evSell, EVHedge: evHedge, Action: action}
}

// clampProb: [0,1].
func clampProb(p float64) float64 { return math.Max(0, math.Min(1, p)) }
