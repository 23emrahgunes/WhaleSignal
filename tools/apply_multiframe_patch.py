from pathlib import Path


def replace(path, old, new, count=1):
    p = Path(path)
    s = p.read_text()
    if old not in s:
        raise SystemExit(f"pattern not found in {path}: {old[:120]!r}")
    p.write_text(s.replace(old, new, count))


# storage: accept 5m and 15m canonical BTC up/down markets.
replace("internal/storage/sqlite.go", "if r.SecondsRemaining <= 0 || r.SecondsRemaining > 305 {", "if r.SecondsRemaining <= 0 || r.SecondsRemaining > 905 {")
replace("internal/storage/sqlite.go", 'if !strings.HasPrefix(r.Slug, "btc-updown-5m-") {\n\t\treturn fmt.Errorf("refusing non-canonical BTC 5m slug %q", r.Slug)\n\t}', 'if !IsSupportedBTCMarketSlug(r.Slug) {\n\t\treturn fmt.Errorf("refusing unsupported BTC up/down slug %q", r.Slug)\n\t}')
replace("internal/storage/sqlite.go", 'if !strings.HasPrefix(t.MarketSlug, "btc-updown-5m-") {\n\t\treturn false, fmt.Errorf("invalid paper market slug %q", t.MarketSlug)\n\t}', 'if !IsSupportedBTCMarketSlug(t.MarketSlug) {\n\t\treturn false, fmt.Errorf("invalid paper market slug %q", t.MarketSlug)\n\t}')
replace("internal/storage/hedge.go", 'if h == nil || h.PaperTradeID <= 0 || !strings.HasPrefix(h.MarketSlug, "btc-updown-5m-") {', 'if h == nil || h.PaperTradeID <= 0 || !IsSupportedBTCMarketSlug(h.MarketSlug) {')

# paper engine: each timeframe has its own logical balance and settlement scope.
replace("internal/paper/engine.go", "type Config struct {\n\tEnabled", "type Config struct {\n\tTimeframe string\n\tEnabled")
replace("internal/paper/engine.go", "\treturn &Engine{db: db, cfg: cfg, regimes: make(map[string][]regimeSample)}", "\tcfg.Timeframe = storage.NormalizeTimeframe(cfg.Timeframe)\n\treturn &Engine{db: db, cfg: cfg, regimes: make(map[string][]regimeSample)}")
replace("internal/paper/engine.go", "\tif !e.Enabled() || res == nil || market == nil {\n\t\treturn nil, false, nil\n\t}\n\te.mu.Lock()", "\tif !e.Enabled() || res == nil || market == nil {\n\t\treturn nil, false, nil\n\t}\n\tif storage.TimeframeFromMarketSlug(market.EventSlug) != storage.NormalizeTimeframe(e.cfg.Timeframe) {\n\t\treturn nil, false, nil\n\t}\n\te.mu.Lock()", 1)
replace("internal/paper/engine.go", "stats, err := e.db.GetPaperStats(e.cfg.InitialBalance)", "stats, err := e.db.GetPaperStatsByTimeframe(e.cfg.InitialBalance, e.cfg.Timeframe)")
replace("internal/paper/engine.go", "\tif !e.Enabled() || !e.cfg.HedgeEnabled || res == nil || market == nil || quote == nil {\n\t\treturn nil, false, nil\n\t}\n\te.mu.Lock()", "\tif !e.Enabled() || !e.cfg.HedgeEnabled || res == nil || market == nil || quote == nil {\n\t\treturn nil, false, nil\n\t}\n\tif storage.TimeframeFromMarketSlug(market.EventSlug) != storage.NormalizeTimeframe(e.cfg.Timeframe) {\n\t\treturn nil, false, nil\n\t}\n\te.mu.Lock()")
replace("internal/paper/engine.go", "openTrades, err := e.db.GetOpenPaperTrades()", "openTrades, err := e.db.GetOpenPaperTradesByTimeframe(e.cfg.Timeframe)")

