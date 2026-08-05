package engine

import (
	"math"
	"time"

	"pm-edge/internal/binance"
	"pm-edge/internal/polymarket"
	"pm-edge/internal/probability"
)

type EvaluationResult struct {
	Timestamp         string         `json:"timestamp"`
	PriceToBeat       float64        `json:"priceToBeat"`
	CurrentPrice      float64        `json:"currentPrice"`
	SecondsRemaining  float64        `json:"secondsRemaining"`
	PUp               float64        `json:"pUp"`
	PDown             float64        `json:"pDown"`
	BidVol            float64        `json:"bidVol"`
	AskVol            float64        `json:"askVol"`
	Imbalance         float64        `json:"imbalance"`
	WeightedImbalance float64        `json:"weightedImbalance"`
	ProbabilityScore  float64        `json:"probabilityScore"`
	OrderFlowScore    float64        `json:"orderFlowScore"`
	TechnicalScore    float64        `json:"technicalScore"`
	FinalScore        float64        `json:"finalScore"`
	Decision          string         `json:"decision"`
	Confidence        float64        `json:"confidence"`
	Indicators        map[string]int `json:"indicators"`
	MarketStale       bool           `json:"marketStale"`
}

type Evaluator struct{}

func NewEvaluator() *Evaluator {
	return &Evaluator{}
}

// Evaluate performs all mathematical, order flow, indicator, and scoring metrics for a single interval tick.
func (e *Evaluator) Evaluate(binanceClient *binance.Client, market *polymarket.Market, nowUTC string, elapsedSec float64) *EvaluationResult {
	currentPrice := binanceClient.GetPrice()
	if currentPrice == 0 {
		return nil
	}

	// 1. Kalan süre hesabı (years)
	var secondsRemaining float64
	if market != nil {
		endTimeUTC := market.EndTime
		nowTime, _ := timeParseRFC3339(nowUTC)
		secondsRemaining = endTimeUTC.Sub(nowTime).Seconds()
	} else {
		// Default or placeholder remaining seconds for headless tests
		secondsRemaining = 150.0
	}
	if secondsRemaining < 0 {
		secondsRemaining = 0
	}
	T := secondsRemaining / 31536000.0 // seconds to years

	// 2. Volatilite ve Drift hesabı (annualized)
	logReturns := binanceClient.GetLogReturns()
	sigmaAnnual := 0.15 // default fallback (15% annualized)
	muAnnual := 0.05    // default fallback (5% annualized)

	if len(logReturns) >= 2 {
		// Calculate mean and variance of log returns
		sum := 0.0
		for _, r := range logReturns {
			sum += r
		}
		mean := sum / float64(len(logReturns))

		varianceSum := 0.0
		for _, r := range logReturns {
			varianceSum += (r - mean) * (r - mean)
		}
		variance := varianceSum / float64(len(logReturns)-1)

		// Annualise: 1s returns -> annual
		// sigma_annual = sqrt(variance * 31,536,000)
		sigmaAnnual = math.Sqrt(variance * 31536000.0)
		// mu_annual = mean * 31,536,000 + 0.5 * sigma_annual^2
		muAnnual = mean*31536000.0 + 0.5*sigmaAnnual*sigmaAnnual
	}

	priceToBeat := 100000.0
	if market != nil {
		priceToBeat = market.PriceToBeat
	}

	// 3. Olasılık hesabı
	pUp, pDown := probability.CalculateProbability(currentPrice, priceToBeat, T, sigmaAnnual, muAnnual)

	// 4. Order Book likidite yoğunluğu analizi, bid/ask imbalance, liquidity wall tespiti
	lastBids, lastAsks := binanceClient.GetLastBidsAndAsks()

	bidVol := 0.0
	askVol := 0.0
	weightedBidVol := 0.0
	weightedAskVol := 0.0

	// Imbalance ve wall detection
	for p, size := range lastBids {
		if binanceClient.IsSpoofing(p, size, true) {
			continue // skip spoofing order
		}
		bidVol += size
		dist := math.Abs(currentPrice - p)
		if dist > 0 {
			weightedBidVol += size / (dist * dist)
		} else {
			weightedBidVol += size
		}
	}

	for p, size := range lastAsks {
		if binanceClient.IsSpoofing(p, size, false) {
			continue // skip spoofing order
		}
		askVol += size
		dist := math.Abs(currentPrice - p)
		if dist > 0 {
			weightedAskVol += size / (dist * dist)
		} else {
			weightedAskVol += size
		}
	}

	imbalance := 0.0
	if (bidVol + askVol) > 0 {
		imbalance = (bidVol - askVol) / (bidVol + askVol)
	}

	weightedImbalance := 0.0
	if (weightedBidVol + weightedAskVol) > 0 {
		weightedImbalance = (weightedBidVol - weightedAskVol) / (weightedBidVol + weightedAskVol)
	}

	// 5. Teknik indikatör skoru
	candles1m := binanceClient.GetCandles("1m")
	candles5m := binanceClient.GetCandles("5m")
	indicators := GetIndicatorScores(candles1m, candles5m)

	technicalSum := 0.0
	for _, val := range indicators {
		technicalSum += float64(val)
	}
	technicalScore := 0.0
	if len(indicators) > 0 {
		technicalScore = technicalSum / float64(len(indicators))
	}

	// 6. Birleşik yön skoru
	// finalScore = 0.55 * probabilityScore + 0.30 * orderFlowScore + 0.15 * technicalScore
	probabilityScore := (pUp - 0.5) * 2.0
	orderFlowScore := weightedImbalance

	finalScore := (0.55 * probabilityScore) + (0.30 * orderFlowScore) + (0.15 * technicalScore)

	decision := "NEUTRAL"
	if finalScore >= 0.20 {
		decision = "UP"
	} else if finalScore <= -0.20 {
		decision = "DOWN"
	}

	confidence := math.Min(100.0, math.Abs(finalScore)*100.0)

	marketStale := false
	if market != nil {
		marketStale = market.MarketStale
	}

	return &EvaluationResult{
		Timestamp:         nowUTC,
		PriceToBeat:       priceToBeat,
		CurrentPrice:      currentPrice,
		SecondsRemaining:  secondsRemaining,
		PUp:               pUp,
		PDown:             pDown,
		BidVol:            bidVol,
		AskVol:            askVol,
		Imbalance:         imbalance,
		WeightedImbalance: weightedImbalance,
		ProbabilityScore:  probabilityScore,
		OrderFlowScore:    orderFlowScore,
		TechnicalScore:    technicalScore,
		FinalScore:        finalScore,
		Decision:          decision,
		Confidence:        confidence,
		Indicators:        indicators,
		MarketStale:       marketStale,
	}
}

func timeParseRFC3339(s string) (time.Time, error) {
	return timeParse(s)
}
