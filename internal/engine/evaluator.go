package engine

import (
	"math"
	"time"

	"pm-edge/internal/binance"
	"pm-edge/internal/polymarket"
	"pm-edge/internal/probability"
)

type EvaluationResult struct {
	Timestamp                  string         `json:"timestamp"`
	Question                   string         `json:"question"`
	Slug                       string         `json:"slug"`
	MarketEndTime              string         `json:"marketEndTime"`
	PriceToBeat                float64        `json:"priceToBeat"`
	CurrentPrice               float64        `json:"currentPrice"`
	BinancePrice               float64        `json:"binancePrice"`
	ChainlinkBinanceBasisBps   float64        `json:"chainlinkBinanceBasisBps"`
	SpotMinusPriceToBeat       float64        `json:"spotMinusPriceToBeat"`
	SecondsRemaining           float64        `json:"secondsRemaining"`
	PUp                        float64        `json:"pUp"`
	PDown                      float64        `json:"pDown"`
	ForecastReady              bool           `json:"forecastReady"`
	ForecastSamples            int            `json:"forecastSamples"`
	ForecastPrice              float64        `json:"forecastPrice"`
	ForecastMeanPrice          float64        `json:"forecastMeanPrice"`
	ForecastLow68              float64        `json:"forecastLow68"`
	ForecastHigh68             float64        `json:"forecastHigh68"`
	ForecastLow95              float64        `json:"forecastLow95"`
	ForecastHigh95             float64        `json:"forecastHigh95"`
	PTBZ                       float64        `json:"ptbZ"`
	RequiredMoveBps            float64        `json:"requiredMoveBps"`
	ExpectedMoveBps            float64        `json:"expectedMoveBps"`
	ForecastSigmaExpiryBps     float64        `json:"forecastSigmaExpiryBps"`
	ForecastConfidence         float64        `json:"forecastConfidence"`
	MicroVolatilityAnnual      float64        `json:"microVolatilityAnnual"`
	VolatilityFloorAnnual      float64        `json:"volatilityFloorAnnual"`
	BasisVolatilityAnnual      float64        `json:"basisVolatilityAnnual"`
	BidVol                     float64        `json:"bidVol"`
	AskVol                     float64        `json:"askVol"`
	SpoofFilteredBidVol        float64        `json:"spoofFilteredBidVol"`
	SpoofFilteredAskVol        float64        `json:"spoofFilteredAskVol"`
	BidNotionalUSD             float64        `json:"bidNotionalUsd"`
	AskNotionalUSD             float64        `json:"askNotionalUsd"`
	TotalDepthNotionalUSD      float64        `json:"totalDepthNotionalUsd"`
	BestBid                    float64        `json:"bestBid"`
	BestAsk                    float64        `json:"bestAsk"`
	WorstBid20                 float64        `json:"worstBid20"`
	WorstAsk20                 float64        `json:"worstAsk20"`
	DepthMidPrice              float64        `json:"depthMidPrice"`
	SpreadUSD                  float64        `json:"spreadUsd"`
	SpreadBps                  float64        `json:"spreadBps"`
	BidRangeUSD                float64        `json:"bidRangeUsd"`
	AskRangeUSD                float64        `json:"askRangeUsd"`
	BidRangeBps                float64        `json:"bidRangeBps"`
	AskRangeBps                float64        `json:"askRangeBps"`
	Imbalance                  float64        `json:"imbalance"`
	WeightedImbalance          float64        `json:"weightedImbalance"`
	ProbabilityScore           float64        `json:"probabilityScore"`
	OrderFlowScore             float64        `json:"orderFlowScore"`
	TechnicalScore             float64        `json:"technicalScore"`
	Volatility                 float64        `json:"volatility"`
	Drift                      float64        `json:"drift"`
	CompositeScore             float64        `json:"compositeScore"`
	FinalScore                 float64        `json:"finalScore"`
	Decision                   string         `json:"decision"`
	Confidence                 float64        `json:"confidence"`
	Indicators                 map[string]int `json:"indicators"`
	MarketStale                bool           `json:"marketStale"`
	DataSource                 string         `json:"dataSource"`
	DepthSource                string         `json:"depthSource"`
	DepthFresh                 bool           `json:"depthFresh"`
	DepthAgeMs                 int64          `json:"depthAgeMs"`
}

type Evaluator struct {
	basis basisTracker
}

func NewEvaluator() *Evaluator { return &Evaluator{} }

