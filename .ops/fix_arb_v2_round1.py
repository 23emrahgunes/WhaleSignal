from pathlib import Path

def replace_once(path, old, new):
    p=Path(path); s=p.read_text()
    if old not in s: raise SystemExit(f'marker missing {path}: {old[:100]!r}')
    p.write_text(s.replace(old,new,1))

# Existing synthetic tests use wall-clock timestamps while their simulated process
# clock is fixed. Real WS trades should not be accepted as future executions.
replace_once('internal/arb/paper.go',
'''func eventOrNow(t, now time.Time) time.Time {
	if t.IsZero() { return now.UTC() }
	return t.UTC()
}
''',
'''func eventOrNow(t, now time.Time) time.Time {
	now = now.UTC()
	if t.IsZero() || t.After(now.Add(time.Second)) {
		return now
	}
	return t.UTC()
}
''')

# Raw Engine no longer has enough evidence to declare a live candidate. The
# empirical completion model promotes it later in runtime after history is read.
replace_once('internal/arb/engine_test.go',
'''if s.StrategyMode != "SAFE_FIRST_SEQUENTIAL_MAKER" || s.UpMakerPrice != .41 || s.DownMakerPrice != .54 {''',
'''if s.StrategyMode != "COMPLETION_PROBABILITY_SAFE_FIRST_V2" || s.UpMakerPrice != .41 || s.DownMakerPrice != .54 {''')
replace_once('internal/arb/engine_test.go',
'''if !s.PaperEdgePass || !s.LiveEdgePass || s.Status != StatusCandidate {
		t.Fatalf("candidate %+v", s)
	}''',
'''if !s.PaperEdgePass || !s.LiveEdgePass || s.Status != StatusPaperCandidate || s.Reason != "AWAITING_COMPLETION_MODEL" {
		t.Fatalf("await completion model %+v", s)
	}''')

# PR31 renamed the card after the first Arb-v2 draft marker was written.
replace_once('web/static/index.html',
'''<h2>Maker Arbitraj — SAFE-FIRST Ters Bacak Motoru (Gölge)</h2>''',
'''<h2>Maker Completion Arb v2 — Queue + P(Tamamlama) + CycleEV</h2>''')
