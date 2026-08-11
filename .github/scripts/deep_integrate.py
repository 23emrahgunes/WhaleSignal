from pathlib import Path


def replace(path: str, old: str, new: str) -> None:
    p = Path(path)
    s = p.read_text()
    if old not in s:
        raise SystemExit(f"missing replacement marker in {path}: {old[:120]!r}")
    p.write_text(s.replace(old, new, 1))


replace(
    "internal/binance/microstructure.go",
    """\tfor i := len(rows) - 1; i >= 0; i-- {\n\t\tif rows[i].Time.Before(cutoff) {\n\t\t\tbreak\n\t\t}\n\t\tbuy += rows[i].BuyUSD\n\t\tsell += rows[i].SellUSD\n\t}""",
    """\tfor i := len(rows) - 1; i >= 0; i-- {\n\t\tif rows[i].Time.Before(cutoff) {\n\t\t\tcontinue\n\t\t}\n\t\tbuy += rows[i].BuyUSD\n\t\tsell += rows[i].SellUSD\n\t}""",
)

replace(
    "internal/engine/microstructure.go",
    "0.35*trade(5) + 0.30*trade(15) + 0.20*trade(30) + 0.15*trade(60) + 0.10*s.TradeAcceleration",
    "0.30*trade(5) + 0.25*trade(15) + 0.20*trade(30) + 0.15*trade(60) + 0.10*s.TradeAcceleration",
)

replace(
    "cmd/pm-edge/main.go",
    """\tif err := db.EnsurePaperHedgeSchema(); err != nil {\n\t\tutil.Logger.Fatal(\"Paper hedge schema setup failed\", zap.Error(err))\n\t}""",
    """\tif err := db.EnsurePaperHedgeSchema(); err != nil {\n\t\tutil.Logger.Fatal(\"Paper hedge schema setup failed\", zap.Error(err))\n\t}\n\tif err := db.EnsureMicrostructureSchema(); err != nil {\n\t\tutil.Logger.Fatal(\"Deep microstructure schema setup failed\", zap.Error(err))\n\t}""",
)
replace("cmd/pm-edge/main.go", "if err := db.InsertSignal(res); err != nil {", "if err := db.InsertSignalWithMicro(res); err != nil {")
replace("cmd/pm-edge/runtime15.go", "if err := db.InsertSignal(res); err != nil {", "if err := db.InsertSignalWithMicro(res); err != nil {")

p = Path("internal/storage/micro_write.go")
s = p.read_text()
s = s.replace("""\tif err := d.EnsureMicrostructureSchema(); err != nil {\n\t\treturn err\n\t}\n\treturn d.InsertMicrostructureSnapshot(r)""", """\treturn d.InsertMicrostructureSnapshot(r)""")
p.write_text(s)

p = Path("internal/storage/microstructure.go")
s = p.read_text().replace('\t"database/sql"\n', '').replace("\nvar _ *sql.DB\n", "\n")
p.write_text(s)

replace(
    "internal/api/server.go",
    '\tmux.HandleFunc("/api/orderflow", s.cors(s.handleOrderflow))',
    '\tmux.HandleFunc("/api/orderflow", s.cors(s.handleOrderflow))\n\tmux.HandleFunc("/api/microstructure", s.cors(s.handleMicrostructure))\n\tmux.HandleFunc("/api/microstructure/history", s.cors(s.handleMicrostructureHistory))',
)

p = Path("web/static/index.html")
s = p.read_text()
s = s.replace("Binance Depth20 · CLOB Paper Execution", "Binance Deep Microstructure · CLOB Paper Execution")
s = s.replace("<h2>Order Flow Statistics</h2>", "<h2>Legacy Depth20 — Model A Order-Book Input</h2>")
panel = r'''
  <div class="card">
    <h2>Binance Deep Microstructure — Shadow Model B</h2>
    <div class="banner" style="margin:-4px 0 14px;border-radius:7px;border:1px solid #80500a"><b>SHADOW ONLY</b> — Paper entries still follow Model A until out-of-sample evidence promotes Model B.</div>
    <div class="grid4">
      <div class="mini"><span>Deep Book Health</span><strong id="deepHealth">WAITING</strong></div>
      <div class="mini"><span>Shadow B Direction</span><strong id="shadowDecision">WAITING</strong></div>
      <div class="mini"><span>Shadow B Score / Confidence</span><strong id="shadowScore">—</strong></div>
      <div class="mini"><span>Microstructure Score</span><strong id="microScore">—</strong></div>
      <div class="mini"><span>±$10 Bid / Ask / Imb.</span><strong id="deep10">—</strong></div>
      <div class="mini"><span>±$25 Bid / Ask / Imb.</span><strong id="deep25">—</strong></div>
      <div class="mini"><span>±$50 Bid / Ask / Imb.</span><strong id="deep50">—</strong></div>
      <div class="mini"><span>±$75 Bid / Ask / Imb.</span><strong id="deep75">—</strong></div>
      <div class="mini"><span>Agg Trade Flow 5s</span><strong id="flow5">—</strong></div>
      <div class="mini"><span>Agg Trade Flow 15s</span><strong id="flow15">—</strong></div>
      <div class="mini"><span>Agg Trade Flow 30s</span><strong id="flow30">—</strong></div>
      <div class="mini"><span>Agg Trade Flow 60s</span><strong id="flow60">—</strong></div>
      <div class="mini"><span>Deep / Trade Scores</span><strong id="deepTradeScores">—</strong></div>
      <div class="mini"><span>Walls / Depletion</span><strong id="wallDynamics">—</strong></div>
      <div class="mini"><span>PTB Path / Barrier</span><strong id="ptbPath">—</strong></div>
      <div class="mini"><span>Entry Economic Edge</span><strong id="economicEdge">—</strong></div>
    </div>
  </div>
'''
marker = '\n\n  <div class="gategrid">'
if marker not in s:
    raise SystemExit("dashboard panel marker missing")
