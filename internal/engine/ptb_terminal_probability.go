package engine

import (
	"math"

	"pm-edge/internal/binance"
)

// PTBTerminalEstimate answers the market question directly: what is the
// probability that the terminal BTC reference closes above/below the PTB after
// conditioning the price/time/volatility prior on current Binance liquidity and
// aggressive trade flow? Coefficients are research priors and must be calibrated
// on out-of-sample settled markets before they can drive live/paper entries.
type PTBTerminalEstimate struct {
	Ready              bool    `json:"ready"`
	CorridorCovered    bool    `json:"corridorCovered"`
	PTBDistanceUSD     float64 `json:"ptbDistanceUsd"`
	PriorPAbove        float64 `json:"priorPAbove"`
	PriorPBelow        float64 `json:"priorPBelow"`
	PAbove             float64 `json:"pAbove"`
	PBelow             float64 `json:"pBelow"`
	Decision           string  `json:"decision"`
	Confidence         float64 `json:"confidence"`
	BuyRateUSDPerSec   float64 `json:"buyRateUsdPerSec"`
	SellRateUSDPerSec  float64 `json:"sellRateUsdPerSec"`
	UpCoverage         float64 `json:"upCoverage"`
	DownCoverage       float64 `json:"downCoverage"`
	FlowCapacityScore  float64 `json:"flowCapacityScore"`
	MicroEvidenceScore float64 `json:"microEvidenceScore"`
	Urgency            float64 `json:"urgency"`
	LogOddsAdjustment  float64 `json:"logOddsAdjustment"`
}

func EstimatePTBTerminalMicroProbability(priorPAbove, secondsRemaining, currentPrice, priceToBeat float64, s binance.DeepMicroSnapshot, m MicrostructureScores) PTBTerminalEstimate {
	prior := clampProbability(priorPAbove)
	out := PTBTerminalEstimate{
		CorridorCovered: s.PTBCorridorCovered,
		PTBDistanceUSD:  math.Abs(priceToBeat - currentPrice),
		PriorPAbove:     prior,
		PriorPBelow:     1 - prior,
		PAbove:          prior,
		PBelow:          1 - prior,
		Decision:        terminalDecision(prior),
		Confidence:      math.Abs(prior-0.5) * 200,
	}
	if secondsRemaining <= 0 || currentPrice <= 0 || priceToBeat <= 0 || !m.Ready || !s.TradeFlowAvailable || !s.PTBCorridorCovered {
		return out
	}

	buyRate, sellRate := blendedAggressiveRates(s.Trades)
	out.BuyRateUSDPerSec = buyRate
	out.SellRateUSDPerSec = sellRate
	if buyRate+sellRate <= 0 {
		return out
	}

	// Do not project a transient flow regime across an entire 15m market. At
	// most 60 seconds of current aggressor flow is assumed to persist.
	flowHorizon := math.Min(secondsRemaining, 60)
	if flowHorizon < 5 {
		flowHorizon = 5
	}

	if currentPrice < priceToBeat {
		// To finish above PTB, aggressive buyers must consume asks on the path;
		// sellers are compared with the bid support behind spot.
		upBarrier := s.PTBPathAskUSD + 0.35*s.PTBBeyondUSD
		downSupport := s.PTBPathBidUSD
		out.UpCoverage = safeCoverage(buyRate*flowHorizon, upBarrier)
		out.DownCoverage = safeCoverage(sellRate*flowHorizon, downSupport)
	} else {
		// Already above PTB: selling pressure threatens to consume the bid path
		// down to PTB, while buying pressure reinforces staying above it.
		downBarrier := s.PTBPathBidUSD + 0.35*s.PTBBeyondUSD
		upResistance := s.PTBPathAskUSD
		out.UpCoverage = safeCoverage(buyRate*flowHorizon, upResistance)
		out.DownCoverage = safeCoverage(sellRate*flowHorizon, downBarrier)
	}
	out.FlowCapacityScore = math.Tanh(math.Log((1 + out.UpCoverage) / (1 + out.DownCoverage)))

	// PTB path and actual executed flow receive most weight. ±$50/$75 book and
	// wall dynamics are supporting evidence, not substitutes for the target path.
	out.MicroEvidenceScore = clampScore(
		0.35*m.PTBBarrierScore +
			0.30*out.FlowCapacityScore +
			0.20*m.TradeFlowScore +
			0.10*m.DeepBookScore +
			0.05*m.WallDynamicsScore,
	)

	// Microstructure becomes more informative as expiry approaches, while being
	// deliberately damped far from expiry. 120s is the neutral reference point.
	out.Urgency = math.Sqrt(120 / math.Max(secondsRemaining, 15))
	if out.Urgency < 0.35 {
		out.Urgency = 0.35
	} else if out.Urgency > 1.50 {
		out.Urgency = 1.50
	}

	// Bayesian-style odds update: posterior odds = prior odds * exp(adjustment).
	// 1.35 is an intentionally bounded shadow research scale, not a fitted beta.
	out.LogOddsAdjustment = 1.35 * out.Urgency * out.MicroEvidenceScore
	priorLogOdds := math.Log(prior / (1 - prior))
	out.PAbove = logistic(priorLogOdds + out.LogOddsAdjustment)
	out.PBelow = 1 - out.PAbove
	out.Decision = terminalDecision(out.PAbove)
	out.Confidence = math.Abs(out.PAbove-0.5) * 200
	out.Ready = true
	return out
}

func blendedAggressiveRates(rows []binance.TradeWindow) (float64, float64) {
	weights := map[int]float64{15: 0.55, 30: 0.30, 60: 0.15}
	buyRate, sellRate, totalWeight := 0.0, 0.0, 0.0
	for _, row := range rows {
		w, ok := weights[row.Seconds]
		if !ok || row.Seconds <= 0 {
			continue
		}
		buyRate += w * row.BuyUSD / float64(row.Seconds)
		sellRate += w * row.SellUSD / float64(row.Seconds)
		totalWeight += w
	}
	if totalWeight <= 0 {
		return 0, 0
	}
	return buyRate / totalWeight, sellRate / totalWeight
}

func safeCoverage(flowCapacity, barrier float64) float64 {
	if flowCapacity <= 0 {
		return 0
	}
	if barrier <= 0 {
		return 10
	}
	v := flowCapacity / barrier
	if v > 10 {
		return 10
	}
	return v
}

func clampProbability(p float64) float64 {
	if p < 0.02 {
		return 0.02
	}
	if p > 0.98 {
		return 0.98
	}
	return p
}

func logistic(x float64) float64 {
	if x >= 0 {
		z := math.Exp(-x)
		return 1 / (1 + z)
	}
	z := math.Exp(x)
	return z / (1 + z)
}

func terminalDecision(pAbove float64) string {
	if pAbove >= 0.55 {
		return "UP"
	}
	if pAbove <= 0.45 {
		return "DOWN"
	}
	return "NEUTRAL"
}
