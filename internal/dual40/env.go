package dual40

import (
	"os"
	"sort"
	"strconv"
	"strings"
)

func LoadConfigFromEnv() (Config, bool) {
	cfg := DefaultConfig()
	enabled := envBool("DUAL40_ENABLED", true)
	cfg.EntryPrice = envFloat("DUAL40_ENTRY_PRICE", cfg.EntryPrice)
	cfg.Shares = envFloat("DUAL40_SHARES", cfg.Shares)
	cfg.EntrySeconds = envIntList("DUAL40_ENTRY_WINDOWS", cfg.EntrySeconds)
	cfg.MinChopScore = envFloat("DUAL40_MIN_CHOP_SCORE", cfg.MinChopScore)
	cfg.MinRangeBps = envFloat("DUAL40_MIN_RANGE_BPS", cfg.MinRangeBps)
	cfg.MaxRangeBps = envFloat("DUAL40_MAX_RANGE_BPS", cfg.MaxRangeBps)
	cfg.MaxAbsDriftBps = envFloat("DUAL40_MAX_ABS_DRIFT_BPS", cfg.MaxAbsDriftBps)
	cfg.MaxAbsFlowImbalance = envFloat("DUAL40_MAX_ABS_FLOW_IMBALANCE", cfg.MaxAbsFlowImbalance)
	cfg.MaxPolySkew = envFloat("DUAL40_MAX_POLY_SKEW", cfg.MaxPolySkew)
	cfg.OrderTTLSec = envInt("DUAL40_ORDER_TTL_SEC", cfg.OrderTTLSec)
	cfg.HedgeMaxWaitSec = envInt("DUAL40_HEDGE_MAX_WAIT_SEC", cfg.HedgeMaxWaitSec)
	cfg.HedgeTriggerPrice = envFloat("DUAL40_HEDGE_TRIGGER_PRICE", cfg.HedgeTriggerPrice)
	cfg.StopBeforeEndSec = envInt("DUAL40_STOP_BEFORE_END_SEC", cfg.StopBeforeEndSec)
	cfg.GateMode = envStr("DUAL40_GATE_MODE", cfg.GateMode)
	cfg.MaxEntryDriftBps = envFloat("DUAL40_ENTRY_MAX_DRIFT_BPS", cfg.MaxEntryDriftBps)
	return NormalizeConfig(cfg), enabled
}

func envStr(key, fallback string) string {
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return fallback
	}
	return v
}

func envFloat(key string, fallback float64) float64 {
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return fallback
	}
	n, err := strconv.ParseFloat(v, 64)
	if err != nil || n <= 0 {
		return fallback
	}
	return n
}

func envInt(key string, fallback int) int {
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return fallback
	}
	n, err := strconv.Atoi(v)
	if err != nil || n <= 0 {
		return fallback
	}
	return n
}

func envBool(key string, fallback bool) bool {
	v := strings.ToLower(strings.TrimSpace(os.Getenv(key)))
	if v == "" {
		return fallback
	}
	return v == "1" || v == "true" || v == "yes" || v == "on"
}

func envIntList(key string, fallback []int) []int {
	raw := strings.TrimSpace(os.Getenv(key))
	if raw == "" {
		return append([]int(nil), fallback...)
	}
	seen := map[int]struct{}{}
	var out []int
	for _, part := range strings.Split(raw, ",") {
		n, err := strconv.Atoi(strings.TrimSpace(part))
		if err != nil || n <= 0 {
			continue
		}
		if _, ok := seen[n]; ok {
			continue
		}
		seen[n] = struct{}{}
		out = append(out, n)
	}
	if len(out) == 0 {
		return append([]int(nil), fallback...)
	}
	sort.Ints(out)
	return out
}
