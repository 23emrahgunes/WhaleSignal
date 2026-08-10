package config

import (
	"os"
	"strconv"
	"strings"

	"github.com/joho/godotenv"
)

type Config struct {
	Port                 string
	DBPath               string
	PolymarketPollSec    int
	LogLevel             string
	PaperEnabled         bool
	PaperInitialBalance  float64
	PaperStake           float64
	PaperMinConfidence   float64
	PaperMinSecondsToEnd float64
	PaperMaxSecondsToEnd float64
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
		Port:                 port,
		DBPath:               dbPath,
		PolymarketPollSec:    pollSec,
		LogLevel:             envString("LOG_LEVEL", "info"),
		PaperEnabled:         envBool("PAPER_ENABLED", true),
		PaperInitialBalance:  envFloat("PAPER_INITIAL_BALANCE", 1000),
		PaperStake:           envFloat("PAPER_STAKE", 2.50),
		PaperMinConfidence:   envFloat("PAPER_MIN_CONFIDENCE", 55),
		PaperMinSecondsToEnd: envFloat("PAPER_MIN_SECONDS_TO_END", 30),
		PaperMaxSecondsToEnd: envFloat("PAPER_MAX_SECONDS_TO_END", 240),
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