# API server becomes timeframe aware while preserving 5m default compatibility.
replace("internal/api/server.go", "\tcurrentResult       *engine.EvaluationResult\n\tcurrentMarket       *polymarket.Market", "\tcurrentResults      map[string]*engine.EvaluationResult\n\tcurrentMarkets      map[string]*polymarket.Market\n\tgates               map[string]gateState")
replace("internal/api/server.go", "\treturn &Server{db: db, paperInitialBalance: initial}", "\treturn &Server{db: db, paperInitialBalance: initial, currentResults: make(map[string]*engine.EvaluationResult), currentMarkets: make(map[string]*polymarket.Market), gates: make(map[string]gateState)}")
replace("internal/api/server.go", "func (s *Server) UpdateState(res *engine.EvaluationResult, market *polymarket.Market) {\n\ts.mu.Lock()\n\tdefer s.mu.Unlock()\n\ts.currentResult = res\n\ts.currentMarket = market\n}", "func (s *Server) UpdateState(res *engine.EvaluationResult, market *polymarket.Market) {\n\ts.UpdateStateFor(\"5m\", res, market)\n}")
replace("internal/api/server.go", '\tmux.HandleFunc("/api/paper/hedge/stats", s.cors(s.handlePaperHedgeStats))', '\tmux.HandleFunc("/api/paper/hedge/stats", s.cors(s.handlePaperHedgeStats))\n\tmux.HandleFunc("/api/gates", s.cors(s.handleGates))\n\tmux.HandleFunc("/api/comparison", s.cors(s.handleComparison))')
replace("internal/api/server.go", "func (s *Server) handleLive(w http.ResponseWriter, r *http.Request) {\n\ts.mu.RLock()\n\tres := s.currentResult\n\ts.mu.RUnlock()", "func (s *Server) handleLive(w http.ResponseWriter, r *http.Request) {\n\ttf := normalizeTF(r)\n\ts.mu.RLock()\n\tres := s.currentResults[tf]\n\ts.mu.RUnlock()")
replace("internal/api/server.go", "history, err := s.db.GetHistory(limit)", "history, err := s.db.GetHistoryByTimeframe(limit, normalizeTF(r))")
replace("internal/api/server.go", "func (s *Server) handleMarket(w http.ResponseWriter, r *http.Request) {\n\ts.mu.RLock()\n\tm := s.currentMarket\n\ts.mu.RUnlock()", "func (s *Server) handleMarket(w http.ResponseWriter, r *http.Request) {\n\ttf := normalizeTF(r)\n\ts.mu.RLock()\n\tm := s.currentMarkets[tf]\n\ts.mu.RUnlock()")
replace("internal/api/server.go", "func (s *Server) handleOrderflow(w http.ResponseWriter, r *http.Request) {\n\ts.mu.RLock()\n\tres := s.currentResult\n\ts.mu.RUnlock()", "func (s *Server) handleOrderflow(w http.ResponseWriter, r *http.Request) {\n\ttf := normalizeTF(r)\n\ts.mu.RLock()\n\tres := s.currentResults[tf]\n\ts.mu.RUnlock()")
replace("internal/api/server.go", "stats, err := s.db.GetPaperStats(s.paperInitialBalance)", "stats, err := s.db.GetTimeframeStats(s.paperInitialBalance, normalizeTF(r))")
replace("internal/api/server.go", "trades, err := s.db.GetPaperTrades(limit)", "trades, err := s.db.GetPaperTradesByTimeframe(limit, normalizeTF(r))")
replace("internal/api/server.go", "rows, err := s.db.GetPaperHedges(limit)", "rows, err := s.db.GetPaperHedgesByTimeframe(limit, normalizeTF(r))")
replace("internal/api/server.go", "stats, err := s.db.GetPaperHedgeStats()", "stats, err := s.db.GetPaperHedgeStatsByTimeframe(normalizeTF(r))")

