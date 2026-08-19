"""P2.5 SHADOW dashboard, paper scorecard and JSON state API."""
from __future__ import annotations

import asyncio

from aiohttp import web


async def run_web(engine, cfg, stop: asyncio.Event) -> None:  # noqa: ANN001
    app = web.Application()

    async def index(_request: web.Request) -> web.Response:
        return web.Response(text=_HTML, content_type="text/html")

    async def state(_request: web.Request) -> web.Response:
        return web.json_response(engine.snapshot())

    async def health(_request: web.Request) -> web.Response:
        snapshot = engine.snapshot()
        return web.json_response(
            {
                "ok": True,
                "mode": "SHADOW",
                "phase": snapshot.get("phase"),
                "markets_active": snapshot.get("footer", {}).get(
                    "markets_active"
                ),
                "paper_trading_enabled": snapshot.get("safety", {}).get(
                    "paper_trading_enabled", False
                ),
                "live_orders": snapshot.get("safety", {}).get(
                    "live_orders", 0
                ),
                "execution_enabled": snapshot.get("safety", {}).get(
                    "execution_enabled", False
                ),
            }
        )

    app.add_routes(
        [
            web.get("/", index),
            web.get("/api/state", state),
            web.get("/health", health),
        ]
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.web_host, cfg.web_port)
    await site.start()
    try:
        await stop.wait()
    finally:
        await runner.cleanup()


