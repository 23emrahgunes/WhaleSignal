from pathlib import Path


def rep(path, old, new):
    p=Path(path); s=p.read_text()
    if old not in s:
        raise SystemExit(f'pattern missing {path}: {old[:100]!r}')
    p.write_text(s.replace(old,new,1))

# config
p=Path('internal/config/config.go'); s=p.read_text()
s=s.replace('''\tPaperLatencyBuffer   float64\n\n\tPaperHedgeEnabled''','''\tPaperLatencyBuffer      float64\n\tPaperMaxEffectiveEntry  float64\n\tPaperMinEconomicEdge    float64\n\n\tPaperHedgeEnabled''')
s=s.replace('''\t\tPaperLatencyBuffer:        envFloat("PAPER_LATENCY_BUFFER", 0.002),\n\t\tPaperHedgeEnabled:''','''\t\tPaperLatencyBuffer:        envFloat("PAPER_LATENCY_BUFFER", 0.002),\n\t\tPaperMaxEffectiveEntry:    envFloat("PAPER_MAX_EFFECTIVE_ENTRY", 0.85),\n\t\tPaperMinEconomicEdge:      envFloat("PAPER_MIN_ECONOMIC_EDGE", 0.05),\n\t\tPaperHedgeEnabled:''')
p.write_text(s)

# paper engine config + production hard gate
p=Path('internal/paper/engine.go'); s=p.read_text()
s=s.replace('''\tTakerFeeRate  float64\n\tLatencyBuffer float64\n\n\tHedgeEnabled''','''\tTakerFeeRate       float64\n\tLatencyBuffer      float64\n\tMaxEffectiveEntry  float64\n\tMinEconomicEdge    float64\n\n\tHedgeEnabled''')
s=s.replace('''\tif cfg.LatencyBuffer < 0 {\n\t\tcfg.LatencyBuffer = 0\n\t}\n''','''\tif cfg.LatencyBuffer < 0 {\n\t\tcfg.LatencyBuffer = 0\n\t}\n\tif cfg.MaxEffectiveEntry <= 0 || cfg.MaxEffectiveEntry >= 1 {\n\t\tcfg.MaxEffectiveEntry = 0.85\n\t}\n\tif cfg.MinEconomicEdge <= 0 {\n\t\tcfg.MinEconomicEdge = 0.05\n\t}\n''')
s=s.replace('''\tentryPrice := 0.0\n\tstake := e.cfg.Stake\n\tshares := 0.0\n\tif quote != nil {''','''\tentryPrice := 0.0\n\tstake := e.cfg.Stake\n\tshares := 0.0\n\tentryProbability := res.PDown\n\tif res.Decision == "UP" {\n\t\tentryProbability = res.PUp\n\t}\n\tif quote != nil {\n\t\t// Production paper entries fail closed unless the direct PTB terminal\n\t\t// probability is available and agrees with the legacy direction.\n\t\tif !res.PTBTerminal.Ready || res.PTBTerminal.Decision != res.Decision {\n\t\t\treturn nil, false, nil\n\t\t}\n\t\tentryProbability = res.PTBTerminal.PBelow\n\t\tif res.Decision == "UP" {\n\t\t\tentryProbability = res.PTBTerminal.PAbove\n\t\t}\n''')
s=s.replace('''\tif entryPrice <= 0 || entryPrice >= 1 || stake <= 0 || shares <= 0 {\n\t\treturn nil, false, nil\n\t}\n\n\tentryProbability := res.PDown\n\tif res.Decision == "UP" {\n\t\tentryProbability = res.PUp\n\t}\n''','''\tif entryPrice <= 0 || entryPrice >= 1 || stake <= 0 || shares <= 0 {\n\t\treturn nil, false, nil\n\t}\n\tif quote != nil {\n\t\teffectiveCost := stake / shares\n\t\tif effectiveCost > e.cfg.MaxEffectiveEntry+1e-12 {\n\t\t\treturn nil, false, nil\n\t\t}\n\t\tif entryProbability-effectiveCost < e.cfg.MinEconomicEdge-1e-12 {\n\t\t\treturn nil, false, nil\n\t\t}\n\t}\n\n''')
p.write_text(s)