# 5m runtime gets a timeframe tag, live gate diagnostics and starts the 15m runtime.
replace("cmd/pm-edge/main.go", "\tpaperEngine := paper.NewEngine(db, paper.Config{\n\t\tEnabled:", "\tpaperEngine := paper.NewEngine(db, paper.Config{\n\t\tTimeframe:            \"5m\",\n\t\tEnabled:")
replace("cmd/pm-edge/main.go", "\tevaluator := engine.NewEvaluator()\n\tstate := &marketState{}", "\tevaluator := engine.NewEvaluator()\n\tstate := &marketState{}\n\tstartBTC15mRuntime(ctx, isMockMode, cfg, db, server, pmClient, bClient, clClient)")
replace("cmd/pm-edge/main.go", "\t\t\t\tif m == nil {\n\t\t\t\t\tserver.UpdateState(nil, nil)\n\t\t\t\t\tcontinue\n\t\t\t\t}", "\t\t\t\tif m == nil {\n\t\t\t\t\tserver.UpdateState(nil, nil)\n\t\t\t\t\tserver.UpdateGatesFor(\"5m\", paperEngine.EntryGateSnapshot(nil, nil, now, quoteBudget), paperEngine.HedgeGateSnapshot(nil, nil, now, quoteShares))\n\t\t\t\t\tcontinue\n\t\t\t\t}")
replace("cmd/pm-edge/main.go", "\t\t\t\tif res == nil {\n\t\t\t\t\tserver.UpdateState(nil, m)\n\t\t\t\t\tcontinue\n\t\t\t\t}", "\t\t\t\tif res == nil {\n\t\t\t\t\tserver.UpdateState(nil, m)\n\t\t\t\t\tserver.UpdateGatesFor(\"5m\", paperEngine.EntryGateSnapshot(nil, m, now, quoteBudget), paperEngine.HedgeGateSnapshot(nil, m, now, quoteShares))\n\t\t\t\t\tcontinue\n\t\t\t\t}")
replace("cmd/pm-edge/main.go", "\t\t\t\tif isMockMode || strings.Contains(res.DataSource, \"MOCK\") {\n\t\t\t\t\tcontinue\n\t\t\t\t}", "\t\t\t\tif isMockMode || strings.Contains(res.DataSource, \"MOCK\") {\n\t\t\t\t\tserver.UpdateGatesFor(\"5m\", paperEngine.EntryGateSnapshot(res, m, now, quoteBudget), paperEngine.HedgeGateSnapshot(res, m, now, quoteShares))\n\t\t\t\t\tcontinue\n\t\t\t\t}")
replace("cmd/pm-edge/main.go", '\t\t\t\tutil.Logger.Info("Evaluated directional bias score",', '\t\t\t\tserver.UpdateGatesFor("5m", paperEngine.EntryGateSnapshot(res, m, now, quoteBudget), paperEngine.HedgeGateSnapshot(res, m, now, quoteShares))\n\t\t\t\tutil.Logger.Info("Evaluated directional bias score",')

