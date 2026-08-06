package engine

import (
	"math"
	"time"

	"pm-edge/internal/binance"
	"pm-edge/internal/polymarket"
	"pm-edge/internal/probability"
)

type EvaluationResult struct {
	Timestamp               string         `json:"timestamp"`
	Question                string         `json:"question"`
	Slug                    string         `json:"slug"`
	MarketEndTime           string         `json:"marketEndTime"`
	PriceToBeat             float64        `json:"priceToBeat"`
	CurrentPrice            float64        `json:"currentPrice"`
	SpotMinusPriceToBeat    float64        `json:"spotMinusPriceToBeat"`
	SecondsRemaining        float64        `json:"secondsRemaining"`
	PUp                     float64        `json:"pUp"`
	PDown                   float64        `json:"pDown"`
	BidVol                  float64        `json:"bidVol"`
	AskVol                  float64        `json:"askVol"`
	SpoofFilteredBidVol     float64        `json:"spoofFilteredBidVol"`
	SpoofFilteredAskVol     float64        `json:"spoofFilteredAskVol"`
	Imbalance               float64        `json:"imbalance"`
	WeightedImbalance       float64        `json:"weightedImbalance"`
	ProbabilityScore        float64        `json:"probabilityScore"`
	OrderFlowScore          float64        `json:"orderFlowScore"`
	TechnicalScore          float64        `json:"technicalScore"`
	Volatility              float64        `json:"volatility"`
	Drift                   float64        `json:"drift"`
	CompositeScore          float64        `json:"compositeScore"`
	FinalScore              float64        `json:"finalScore"`
	Decision                string         `json:"decision"`
	Confidence              float64        `json:"confidence"`
	Indicators              map[string]int `json:"indicators"`
	MarketStale             bool           `json:"marketStale"`
	DataSource              string         `json:"dataSource"`
}

type Evaluator struct{}

func NewEvaluator() *Evaluator {
	return &Evaluator{}
}

func (e *Evaluator) Evaluate(binanceClient *binance.Client, market *polymarket.Market, nowUTC string, elapsedSec float64) *EvaluationResult {
	currentPrice := binanceClient.GetPrice()
	// Defensively ignore or return nil if no real price was fetched yet
	if currentPrice == 0 {
		return nil
	}

	var secondsRemaining float64
	var question string
	var slug string
	var marketEndTime string

	if market != nil {
		endTimeUTC := market.EndTime
		nowTime, _ := timeParseRFC3339(nowUTC)
		secondsRemaining = endTimeUTC.Sub(nowTime).Seconds()
		question = market.Question
		slug = market.Slug
		marketEndTime = endTimeUTC.Format(time.RFC3339)
	} else {
		secondsRemaining = 150.0
		question = "BTC above $100,000 at 15:05?"
		slug = "btc-above-100k-1505"
		marketEndTime = time.Now().UTC().Add(150 * time.Second).Format(time.RFC3339)
	}

	if secondsRemaining < 0 {
		secondsRemaining = 0
	}
	T := secondsRemaining / 31536000.0

	logReturns := binanceClient.GetLogReturns()
	sigmaAnnual := 0.15
	muAnnual := 0.05

	if len(logReturns) >= 2 {
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

		sigmaAnnual = math.Sqrt(variance * 31536000.0)
		muAnnual = mean*31536000.0 + 0.5*sigmaAnnual*sigmaAnnual
	}

	priceToBeat := 100000.0
	if market != nil {
		priceToBeat = market.PriceToBeat
	}

	pUp, pDown := probability.CalculateProbability(currentPrice, priceToBeat, T, sigmaAnnual, muAnnual)

	lastBids, lastAsks := binanceClient.GetLastBidsAndAsks()

	bidVol := 0.0
	askVol := 0.0
	spoofFilteredBidVol := 0.0
	spoofFilteredAskVol := 0.0
	weightedBidVol := 0.0
	weightedAskVol := 0.0

	for p, size := range lastBids {
		bidVol += size
		if binanceClient.IsSpoofing(p, size, true) {
			continue
		}
		spoofFilteredBidVol += size
		dist := math.Abs(currentPrice - p)
		if dist > 0 {
			weightedBidVol += size / (dist * dist)
		} else {
			weightedBidVol += size
		}
	}

	for p, size := range lastAsks {
		askVol += size
		if binanceClient.IsSpoofing(p, size, false) {
			continue
		}
		spoofFilteredAskVol += size
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

	probabilityScore := (pUp - 0.5) * 2.0
	orderFlowScore := weightedImbalance

	compositeScore := (0.55 * probabilityScore) + (0.30 * orderFlowScore) + (0.15 * technicalScore)
	finalScore := compositeScore

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

	source := binanceClient.GetDataSource()

	return &EvaluationResult{
		Timestamp:               nowUTC,
		Question:                question,
		Slug:                    slug,
		MarketEndTime:           marketEndTime,
		PriceToBeat:             priceToBeat,
		CurrentPrice:            currentPrice,
		SpotMinusPriceToBeat:    currentPrice - priceToBeat,
		SecondsRemaining:        secondsRemaining,
		PUp:                     pUp,
		PDown:                   pDown,
		BidVol:                  bidVol,
		AskVol:                  askVol,
		SpoofFilteredBidVol:     spoofFilteredBidVol,
		SpoofFilteredAskVol:     spoofFilteredAskVol,
		Imbalance:               imbalance,
		WeightedImbalance:       weightedImbalance,
		ProbabilityScore:        probabilityScore,
		OrderFlowScore:          orderFlowScore,
		TechnicalScore:          technicalScore,
		Volatility:              sigmaAnnual,
		Drift:                   muAnnual,
		CompositeScore:          compositeScore,
		FinalScore:              finalScore,
		Decision:                decision,
		Confidence:              confidence,
		Indicators:              indicators,
		MarketStale:             marketStale,
		DataSource:              source,
	}
}

func timeParseRFC3339(s string) (time.Time, error) {
	return timeParse(s)
}
