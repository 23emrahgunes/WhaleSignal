package config

import (
	"os"
	"strconv"

	"github.com/joho/godotenv"
)

type Config struct {
	Port              string
	DBPath            string
	PolymarketPollSec int
	LogLevel          string
}

func LoadConfig() (*Config, error) {
	_ = godotenv.Load() // ignore error, fallback to system envs

	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}

	dbPath := os.Getenv("DB_PATH")
	if dbPath == "" {
		dbPath = "data/tv_direction.sqlite"
	}

	pollSecStr := os.Getenv("POLYMARKET_POLL_SEC")
	pollSec := 15
	if pollSecStr != "" {
		if p, err := strconv.Atoi(pollSecStr); err == nil {
			pollSec = p
		}
	}

	logLevel := os.Getenv("LOG_LEVEL")
	if logLevel == "" {
		logLevel = "info"
	}

	return &Config{
		Port:              port,
		DBPath:            dbPath,
		PolymarketPollSec: pollSec,
		LogLevel:          logLevel,
	}, nil
}