_HTML = r"""<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Direction Engine P2.5 — SHADOW + Paper</title>
<style>
:root{--bg:#080d17;--panel:#10192a;--line:#22314a;--tx:#eef3fb;--mut:#91a6c6;--blue:#57a2ff;--green:#18cb8d;--red:#ef5d62;--amber:#efb44c;--purple:#b18cff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:13px Inter,Segoe UI,Arial,sans-serif}
header{position:sticky;top:0;z-index:2;background:#0a101c;border-bottom:1px solid var(--line);padding:12px 18px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
h1{font-size:18px;margin:0;color:var(--blue)}h2{margin:0 0 10px;font-size:17px}h3{margin:0 0 8px;font-size:14px;color:#ddebff}.pill{padding:4px 9px;border-radius:6px;background:#17345d;color:#c9deff;font-weight:800}.mut{color:var(--mut)}
.wrap{max-width:1780px;margin:auto;padding:14px}.banner{padding:10px 12px;border:1px solid #695019;background:#291f08;color:#ffe0a1;border-radius:8px;margin-bottom:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(380px,1fr));gap:11px}.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px}.dead{opacity:.45}
.card h2{font-size:15px;color:var(--blue);margin:0 0 8px;display:flex;justify-content:space-between;gap:8px;align-items:center}.tags{display:flex;gap:5px;flex-wrap:wrap;justify-content:flex-end}.tag{padding:3px 7px;border-radius:5px;font-size:10px;font-weight:800;white-space:nowrap}.UP{background:#09684c;color:#a8f7dc}.DOWN{background:#7d2428;color:#ffd0d1}.ABSTAIN,.NEUTRAL{background:#354158;color:#dfe7f4}.VALIDATED{outline:1px solid var(--green)}.PROVISIONAL{outline:1px solid var(--amber)}.CONFLICTED,.LIMITED{outline:1px solid var(--red)}
.row{display:flex;justify-content:space-between;gap:10px;border-bottom:1px solid #1b2740;padding:3px 0}.row span{color:var(--mut)}.row b{text-align:right}.mono{font-family:ui-monospace,Consolas,monospace;font-size:11px}
.forecast{border:1px solid #315888;background:#0d1b31;border-radius:8px;padding:8px;margin:7px 0}.forecast .hero{display:flex;justify-content:space-between;align-items:center;font-size:15px;font-weight:800}.forecast .prob{font-size:18px;color:#fff}.forecast small{color:#9db1d0}.paperline{border:1px solid #4c3e17;background:#211b0b;border-radius:7px;padding:7px;margin:6px 0}
.qs{display:flex;gap:3px;flex-wrap:wrap;margin:5px 0}.q{padding:2px 5px;border-radius:4px;font-size:10px;font-weight:700}.q-OK{background:#0b5a3e;color:#a1f2d4}.q-WARN{background:#5b4907;color:#ffe8a6}.q-FAIL{background:#711c21;color:#ffc8ca}.q-WAITING{background:#29364f;color:#a9bddb}
.good{color:var(--green)}.bad{color:var(--red)}.amb{color:var(--amber)}.purple{color:var(--purple)}.why{color:#9db1d0;font-size:11px;margin-top:7px;min-height:26px}
.foot{display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:7px;margin-top:12px}.metric{background:#0d1524;border:1px solid var(--line);border-radius:7px;padding:7px}.metric b{display:block;font-size:15px;color:#ddebff}
.section{margin-top:16px;background:#0b1220;border:1px solid var(--line);border-radius:11px;padding:13px}.tables{display:grid;grid-template-columns:repeat(auto-fit,minmax(390px,1fr));gap:12px;margin-top:12px}.tablebox{background:#0d1524;border:1px solid var(--line);border-radius:8px;padding:10px;overflow:hidden}.tablewrap{overflow:auto;max-height:440px}table{width:100%;border-collapse:collapse;min-width:650px}th,td{text-align:right;padding:7px 8px;border-bottom:1px solid #1d2b43;white-space:nowrap}th:first-child,td:first-child{text-align:left}th{position:sticky;top:0;background:#111c2e;color:#9eb4d3;font-size:11px}td{font-size:12px}.hit{color:var(--green);font-weight:800}.miss{color:var(--red);font-weight:800}.open{color:var(--amber);font-weight:800}.skip{color:#9aaac2}.pnlpos{color:var(--green)}.pnlneg{color:var(--red)}
@media(max-width:700px){.grid{grid-template-columns:1fr}.card{padding:9px}.tables{grid-template-columns:1fr}.wrap{padding:8px}}
</style>
</head>
<body>
<header>
<h1>Direction Engine — P2.5</h1>
<span class="pill" id="phase">SHADOW</span>
<span class="pill" id="paperpill">PAPER</span>
<span class="mut" id="conn">bağlanıyor…</span>
<span class="mut" id="up"></span>
</header>
<div class="wrap">
<div class="banner"><b>TAHMİN</b> research ensemble’dır. <b>SİNYAL</b> yalnız doğrulama geçince açılır. <b>PAPER TRADE</b> seçilen tarafı gerçek best ask + slippage ile simüle eder; emir, imza ve private key yoktur.</div>
<div class="grid" id="grid"></div>
<div class="foot" id="foot"></div>

<section class="section">
<h2>Paper Trade — Genel Durum</h2>
<div class="foot" id="paperSummary"></div>
<div class="tables">
  <div class="tablebox"><h3>Kripto Bazlı Sonuç</h3><div class="tablewrap"><table><thead><tr><th>Kripto</th><th>Trade</th><th>Settled</th><th>W/L</th><th>Hit</th><th>PnL</th><th>ROI</th></tr></thead><tbody id="assetPaper"></tbody></table></div></div>
  <div class="tablebox"><h3>Market / Timeframe Bazlı Sonuç</h3><div class="tablewrap"><table><thead><tr><th>Market</th><th>Trade</th><th>Settled</th><th>W/L</th><th>Hit</th><th>PnL</th><th>ROI</th></tr></thead><tbody id="comboPaper"></tbody></table></div></div>
</div>
<div class="tables">
  <div class="tablebox"><h3>Tahmin Doğruluğu — Kripto Bazlı (tüm checkpointler)</h3><div class="tablewrap"><table><thead><tr><th>Kripto</th><th>N</th><th>Directional</th><th>Accuracy</th><th>Brier</th><th>Durum</th></tr></thead><tbody id="assetForecast"></tbody></table></div></div>
  <div class="tablebox"><h3>Paper Skip Nedenleri</h3><div class="tablewrap"><table><thead><tr><th>Neden</th><th>Adet</th></tr></thead><tbody id="paperSkips"></tbody></table></div></div>
</div>
</section>

<section class="section">
<h2>Market Bazlı Paper İşlemler</h2>
<div class="tablebox"><div class="tablewrap"><table><thead><tr><th>Market</th><th>Zaman</th><th>Taraf</th><th>Giriş</th><th>Tahmin</th><th>Sonuç</th><th>Tuttu mu?</th><th>PnL</th><th>Durum</th></tr></thead><tbody id="recentPaper"></tbody></table></div></div>
</section>
</div>
<script>
const $=id=>document.getElementById(id);
const n=(v,d=3)=>v==null?'—':Number(v).toFixed(d);
const pc=v=>v==null?'—':(Number(v)*100).toFixed(1)+'%';
const usd=v=>v==null?'—':(Number(v)>=0?'+':'')+'$'+Number(v).toFixed(3);
function qchip(k,v){return `<span class="q q-${v||'WAITING'}">${k}:${v||'?'}</span>`}
function paperLine(c){
 const p=c.paper_trade;
 if(!p)return `<div class="paperline"><div class="row"><span>paper trade</span><b class="amb">T-${c.paper_entry_checkpoint||'—'} bekleniyor</b></div></div>`;
 if(p.status==='OPEN')return `<div class="paperline"><div class="row"><span>paper trade</span><b class="open">OPEN ${p.side} @ ${n(p.fill_price)} · $${n(p.stake_usdc,2)}</b></div><div class="row"><span>forecast edge</span><b>${pc(p.forecast_edge)}</b></div></div>`;
 if(p.status==='SETTLED')return `<div class="paperline"><div class="row"><span>paper trade</span><b class="${p.correct?'hit':'miss'}">${p.correct?'TUTTU':'KAÇTI'} · ${p.side} / sonuç ${p.official_result}</b></div><div class="row"><span>paper PnL</span><b class="${Number(p.realized_pnl)>=0?'pnlpos':'pnlneg'}">${usd(p.realized_pnl)}</b></div></div>`;
 return `<div class="paperline"><div class="row"><span>paper trade</span><b class="skip">SKIPPED · ${p.skip_reason||'—'}</b></div></div>`;
}
function card(c){
 if(!c.active)return `<div class="card dead"><h2>${c.combo}<span class="tag ABSTAIN">NO MARKET</span></h2></div>`;
 const q=c.quality||{};
 const qs=[['T',q.time],['M',q.market],['Tk',q.tokens],['C',q.clob],['R',q.reference],['Ck',q.clock],['Md',q.model]].map(x=>qchip(x[0],x[1])).join('');
 const f=c.feature||{};
 const signal=c.signal_decision||c.decision||'ABSTAIN';
 const forecast=c.forecast_direction||'NEUTRAL';
 const status=c.forecast_status||'NO_DATA';
 const grade=c.forecast_grade||'LOW';
 const fp=c.forecast_p_up==null?'—':pc(c.forecast_p_up);
 const components=(c.forecast_components||[]).sort((a,b)=>Math.abs(b.contribution||0)-Math.abs(a.contribution||0)).slice(0,4).map(x=>`${x.name}:${Number(x.contribution||0)>=0?'+':''}${n(x.contribution,3)}`).join(' · ');
 return `<div class="card">
 <h2>${c.combo}<span class="tags"><span class="tag ${forecast} ${status}">TAHMİN ${forecast}</span><span class="tag ${signal}">SİNYAL ${signal}</span></span></h2>
 <div class="qs">${qs}</div>
 <div class="forecast">
   <div class="hero"><span>Research tahmini</span><span class="prob ${forecast==='UP'?'good':forecast==='DOWN'?'bad':'amb'}">${forecast} · P(UP) ${fp}</span></div>
   <div class="row"><span>güven / sınıf / durum</span><b>${pc(c.forecast_confidence)} · ${grade} · ${status}</b></div>
   <div class="row"><span>uzlaşma / model olgunluğu</span><b>${pc(c.forecast_agreement)} / ${pc(c.forecast_model_maturity)}</b></div>
   <small>${components||'bileşen bekleniyor'}</small>
 </div>
 ${paperLine(c)}
 <div class="row mono"><span>market / TTE</span><b>${c.market_id} · ${n(c.tte_sec,0)}s</b></div>
 <div class="row"><span>PTB / distance</span><b>${n(c.official_reference_open,4)} · ${n(c.distance_bps,2)}bps</b></div>
 <div class="row"><span>UP / DOWN mid</span><b>${n(c.up_mid)} / ${n(c.down_mid)}</b></div>
 <div class="row"><span>feature ready / coverage</span><b class="${f.ready?'good':'amb'}">${f.ready?'READY':'WARMUP'} · ${pc(f.coverage)}</b></div>
 <div class="row"><span>history / ret 1s·15s·60s</span><b>${n(f.history_sec,0)}s · ${n(f.ret_1s_bps,2)}/${n(f.ret_15s_bps,2)}/${n(f.ret_60s_bps,2)}bps</b></div>
 <div class="row"><span>flow / persist / flip</span><b>${n(f.flow_5s,2)} · ${n(f.momentum_persist,2)} · ${n(f.flip_rate,2)}</b></div>
 <div class="row"><span>OBI / OFI / PTB z</span><b>${n(f.obi20,2)} / ${n(f.ofi,2)} / ${n(f.ptb_z,2)}</b></div>
 <div class="row"><span>regime / predictability</span><b>${c.regime||'—'} · ${pc(c.predictability)}</b></div>
 <div class="row"><span>conflict / consensus</span><b>${n(c.conflict_score,2)} / ${n(c.directional_consensus,2)}</b></div>
 <div class="row"><span>P(UP) B2 raw→cal</span><b>${pc(c.p_up_raw)} → ${pc(c.p_up)}</b></div>
 <div class="row"><span>B1 / PTB / market</span><b>${pc(c.p_up_external)} / ${pc(c.p_up_ptb)} / ${pc(c.p_up_market)}</b></div>
 <div class="row"><span>validated signal / gate</span><b class="${signal==='ABSTAIN'?'amb':signal==='UP'?'good':'bad'}">${signal} · ${c.decision_gate||c.abstain_reason||'—'}</b></div>
 <div class="row"><span>threshold / calibration</span><b>${pc(c.threshold)} · ${c.threshold_source||'—'} · ${c.calibration_source||'—'}</b></div>
 <div class="why">${(c.forecast_reasons||[]).join(' · ')}</div>
 </div>`;
}
function metricsHtml(items){return items.map(x=>`<div class="metric"><b>${x[1]==null?'—':x[1]}</b>${x[0]}</div>`).join('')}
function groupRows(groups){
 const entries=Object.entries(groups||{});
 if(!entries.length)return `<tr><td colspan="7" class="mut">Henüz paper trade yok</td></tr>`;
 return entries.map(([key,m])=>`<tr><td>${key}</td><td>${m.trades||0}</td><td>${m.settled||0}</td><td>${m.wins||0}/${m.losses||0}</td><td>${pc(m.hit_rate)}</td><td class="${Number(m.realized_pnl_usdc||0)>=0?'pnlpos':'pnlneg'}">${usd(m.realized_pnl_usdc||0)}</td><td>${pc(m.roi)}</td></tr>`).join('');
}
function forecastAssetRows(groups){
 const entries=Object.entries(groups||{});
 if(!entries.length)return `<tr><td colspan="6" class="mut">Etiketli tahmin bekleniyor</td></tr>`;
 return entries.map(([key,v])=>{const m=(v||{}).research_forecast||{};return `<tr><td>${key}</td><td>${m.n||0}</td><td>${m.n_directional||0}</td><td>${pc(m.accuracy)}</td><td>${n(m.brier,4)}</td><td>${m.insufficient?'YETERSİZ N':'ÖLÇÜLÜYOR'}</td></tr>`}).join('');
}
function skipRows(skips){
 const entries=Object.entries(skips||{}).sort((a,b)=>b[1]-a[1]);
 if(!entries.length)return `<tr><td colspan="2" class="mut">Skip yok</td></tr>`;
 return entries.map(([key,value])=>`<tr><td>${key}</td><td>${value}</td></tr>`).join('');
}
function recentRows(rows){
 if(!(rows||[]).length)return `<tr><td colspan="9" class="mut">İlk canonical paper giriş checkpoint’i bekleniyor</td></tr>`;
 return rows.map(r=>{
  const date=r.attempted_at?new Date(r.attempted_at*1000).toLocaleTimeString('tr-TR'):'—';
  const result=r.official_result||'BEKLİYOR';
  const correctness=r.status==='SETTLED'?(r.correct?'<span class="hit">✅ TUTTU</span>':'<span class="miss">❌ KAÇTI</span>'):(r.status==='OPEN'?'<span class="open">AÇIK</span>':'<span class="skip">—</span>');
  const status=r.status==='SKIPPED'?`SKIPPED: ${r.skip_reason||'—'}`:r.status;
  return `<tr><td>${r.combo_key}<br><span class="mono mut">${(r.slug||'').slice(-26)}</span></td><td>${date}</td><td>${r.side||'—'}</td><td>${n(r.fill_price)}</td><td>P(UP) ${pc(r.forecast_p_up)}<br>${r.forecast_grade||'—'} / ${r.forecast_status||'—'}</td><td>${result}</td><td>${correctness}</td><td class="${Number(r.realized_pnl||0)>=0?'pnlpos':'pnlneg'}">${r.realized_pnl==null?'—':usd(r.realized_pnl)}</td><td>${status}</td></tr>`;
 }).join('');
}
async function tick(){
 let d;
 try{const r=await fetch('/api/state',{cache:'no-store'});d=await r.json()}catch(e){$('conn').textContent='API yok';return}
 $('phase').textContent=(d.mode||'SHADOW')+' · '+(d.phase||'');
 const paper=d.paper_trading||{};
 $('paperpill').textContent=paper.enabled?'PAPER ON':'PAPER OFF';
 $('conn').innerHTML='Binance '+(d.binance_connected?'<span class="good">bağlı</span>':'<span class="bad">yok</span>')+' · clock '+(d.clock_synced?'<span class="good">sync</span>':'<span class="bad">UNSYNC</span>');
 $('up').textContent='uptime '+Math.round(d.uptime_sec||0)+'s';
 $('grid').innerHTML=(d.cards||[]).map(card).join('');
 const f=d.footer||{},s=d.safety||{},a=(d.forecast_analytics||{}).overall||{},rf=a.research_forecast||{};
 $('foot').innerHTML=metricsHtml([
 ['active',f.markets_active],['PTB',f.ptb_states_healthy],['CLOB',f.clob_quote_healthy],['features ready',f.features_ready],
 ['tahmin UP',f.forecast_up_cards],['tahmin DOWN',f.forecast_down_cards],['HIGH tahmin',f.forecast_high_grade_cards],['provisional',f.forecast_provisional_cards],
 ['validated signals',f.validated_decision_cards],['resolved',f.resolved_total],['tahmin accuracy',rf.accuracy==null?'—':pc(rf.accuracy)],['tahmin Brier',rf.brier],
 ['paper hit',f.paper_hit_rate==null?'—':pc(f.paper_hit_rate)],['paper PnL',usd(f.paper_realized_pnl_usdc||0)],['orders',s.live_orders],['execution',s.execution_enabled?'ON':'OFF']
 ]);
 const po=paper.overall||{};
 $('paperSummary').innerHTML=metricsHtml([
 ['başlangıç',po.starting_bankroll_usdc==null?'—':'$'+n(po.starting_bankroll_usdc,2)],['equity',po.equity_usdc==null?'—':'$'+n(po.equity_usdc,2)],['kullanılabilir',po.available_bankroll_usdc==null?'—':'$'+n(po.available_bankroll_usdc,2)],['açık risk',po.open_exposure_usdc==null?'—':'$'+n(po.open_exposure_usdc,2)],
 ['attempts',po.attempts],['trade',po.trades],['open',po.open],['settled',po.settled],['skipped',po.skipped],['W / L',`${po.wins||0} / ${po.losses||0}`],['hit rate',pc(po.hit_rate)],['realized PnL',usd(po.realized_pnl_usdc||0)],['ROI',pc(po.roi)],['coverage',pc(po.coverage)]
 ]);
 $('assetPaper').innerHTML=groupRows(paper.per_asset);
 $('comboPaper').innerHTML=groupRows(paper.per_combo);
 $('assetForecast').innerHTML=forecastAssetRows((d.forecast_analytics||{}).per_asset);
 $('paperSkips').innerHTML=skipRows(paper.skip_reasons);
 $('recentPaper').innerHTML=recentRows(paper.recent_markets);
}
setInterval(tick,1500);tick();
</script>
</body>
</html>"""
