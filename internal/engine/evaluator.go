package engine

import (
	"math"
	"time"

	"pm-edge/internal/binance"
	"pm-edge/internal/polymarket"
	"pm-edge/internal/probability"
)

type EvaluationResult struct {
	Timestamp             string         `json:"timestamp"`
	Question              string         `json:"question"`
	Slug                  string         `json:"slug"`
	MarketEndTime         string         `json:"marketEndTime"`
	PriceToBeat           float64        `json:"priceToBeat"`
	CurrentPrice          float64        `json:"currentPrice"`
	SpotMinusPriceToBeat  float64        `json:"spotMinusPriceToBeat"`
	SecondsRemaining      float64        `json:"secondsRemaining"`
	PUp                   float64        `json:"pUp"`
	PDown                 float64        `json:"pDown"`
	BidVol                float64        `json:"bidVol"`
	AskVol                float64        `json:"askVol"`
	SpoofFilteredBidVol   float64        `json:"spoofFilteredBidVol"`
	SpoofFilteredAskVol   float64        `json:"spoofFilteredAskVol"`
	BidNotionalUSD        float64        `json:"bidNotionalUsd"`
	AskNotionalUSD        float64        `json:"askNotionalUsd"`
	TotalDepthNotionalUSD float64        `json:"totalDepthNotionalUsd"`
	BestBid               float64        `json:"bestBid"`
	BestAsk               float64        `json:"bestAsk"`
	WorstBid20            float64        `json:"worstBid20"`
	WorstAsk20            float64        `json:"worstAsk20"`
	DepthMidPrice         float64        `json:"depthMidPrice"`
	SpreadUSD             float64        `json:"spreadUsd"`
	SpreadBps             float64        `json:"spreadBps"`
	BidRangeUSD           float64        `json:"bidRangeUsd"`
	AskRangeUSD           float64        `json:"askRangeUsd"`
	BidRangeBps           float64        `json:"bidRangeBps"`
	AskRangeBps           float64        `json:"askRangeBps"`
	Imbalance             float64        `json:"imbalance"`
	WeightedImbalance     float64        `json:"weightedImbalance"`
	ProbabilityScore      float64        `json:"probabilityScore"`
	OrderFlowScore        float64        `json:"orderFlowScore"`
	TechnicalScore        float64        `json:"technicalScore"`
	Volatility            float64        `json:"volatility"`
	Drift                 float64        `json:"drift"`
	CompositeScore        float64        `json:"compositeScore"`
	FinalScore            float64        `json:"finalScore"`
	Decision              string         `json:"decision"`
	Confidence            float64        `json:"confidence"`
	Indicators            map[string]int `json:"indicators"`
	MarketStale           bool           `json:"marketStale"`
	DataSource            string         `json:"dataSource"`
	DepthSource           string         `json:"depthSource"`
	DepthFresh            bool           `json:"depthFresh"`
	DepthAgeMs            int64          `json:"depthAgeMs"`
}

type Evaluator struct{}

func NewEvaluator() *Evaluator { return &Evaluator{} }