# gate observability and same hard logic
p=Path('internal/paper/gates.go'); s=p.read_text()
s=s.replace('''\tPositionExists   bool    `json:"positionExists"`\n}''','''\tPositionExists          bool    `json:"positionExists"`\n\tPTBTerminalReady        bool    `json:"ptbTerminalReady"`\n\tPTBTerminalDecision     string  `json:"ptbTerminalDecision"`\n\tPTBTerminalProbability  float64 `json:"ptbTerminalProbability"`\n\tPTBTerminalDirectionPass bool   `json:"ptbTerminalDirectionPass"`\n\tEffectiveCost           float64 `json:"effectiveCost"`\n\tMaxEffectiveEntry       float64 `json:"maxEffectiveEntry"`\n\tEffectivePricePass      bool    `json:"effectivePricePass"`\n\tEconomicEdge            float64 `json:"economicEdge"`\n\tMinEconomicEdge         float64 `json:"minEconomicEdge"`\n\tEconomicEdgePass        bool    `json:"economicEdgePass"`\n}''')
s=s.replace('''\tg := EntryGateSnapshot{Timeframe: storage.NormalizeTimeframe(e.cfg.Timeframe), Reason: "WAITING_FOR_DATA", MinConfidence: e.cfg.MinConfidence, MinSeconds: e.cfg.MinSecondsToEnd, MaxSeconds: e.cfg.MaxSecondsToEnd, Stake: e.cfg.Stake}''','''\tg := EntryGateSnapshot{Timeframe: storage.NormalizeTimeframe(e.cfg.Timeframe), Reason: "WAITING_FOR_DATA", MinConfidence: e.cfg.MinConfidence, MinSeconds: e.cfg.MinSecondsToEnd, MaxSeconds: e.cfg.MaxSecondsToEnd, Stake: e.cfg.Stake, MaxEffectiveEntry: e.cfg.MaxEffectiveEntry, MinEconomicEdge: e.cfg.MinEconomicEdge}''')
s=s.replace('''\tg.ConfidencePass = res.Confidence >= e.cfg.MinConfidence\n''','''\tg.PTBTerminalReady = res.PTBTerminal.Ready\n\tg.PTBTerminalDecision = res.PTBTerminal.Decision\n\tg.PTBTerminalProbability = res.PTBTerminal.PBelow\n\tif res.Decision == "UP" {\n\t\tg.PTBTerminalProbability = res.PTBTerminal.PAbove\n\t}\n\tif !g.PTBTerminalReady {\n\t\tg.Reason = "PTB_TERMINAL_NOT_READY"\n\t\treturn g\n\t}\n\tg.PTBTerminalDirectionPass = res.PTBTerminal.Decision == res.Decision\n\tif !g.PTBTerminalDirectionPass {\n\t\tg.Reason = "PTB_TERMINAL_DIRECTION_MISMATCH"\n\t\treturn g\n\t}\n\tg.ConfidencePass = res.Confidence >= e.cfg.MinConfidence\n''')
s=s.replace('''\tif !g.MinSharesPass {\n\t\tg.Reason = "MIN_ORDER_SIZE_NOT_MET"\n\t\treturn g\n\t}\n\tg.Allowed = true''','''\tif !g.MinSharesPass {\n\t\tg.Reason = "MIN_ORDER_SIZE_NOT_MET"\n\t\treturn g\n\t}\n\tg.EffectiveCost = q.TotalCost / q.Shares\n\tg.EffectivePricePass = g.EffectiveCost <= e.cfg.MaxEffectiveEntry+1e-12\n\tif !g.EffectivePricePass {\n\t\tg.Reason = "EFFECTIVE_ENTRY_PRICE_TOO_HIGH"\n\t\treturn g\n\t}\n\tg.EconomicEdge = g.PTBTerminalProbability - g.EffectiveCost\n\tg.EconomicEdgePass = g.EconomicEdge >= e.cfg.MinEconomicEdge-1e-12\n\tif !g.EconomicEdgePass {\n\t\tg.Reason = "ECONOMIC_EDGE_BELOW_THRESHOLD"\n\t\treturn g\n\t}\n\tg.Allowed = true''')
p.write_text(s)