// Evaluate fails closed unless the Chainlink reference, Binance spot, Depth20
// orderbook and terminal-forecast inputs are all fresh enough for research use.
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
	binanceSpot := binanceClient.GetPrice()
	if binanceSpot <= 0 {
		return nil
	}
	candles1m := binanceClient.GetCandles("1m")
	candles5m := binanceClient.GetCandles("5m")
	macroFloor := estimateMacroVolFloor(candles1m)
	basisBps, basisVol := e.basis.Observe(currentPrice, binanceSpot)
	forecast := probability.EstimateTerminalForecastWithContext(
		currentPrice,
		priceToBeat,
		secondsRemaining,
		binanceClient.GetLogReturns(),
		probability.ForecastContext{
			VolatilityFloorPerSqrtS: macroFloor,
			BasisVolatilityPerSqrtS: basisVol,
			ModelUncertainty:        1.15,
		},
	)
	if !forecast.Ready {
		return nil
	}
	pUp := forecast.PAbove
	pDown := forecast.PBelow

	lastBids, lastAsks := binanceClient.GetLastBidsAndAsks()
	if len(lastBids) == 0 || len(lastAsks) == 0 {
		return nil
	}
	depth := CalculateDepthMetrics(lastBids, lastAsks, binanceClient.IsSpoofing)
	if depth.BestBid <= 0 || depth.BestAsk <= 0 || depth.FilteredBidVol+depth.FilteredAskVol <= 0 {
		return nil
	}

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
		Timestamp:                nowUTC,
		Question:                 market.Question,
		Slug:                     market.EventSlug,
		MarketEndTime:            market.EndTime.UTC().Format(time.RFC3339),
		PriceToBeat:              priceToBeat,
		CurrentPrice:             currentPrice,
		BinancePrice:             binanceSpot,
		ChainlinkBinanceBasisBps: basisBps,
		SpotMinusPriceToBeat:     currentPrice - priceToBeat,
		SecondsRemaining:         secondsRemaining,
		PUp:                      pUp,
		PDown:                    pDown,
		ForecastReady:            forecast.Ready,
		ForecastSamples:          forecast.Samples,
		ForecastPrice:            forecast.ForecastMedian,
		ForecastMeanPrice:        forecast.ForecastMean,
		ForecastLow68:            forecast.Lower68,
		ForecastHigh68:           forecast.Upper68,
		ForecastLow95:            forecast.Lower95,
		ForecastHigh95:           forecast.Upper95,
		PTBZ:                     forecast.TargetZ,
		RequiredMoveBps:          forecast.RequiredMoveBps,
		ExpectedMoveBps:          forecast.ExpectedMoveBps,
		ForecastSigmaExpiryBps:   forecast.SigmaAtExpiryBps,
		ForecastConfidence:       forecast.Confidence,
		MicroVolatilityAnnual:    forecast.MicroVolatilityAnnual,
		VolatilityFloorAnnual:    forecast.VolatilityFloorAnnual,
		BasisVolatilityAnnual:    forecast.BasisVolatilityAnnual,
		BidVol:                   depth.BidVol,
		AskVol:                   depth.AskVol,
		SpoofFilteredBidVol:      depth.FilteredBidVol,
		SpoofFilteredAskVol:      depth.FilteredAskVol,
		BidNotionalUSD:           depth.BidNotionalUSD,
		AskNotionalUSD:           depth.AskNotionalUSD,
		TotalDepthNotionalUSD:    depth.TotalNotionalUSD,
		BestBid:                  depth.BestBid,
		BestAsk:                  depth.BestAsk,
		WorstBid20:               depth.WorstBid,
		WorstAsk20:               depth.WorstAsk,
		DepthMidPrice:            depth.MidPrice,
		SpreadUSD:                depth.SpreadUSD,
		SpreadBps:                depth.SpreadBps,
		BidRangeUSD:              depth.BidRangeUSD,
		AskRangeUSD:              depth.AskRangeUSD,
		BidRangeBps:              depth.BidRangeBps,
		AskRangeBps:              depth.AskRangeBps,
		Imbalance:                depth.Imbalance,
		WeightedImbalance:        depth.WeightedImbalance,
		ProbabilityScore:         probabilityScore,
		OrderFlowScore:           orderFlowScore,
		TechnicalScore:           technicalScore,
		Volatility:               forecast.VolatilityAnnual,
		Drift:                    forecast.DriftAnnual,
		CompositeScore:           compositeScore,
		FinalScore:               finalScore,
		Decision:                 decision,
		Confidence:               confidence,
		Indicators:               indicators,
		MarketStale:              market.MarketStale,
		DataSource:               "CHAINLINK_RTDS+" + binanceClient.GetDataSource() + "+" + depthSource,
		DepthSource:              depthSource,
		DepthFresh:               true,
		DepthAgeMs:               depthAgeMs,
	}
}

func timeParseRFC3339(s string) (time.Time, error) { return timeParse(s) }
