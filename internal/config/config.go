package config

import (
	"os"
	"strconv"
	"strings"

	"github.com/joho/godotenv"
)

type Config struct {
	Port                   string
	DBPath                 string
	PolymarketPollSec      int
	LogLevel               string
	PaperEnabled           bool
	PaperInitialBalance    float64
	PaperStake             float64
	PaperMinConfidence     float64
	PaperMinSecondsToEnd   float64
	PaperMaxSecondsToEnd   float64
	PaperTakerFeeRate      float64
	PaperLatencyBuffer     float64
	PaperMaxEffectiveEntry float64
	PaperMinEconomicEdge   float64

	ArbShadowEnabled                bool
	ArbTargetEdge                   float64
	ArbOperationalBuffer            float64
	ArbUncertaintyPenalty           float64
	ArbMaxStrandedUnits             int
	ArbPaperMinEdge                 float64
	ArbMaxBookFetchMs               int
	ArbTradeStreamMaxAgeSec         int
	ArbPaperEnabled                 bool
	ArbPaperOrderTTLSec             int
	ArbPaperMaxStrandedSec          int
	ArbPaperStopBeforeEndSec        int
	ArbPaperSoftCompletionSec       int
	ArbCompletionLookback           int
	ArbCompletionMinSamples         int
	ArbCompletionMinStrandedSamples int
	ArbCompletionMinP5Lower         float64
	ArbCompletionMinCycleEV         float64
	ArbCompletionMaxLossMultiple    float64

	PaperHedgeEnabled         bool
	PaperHedgeWindow          int
	PaperHedgeMinVotes        int
	PaperHedgeMinConsecutive  int
	PaperHedgeScoreThreshold  float64
	PaperHedgeMinProbability  float64
	PaperHedgeMinEdge         float64
	PaperHedgeMinAbsPTBZ      float64
	PaperHedgeMinSecondsToEnd float64
	PaperHedgeMaxSecondsToEnd float64
}

func LoadConfig() (*Config, error) {
	_ = godotenv.Load()
	port := envString("PORT", "8080")
	dbPath := envString("DB_PATH", "data/tv_direction.sqlite")
	pollSec := envInt("POLYMARKET_POLL_SEC", 15)
	if pollSec < 1 {
		pollSec = 1
	}
	return &Config{
		Port:                            port,
		DBPath:                          dbPath,
		PolymarketPollSec:               pollSec,
		LogLevel:                        envString("LOG_LEVEL", "info"),
		PaperEnabled:                    envBool("PAPER_ENABLED", true),
		PaperInitialBalance:             envFloat("PAPER_INITIAL_BALANCE", 1000),
		PaperStake:                      envFloat("PAPER_STAKE", 2.50),
		PaperMinConfidence:              envFloat("PAPER_MIN_CONFIDENCE", 55),
		PaperMinSecondsToEnd:            envFloat("PAPER_MIN_SECONDS_TO_END", 30),
		PaperMaxSecondsToEnd:            envFloat("PAPER_MAX_SECONDS_TO_END", 240),
		PaperTakerFeeRate:               envFloat("PAPER_TAKER_FEE_RATE", 0.07),
		PaperLatencyBuffer:              envFloat("PAPER_LATENCY_BUFFER", 0.002),
		PaperMaxEffectiveEntry:          envFloat("PAPER_MAX_EFFECTIVE_ENTRY", 0.85),
		PaperMinEconomicEdge:            envFloat("PAPER_MIN_ECONOMIC_EDGE", 0.05),
		ArbShadowEnabled:                envBool("ARB_SHADOW_ENABLED", true),
		ArbTargetEdge:                   envFloat("ARB_TARGET_EDGE", 0.02),
		ArbOperationalBuffer:            envFloat("ARB_OPERATIONAL_BUFFER", 0.002),
		ArbUncertaintyPenalty:           envFloat("ARB_UNCERTAINTY_PENALTY", 0.02),
		ArbMaxStrandedUnits:             envInt("ARB_MAX_STRANDED_UNITS", 1),
		ArbPaperMinEdge:                 envFloat("ARB_PAPER_MIN_EDGE", 0.002),
		ArbMaxBookFetchMs:               envInt("ARB_MAX_BOOK_FETCH_MS", 1000),
		ArbTradeStreamMaxAgeSec:         envInt("ARB_TRADE_STREAM_MAX_AGE_SEC", 20),
		ArbPaperEnabled:                 envBool("ARB_PAPER_ENABLED", true),
		ArbPaperOrderTTLSec:             envInt("ARB_PAPER_ORDER_TTL_SEC", 12),
		ArbPaperMaxStrandedSec:          envInt("ARB_PAPER_MAX_STRANDED_SEC", 5),
		ArbPaperStopBeforeEndSec:        envInt("ARB_PAPER_STOP_BEFORE_END_SEC", 12),
		ArbPaperSoftCompletionSec:       envInt("ARB_PAPER_SOFT_COMPLETION_SEC", 2),
		ArbCompletionLookback:           envInt("ARB_COMPLETION_LOOKBACK", 500),
		ArbCompletionMinSamples:         envInt("ARB_COMPLETION_MIN_SAMPLES", 30),
		ArbCompletionMinStrandedSamples: envInt("ARB_COMPLETION_MIN_STRANDED_SAMPLES", 3),
		ArbCompletionMinP5Lower:         envFloat("ARB_COMPLETION_MIN_P5_LOWER", 0.70),
		ArbCompletionMinCycleEV:         envFloat("ARB_COMPLETION_MIN_CYCLE_EV", 0.01),
		ArbCompletionMaxLossMultiple:    envFloat("ARB_COMPLETION_MAX_LOSS_MULTIPLE", 4.0),
		PaperHedgeEnabled:               envBool("PAPER_HEDGE_ENABLED", true),
		PaperHedgeWindow:                envInt("PAPER_HEDGE_WINDOW", 8),
		PaperHedgeMinVotes:              envInt("PAPER_HEDGE_MIN_VOTES", 6),
		PaperHedgeMinConsecutive:        envInt("PAPER_HEDGE_MIN_CONSECUTIVE", 3),
		PaperHedgeScoreThreshold:        envFloat("PAPER_HEDGE_SCORE_THRESHOLD", 0.35),
		PaperHedgeMinProbability:        envFloat("PAPER_HEDGE_MIN_PROBABILITY", 0.65),
		PaperHedgeMinEdge:               envFloat("PAPER_HEDGE_MIN_EDGE", 0.03),
		PaperHedgeMinAbsPTBZ:            envFloat("PAPER_HEDGE_MIN_ABS_PTB_Z", 0.50),
		PaperHedgeMinSecondsToEnd:       envFloat("PAPER_HEDGE_MIN_SECONDS_TO_END", 20),
		PaperHedgeMaxSecondsToEnd:       envFloat("PAPER_HEDGE_MAX_SECONDS_TO_END", 120),
	}, nil
}

func envString(key, fallback string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return fallback
}

func envInt(key string, fallback int) int {
	if v, err := strconv.Atoi(strings.TrimSpace(os.Getenv(key))); err == nil && v > 0 {
		return v
	}
	return fallback
}

func envFloat(key string, fallback float64) float64 {
	if v, err := strconv.ParseFloat(strings.TrimSpace(os.Getenv(key)), 64); err == nil && v > 0 {
		return v
	}
	return fallback
}

func envBool(key string, fallback bool) bool {
	v := strings.ToLower(strings.TrimSpace(os.Getenv(key)))
	if v == "" {
		return fallback
	}
	return v == "1" || v == "true" || v == "yes" || v == "on"
}