# Wire config 5m/15m
for path in ['cmd/pm-edge/main.go','cmd/pm-edge/runtime15.go']:
    p=Path(path); s=p.read_text()
    s=s.replace('''\t\tLatencyBuffer:        cfg.PaperLatencyBuffer,\n\t\tHedgeEnabled:''','''\t\tLatencyBuffer:        cfg.PaperLatencyBuffer,\n\t\tMaxEffectiveEntry:    cfg.PaperMaxEffectiveEntry,\n\t\tMinEconomicEdge:      cfg.PaperMinEconomicEdge,\n\t\tHedgeEnabled:''')
    p.write_text(s)

# env example
p=Path('.env.example'); s=p.read_text()
s=s.replace('''PAPER_LATENCY_BUFFER=0.002\n''','''PAPER_LATENCY_BUFFER=0.002\n# Hard paper-entry economics: fee/latency-inclusive cost and PTB terminal edge\nPAPER_MAX_EFFECTIVE_ENTRY=0.85\nPAPER_MIN_ECONOMIC_EDGE=0.05\n''')
p.write_text(s)

# tests
p=Path('internal/paper/engine_test.go'); s=p.read_text()
append=r'''

func TestProductionEntryRequiresPTBTerminalAndEconomicEdge(t *testing.T) {
	tests := []struct {
		name       string
		terminal   engine.PTBTerminalEstimate
		cost       float64
		wantOpened bool
	}{
		{name: "good edge", terminal: engine.PTBTerminalEstimate{Ready: true, Decision: "UP", PAbove: .91, PBelow: .09}, cost: .80, wantOpened: true},
		{name: "terminal not ready", terminal: engine.PTBTerminalEstimate{Ready: false, Decision: "UP", PAbove: .95}, cost: .70},
		{name: "direction mismatch", terminal: engine.PTBTerminalEstimate{Ready: true, Decision: "DOWN", PAbove: .20, PBelow: .80}, cost: .70},
		{name: "price too expensive", terminal: engine.PTBTerminalEstimate{Ready: true, Decision: "UP", PAbove: .97, PBelow: .03}, cost: .86},
		{name: "edge too small", terminal: engine.PTBTerminalEstimate{Ready: true, Decision: "UP", PAbove: .83, PBelow: .17}, cost: .80},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			db, err := storage.NewDatabase(filepath.Join(t.TempDir(), "paper.sqlite"))
			if err != nil { t.Fatal(err) }
			defer db.Close()
			now := time.Now().UTC()
			market := &polymarket.Market{Question:"BTC", EventSlug:"btc-updown-5m-1786389900", Active:true, EndTime:now.Add(90*time.Second), Outcomes:[]string{"Up","Down"}, Tokens:[]polymarket.Token{{Outcome:"Up",Price:.5},{Outcome:"Down",Price:.5}}}
			res := &engine.EvaluationResult{PriceToBeat:64000, CurrentPrice:64010, SecondsRemaining:90, PUp:.8, PDown:.2, Decision:"UP", Confidence:70, DataSource:"CHAINLINK_RTDS+BINANCE_REST+BINANCE_REST_DEPTH20", PTBTerminal:tc.terminal}
			pe := NewEngine(db, Config{Timeframe:"5m", Enabled:true, InitialBalance:1000, Stake:2.5, MinConfidence:55, MinSecondsToEnd:30, MaxSecondsToEnd:240, MaxEffectiveEntry:.85, MinEconomicEdge:.05})
			quote := func(string, float64) (polymarket.BuyQuote,error) {
				shares := 2.5/tc.cost
				return polymarket.BuyQuote{BestAsk:tc.cost, AveragePrice:tc.cost, Shares:shares, TotalCost:2.5}, nil
			}
			trade, opened, err := pe.MaybeOpenWithQuote(res, market, now, quote)
			if err != nil { t.Fatal(err) }
			if opened != tc.wantOpened { t.Fatalf("opened=%v want=%v trade=%+v",opened,tc.wantOpened,trade) }
			if opened && math.Abs(trade.EntryProbability-tc.terminal.PAbove)>1e-9 { t.Fatalf("entry probability %.4f must store PTB terminal %.4f",trade.EntryProbability,tc.terminal.PAbove) }
		})
	}
}
'''
s += append
p.write_text(s)