// Evaluate fails closed unless the reference price, Binance price and Depth20
// orderbook are all fresh. A missing orderbook must never be represented as a
// real 0.00% imbalance.
func (e *Evaluator) Evaluate(binanceClient *binance.Client, market *polymarket.Market, referencePrice float64, referenceFresh bool, nowUTC string) *EvaluationResult {
	if market == nil || market.PriceToBeat <= 0 || referencePrice <= 0 || !referenceFresh {
		return nil
	}
	if !market.Active || market.Closed {
		return nil
	}
	if !binanceClient.IsPriceFresh(3*time.Second) || !binanceClient.IsDepthFresh(3*time.Second) {
		return nil
	}

	nowTime, err := timeParseRFC3339(nowUTC)
	if err != nil {
		return nil
	}
	secondsRemaining := market.EndTime.Sub(nowTime).Seconds()
	if secondsRemaining <= 0 || secondsRemaining > 5*60+5 {
		return nil
	}

	currentPrice := referencePrice
	priceToBeat := market.PriceToBeat
	T := secondsRemaining / 31536000.0
	logReturns := binanceClient.GetLogReturns()
	sigmaAnnual := 0.15
	muAnnual := 0.0
	if len(logReturns) >= 10 {
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
		muAnnual = mean * 31536000.0
	}

	pUp, pDown := probability.CalculateProbability(currentPrice, priceToBeat, T, sigmaAnnual, muAnnual)
	lastBids, lastAsks := binanceClient.GetLastBidsAndAsks()
	if len(lastBids) == 0 || len(lastAsks) == 0 {
		return nil
	}
	depth := CalculateDepthMetrics(lastBids, lastAsks, binanceClient.IsSpoofing)
	if depth.BestBid <= 0 || depth.BestAsk <= 0 || depth.FilteredBidVol+depth.FilteredAskVol <= 0 {
		return nil
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
	orderFlowScore := depth.WeightedImbalance
	// Guard against any single microstructure component overwhelming the full
	// decision. The displayed weighted imbalance remains raw and bounded [-1,1].
	if orderFlowScore > 0.80 {
		orderFlowScore = 0.80
	} else if orderFlowScore < -0.80 {
		orderFlowScore = -0.80
	}
	compositeScore := (0.55 * probabilityScore) + (0.30 * orderFlowScore) + (0.15 * technicalScore)
	finalScore := compositeScore
	decision := "NEUTRAL"
	if finalScore >= 0.20 {
		decision = "UP"
	} else if finalScore <= -0.20 {
		decision = "DOWN"
	}
	confidence := math.Min(100.0, math.Abs(finalScore)*100.0)
	depthAge := binanceClient.DepthAge(nowTime)
	depthAgeMs := int64(-1)
	if depthAge >= 0 {
		depthAgeMs = depthAge.Milliseconds()
	}
	depthSource := binanceClient.GetDepthDataSource()

	return &EvaluationResult{
		Timestamp:             nowUTC,
		Question:              market.Question,
		Slug:                  market.EventSlug,
		MarketEndTime:         market.EndTime.UTC().Format(time.RFC3339),
		PriceToBeat:           priceToBeat,
		CurrentPrice:          currentPrice,
		SpotMinusPriceToBeat:  currentPrice - priceToBeat,
		SecondsRemaining:      secondsRemaining,
		PUp:                   pUp,
		PDown:                 pDown,
		BidVol:                depth.BidVol,
		AskVol:                depth.AskVol,
		SpoofFilteredBidVol:   depth.FilteredBidVol,
		SpoofFilteredAskVol:   depth.FilteredAskVol,
		BidNotionalUSD:        depth.BidNotionalUSD,
		AskNotionalUSD:        depth.AskNotionalUSD,
		TotalDepthNotionalUSD: depth.TotalNotionalUSD,
		BestBid:               depth.BestBid,
		BestAsk:               depth.BestAsk,
		WorstBid20:            depth.WorstBid,
		WorstAsk20:            depth.WorstAsk,
		DepthMidPrice:         depth.MidPrice,
		SpreadUSD:             depth.SpreadUSD,
		SpreadBps:             depth.SpreadBps,
		BidRangeUSD:           depth.BidRangeUSD,
		AskRangeUSD:           depth.AskRangeUSD,
		BidRangeBps:           depth.BidRangeBps,
		AskRangeBps:           depth.AskRangeBps,
		Imbalance:             depth.Imbalance,
		WeightedImbalance:     depth.WeightedImbalance,
		ProbabilityScore:      probabilityScore,
		OrderFlowScore:        orderFlowScore,
		TechnicalScore:        technicalScore,
		Volatility:            sigmaAnnual,
		Drift:                 muAnnual,
		CompositeScore:        compositeScore,
		FinalScore:            finalScore,
		Decision:              decision,
		Confidence:            confidence,
		Indicators:            indicators,
		MarketStale:           market.MarketStale,
		DataSource:            "CHAINLINK_RTDS+" + binanceClient.GetDataSource() + "+" + depthSource,
		DepthSource:           depthSource,
		DepthFresh:            true,
		DepthAgeMs:            depthAgeMs,
	}
}

func timeParseRFC3339(s string) (time.Time, error) { return timeParse(s) }