# Dashboard selector + gate panels + statistical comparison.
p = Path("web/static/index.html")
s = p.read_text()
s = s.replace('.positive{color:var(--green)}.negative{color:var(--red)}.muted{color:var(--muted)}#connection', '.positive{color:var(--green)}.negative{color:var(--red)}.muted{color:var(--muted)}.tfbar{display:flex;justify-content:center;gap:10px;margin-bottom:18px}.tfbtn{background:var(--panel2);color:var(--muted);border:1px solid var(--line);border-radius:8px;padding:10px 22px;font-weight:800;cursor:pointer}.tfbtn.active{background:#17345d;color:#b9d8ff;border-color:#3778c8}.gategrid{display:grid;grid-template-columns:1fr 1fr;gap:18px}.gate-row{display:flex;justify-content:space-between;gap:12px;padding:8px 0;border-bottom:1px solid #202c41;font-size:12px}.gate-row span:first-child{color:#9db4d5}#connection')
s = s.replace('BTC 5m · Chainlink Reference · Conservative Terminal Forecast · Binance Depth20 · CLOB Paper Execution · Shadow Hedge A/B', 'BTC 5m + 15m · Chainlink Reference · Conservative Terminal Forecast · Binance Depth20 · CLOB Paper Execution · Shadow Hedge A/B')
s = s.replace('<div class="wrap">', '<div class="wrap">\n  <div class="tfbar"><button class="tfbtn active" data-tf="5m" onclick="switchTf(\'5m\')">BTC 5 MIN</button><button class="tfbtn" data-tf="15m" onclick="switchTf(\'15m\')">BTC 15 MIN</button></div>')
gate_html = '''
  <div class="gategrid">
    <div class="card"><h2>Paper Entry Gate Monitor — <span id="entryTf">5m</span></h2><div id="entryGateBody"><div class="gate-row"><span>Status</span><b>Waiting...</b></div></div></div>
    <div class="card"><h2>Hedge Gate Monitor — <span id="hedgeTf">5m</span></h2><div id="hedgeGateBody"><div class="gate-row"><span>Status</span><b>Waiting...</b></div></div></div>
  </div>
  <div class="card"><h2>BTC 5m vs 15m — Paper Efficiency Experiment</h2>
    <div class="grid3">
      <div class="mini"><span>5m: N / Win Rate</span><strong id="cmp5n">—</strong></div><div class="mini"><span>5m: Return on Stake</span><strong id="cmp5roi">—</strong></div><div class="mini"><span>5m: Avg Return ± SE</span><strong id="cmp5avg">—</strong></div>
      <div class="mini"><span>15m: N / Win Rate</span><strong id="cmp15n">—</strong></div><div class="mini"><span>15m: Return on Stake</span><strong id="cmp15roi">—</strong></div><div class="mini"><span>15m: Avg Return ± SE</span><strong id="cmp15avg">—</strong></div>
      <div class="mini"><span>5m Brier / Cal Gap</span><strong id="cmp5cal">—</strong></div><div class="mini"><span>15m Brier / Cal Gap</span><strong id="cmp15cal">—</strong></div><div class="mini"><span>Inference</span><strong id="cmpInference">Collecting...</strong></div>
    </div>
  </div>
'''
s = s.replace('  <div class="card">\n    <h2>Paper Portfolio — Original Strategy A</h2>', gate_html + '\n  <div class="card">\n    <h2>Paper Portfolio — Original Strategy A</h2>')
s = s.replace("let hasLiveSnapshot=false;", "let activeTf='5m';\nlet hasLiveSnapshot=false;")
s = s.replace("const data=await getJSON('/api/live');", "const data=await getJSON('/api/live?tf='+activeTf);")
s = s.replace("getJSON('/api/history?limit=20')", "getJSON('/api/history?limit=20&tf='+activeTf)")
s = s.replace("getJSON('/api/paper/stats')", "getJSON('/api/paper/stats?tf='+activeTf)")
s = s.replace("getJSON('/api/paper/trades?limit=50')", "getJSON('/api/paper/trades?limit=50&tf='+activeTf)")
s = s.replace("getJSON('/api/paper/hedge/stats')", "getJSON('/api/paper/hedge/stats?tf='+activeTf)")
s = s.replace("getJSON('/api/paper/hedges?limit=30')", "getJSON('/api/paper/hedges?limit=30&tf='+activeTf)")
old_tick = "async function tickLive(){if(liveBusy)return;liveBusy=true;try{await updateLive();setConnection(true)}catch(e){console.error(e);setConnection(false)}finally{liveBusy=false}}\nasync function tickSlow(){if(slowBusy)return;slowBusy=true;try{await Promise.all([updateHistory(),updatePaper(),updateHedge()])}catch(e){console.error(e)}finally{slowBusy=false}}"
new_tick = r'''function boolChip(v){return v?chip('PASS','fresh'):chip('BLOCK','down')}
function gateRow(k,v){return `<div class="gate-row"><span>${k}</span><b>${v}</b></div>`}
async function updateGates(){
  const g=await getJSON('/api/gates?tf='+activeTf);if(g.status)return;
  document.getElementById('entryTf').textContent=activeTf;document.getElementById('hedgeTf').textContent=activeTf;
  const e=g.entry||{};
  document.getElementById('entryGateBody').innerHTML=[gateRow('ENTRY',e.allowed?chip('READY','fresh'):chip(e.reason||'BLOCKED','warn')),gateRow('Direction',`${e.decision||'—'} ${boolChip(e.directionPass)}`),gateRow('Confidence',`${Number(e.confidence||0).toFixed(1)} / ${Number(e.minConfidence||0).toFixed(1)}% ${boolChip(e.confidencePass)}`),gateRow('Time',`${Math.round(e.secondsRemaining||0)}s / ${Math.round(e.minSeconds||0)}-${Math.round(e.maxSeconds||0)}s ${boolChip(e.timePass)}`),gateRow('Fresh market/data',boolChip(e.freshPass)),gateRow('Paper balance',`${usd(e.cashBalance)} / stake ${usd(e.stake)} ${boolChip(e.balancePass)}`),gateRow('CLOB ask / VWAP',e.bestAsk?`${Number(e.bestAsk).toFixed(3)} / ${Number(e.averagePrice||0).toFixed(3)}`:'—'),gateRow('Shares / minimum',e.estimatedShares?`${Number(e.estimatedShares).toFixed(3)} / ${Number(e.minOrderSize||0).toFixed(3)} ${boolChip(e.minSharesPass)}`:'—')].join('');
  const h=g.hedge||{};
  document.getElementById('hedgeGateBody').innerHTML=[gateRow('HEDGE',h.allowed?chip('READY','fresh'):chip(h.reason||'BLOCKED','warn')),gateRow('Open A position',boolChip(h.hasOpenPosition)),gateRow('Original → Reverse',`${h.originalSide||'—'} → ${h.reverseSide||'—'} ${boolChip(h.decisionPass)}`),gateRow('Reverse votes',`${h.reverseVotes||0}/${h.windowSize||0} · min ${h.minVotes||0}`),gateRow('Consecutive',`${h.consecutive||0} / ${h.minConsecutive||0}`),gateRow('P(reverse)',`${pct(h.reverseProbability||0)} / min ${pct(h.minProbability||0)} ${boolChip(h.probabilityPass)}`),gateRow('EWMA score',`${Number(h.smoothedScore||0).toFixed(3)} / ±${Number(h.scoreThreshold||0).toFixed(2)} ${boolChip(h.scorePass)}`),gateRow('PTB Z',`${Number(h.ptbZ||0).toFixed(2)}σ / ±${Number(h.minAbsPtbZ||0).toFixed(2)}σ ${boolChip(h.ptbZPass)}`),gateRow('Edge',`${pct(h.edge||0)} / min ${pct(h.minEdge||0)} ${boolChip(h.edgePass)}`),gateRow('Expected improve',`${usd(h.expectedImprovement||0)} ${boolChip(h.improvementPass)}`)].join('');
}
async function updateComparison(){
 const c=await getJSON('/api/comparison'),a=c.fiveMinute||{},b=c.fifteenMinute||{};
 document.getElementById('cmp5n').textContent=`${a.settledTrades||0} / ${Number(a.winRate||0).toFixed(1)}%`;document.getElementById('cmp5roi').textContent=`${Number(a.returnOnStakePct||0).toFixed(2)}%`;document.getElementById('cmp5avg').textContent=`${Number(a.averageReturnPct||0).toFixed(2)}% ± ${Number(a.returnSePct||0).toFixed(2)}%`;
 document.getElementById('cmp15n').textContent=`${b.settledTrades||0} / ${Number(b.winRate||0).toFixed(1)}%`;document.getElementById('cmp15roi').textContent=`${Number(b.returnOnStakePct||0).toFixed(2)}%`;document.getElementById('cmp15avg').textContent=`${Number(b.averageReturnPct||0).toFixed(2)}% ± ${Number(b.returnSePct||0).toFixed(2)}%`;
 document.getElementById('cmp5cal').textContent=`${Number(a.brierScore||0).toFixed(3)} / ${pct(a.calibrationGap||0)}`;document.getElementById('cmp15cal').textContent=`${Number(b.brierScore||0).toFixed(3)} / ${pct(b.calibrationGap||0)}`;document.getElementById('cmpInference').textContent=`${c.status||'collecting'} · leader ${c.leader||'none'} · z=${Number(c.zScore||0).toFixed(2)}`;
}
function switchTf(tf){activeTf=tf==='15m'?'15m':'5m';hasLiveSnapshot=false;document.querySelectorAll('.tfbtn').forEach(b=>b.classList.toggle('active',b.dataset.tf===activeTf));markLiveWaiting();tickLive();tickSlow()}
async function tickLive(){if(liveBusy)return;liveBusy=true;try{await Promise.all([updateLive(),updateGates()]);setConnection(true)}catch(e){console.error(e);setConnection(false)}finally{liveBusy=false}}
async function tickSlow(){if(slowBusy)return;slowBusy=true;try{await Promise.all([updateHistory(),updatePaper(),updateHedge(),updateComparison()])}catch(e){console.error(e)}finally{slowBusy=false}}'''
if old_tick not in s:
    raise SystemExit("dashboard tick block not found")
s = s.replace(old_tick, new_tick)
p.write_text(s)