# dashboard translations and gate details
p=Path('web/static/index.html'); s=p.read_text()
s=s.replace("'DATA_NOT_FRESH_OR_MARKET_INACTIVE':'Veri güncel değil veya piyasa aktif değil'", "'PTB_TERMINAL_NOT_READY':'PTB terminal kapanış olasılığı henüz hazır değil','PTB_TERMINAL_DIRECTION_MISMATCH':'PTB terminal yönü ana yön sinyaliyle uyuşmuyor','EFFECTIVE_ENTRY_PRICE_TOO_HIGH':'Efektif giriş maliyeti %85 üst sınırını aşıyor','ECONOMIC_EDGE_BELOW_THRESHOLD':'PTB terminal olasılığı giriş maliyetine göre en az %5 avantaj sağlamıyor','DATA_NOT_FRESH_OR_MARKET_INACTIVE':'Veri güncel değil veya piyasa aktif değil'")
old="gateRow('Güven skoru',`${Number(e.confidence||0).toFixed(1)} / ${Number(e.minConfidence||0).toFixed(1)}% ${boolChip(e.confidencePass)}`),gateRow('Kalan süre'"
new="gateRow('PTB terminal yönü',`${e.ptbTerminalDecision?directionText(e.ptbTerminalDecision):'—'} ${boolChip(e.ptbTerminalReady&&e.ptbTerminalDirectionPass)}`),gateRow('PTB terminal kazanma olasılığı',`${pct(e.ptbTerminalProbability||0)} ${boolChip(e.ptbTerminalReady)}`),gateRow('Güven skoru',`${Number(e.confidence||0).toFixed(1)} / ${Number(e.minConfidence||0).toFixed(1)}% ${boolChip(e.confidencePass)}`),gateRow('Kalan süre'"
if old not in s: raise SystemExit('dashboard gate anchor1 missing')
s=s.replace(old,new,1)
old="gateRow('Pay adedi / piyasa alış emri',e.estimatedShares?`${Number(e.estimatedShares).toFixed(3)} ${boolChip(e.quotePass)}`:'—'),gateRow('Ekonomik avantaj (gölge test)',(()=>{const lp=latestLiveData||{};const mp=e.decision==='UP'?Number(lp.pUp||0):e.decision==='DOWN'?Number(lp.pDown||0):0;const eff=Number(e.estimatedShares||0)>0?Number(e.totalCost||0)/Number(e.estimatedShares):0;const edge=mp&&eff?mp-eff:0;const el=document.getElementById('economicEdge');if(el)el.textContent=mp&&eff?`${pct(mp)} - ${pct(eff)} = ${pct(edge)}`:'—';return mp&&eff?`${pct(mp)} - ${pct(eff)} = ${pct(edge)} ${edge>0?chip('AVANTAJLI','fresh'):chip('AVANTAJ YOK','down')}`:'—'})())"
new="gateRow('Pay adedi / piyasa alış emri',e.estimatedShares?`${Number(e.estimatedShares).toFixed(3)} ${boolChip(e.quotePass)}`:'—'),gateRow('Efektif giriş maliyeti',e.effectiveCost?`${pct(e.effectiveCost)} / en fazla ${pct(e.maxEffectiveEntry)} ${boolChip(e.effectivePricePass)}`:'—'),gateRow('PTB ekonomik avantajı',e.effectiveCost?`${pct(e.ptbTerminalProbability)} - ${pct(e.effectiveCost)} = ${pct(e.economicEdge)} / en az ${pct(e.minEconomicEdge)} ${boolChip(e.economicEdgePass)}`:'—')"
if old not in s: raise SystemExit('dashboard gate anchor2 missing')
s=s.replace(old,new,1)
p.write_text(s)

print('PTB economic hard gate patch applied')