s = s.replace(marker, "\n" + panel + marker, 1)
s = s.replace("let activeTf='5m';\nlet hasLiveSnapshot=false;", "let activeTf='5m';\nlet latestLiveData=null;\nlet hasLiveSnapshot=false;")
s = s.replace("  hasLiveSnapshot=true;\n  document.getElementById('currentPrice').textContent=usd(data.currentPrice);", "  hasLiveSnapshot=true;\n  latestLiveData=data;\n  document.getElementById('currentPrice').textContent=usd(data.currentPrice);")

deepjs = r'''
  const dm=data.deepMicrostructure||{};
  const bands=Object.fromEntries((dm.bands||[]).map(x=>[Number(x.distanceUsd),x]));
  const flows=Object.fromEntries((dm.trades||[]).map(x=>[Number(x.seconds),x]));
  const fmtBand=d=>{const x=bands[d];return x?`${usdCompact(x.bidUsd)} / ${usdCompact(x.askUsd)} / ${pct(x.imbalance)}`:'—'};
  const fmtFlow=d=>{const x=flows[d];return x?`${usdCompact(x.buyUsd)} / ${usdCompact(x.sellUsd)} / ${pct(x.imbalance)}`:'—'};
  document.getElementById('deepHealth').innerHTML=dm.ready?chip(`SYNC · ${dm.bidLevels||0}/${dm.askLevels||0} · ${dm.ageMs||0}ms`,'fresh'):chip(`${dm.source||'WAITING'} · ${dm.ageMs??'—'}ms`,'warn');
  document.getElementById('shadowDecision').innerHTML=decisionChip(data.shadowDecision||'WAITING');
  document.getElementById('shadowScore').textContent=`${Number(data.shadowModelBScore||0).toFixed(3)} / ${Number(data.shadowConfidence||0).toFixed(1)}%`;
  document.getElementById('microScore').textContent=pct(data.microstructureScore||0,1);
  for(const d of [10,25,50,75])document.getElementById('deep'+d).textContent=fmtBand(d);
  for(const d of [5,15,30,60])document.getElementById('flow'+d).textContent=fmtFlow(d);
  document.getElementById('deepTradeScores').textContent=`${pct(data.deepBookScore||0)} / ${pct(data.tradeFlowScore||0)}`;
  document.getElementById('wallDynamics').textContent=`${pct(data.wallDynamicsScore||0)} · B ${pct(dm.bidWallScore||0)} / A ${pct(dm.askWallScore||0)}`;
  document.getElementById('ptbPath').textContent=`B ${usdCompact(dm.ptbPathBidUsd||0)} / A ${usdCompact(dm.ptbPathAskUsd||0)} · ${pct(data.ptbBarrierScore||0)}`;
'''
marker = "  const box=document.getElementById('indicatorBody');"
if marker not in s:
    raise SystemExit("updateLive marker missing")
s = s.replace(marker, deepjs + "\n" + marker, 1)
old = "gateRow('CLOB ask / VWAP',e.bestAsk?`${Number(e.bestAsk).toFixed(3)} / ${Number(e.averagePrice||0).toFixed(3)}`:'—'),gateRow('Shares / minimum',e.estimatedShares?`${Number(e.estimatedShares).toFixed(3)} / ${Number(e.minOrderSize||0).toFixed(3)} ${boolChip(e.minSharesPass)}`:'—')"
new = "gateRow('CLOB ask / VWAP',e.bestAsk?`${Number(e.bestAsk).toFixed(3)} / ${Number(e.averagePrice||0).toFixed(3)}`:'—'),gateRow('Shares / market BUY',e.estimatedShares?`${Number(e.estimatedShares).toFixed(3)} ${boolChip(e.quotePass)}`:'—'),gateRow('Economic Edge (shadow)',(()=>{const lp=latestLiveData||{};const mp=e.decision==='UP'?Number(lp.pUp||0):e.decision==='DOWN'?Number(lp.pDown||0):0;const eff=Number(e.estimatedShares||0)>0?Number(e.totalCost||0)/Number(e.estimatedShares):0;const edge=mp&&eff?mp-eff:0;const el=document.getElementById('economicEdge');if(el)el.textContent=mp&&eff?`${pct(mp)} - ${pct(eff)} = ${pct(edge)}`:'—';return mp&&eff?`${pct(mp)} - ${pct(eff)} = ${pct(edge)} ${edge>0?chip('POSITIVE','fresh'):chip('NEGATIVE','down')}`:'—'})())"
if old not in s:
    raise SystemExit("entry gate economic marker missing")
s = s.replace(old, new, 1)
p.write_text(s)
