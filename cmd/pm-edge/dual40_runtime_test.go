package main

import (
	"path/filepath"
	"testing"

	"pm-edge/internal/config"
	"pm-edge/internal/polymarket"
	"pm-edge/internal/storage"
)

func TestDual40AttachedOnlyTo5mRuntime(t *testing.T) {
	db, err := storage.NewDatabase(filepath.Join(t.TempDir(), "dual40-runtime.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	cfg := &config.Config{}
	pm := polymarket.NewClient()

	five := newArbShadowRuntime("5m", cfg, db, pm, false)
	if five.dual40 == nil {
		t.Fatal("expected Dual40 runtime on 5m")
	}

	fifteen := newArbShadowRuntime("15m", cfg, db, pm, false)
	if fifteen.dual40 != nil {
		t.Fatal("Dual40 must not attach to 15m runtime")
	}
}
