"""Web dashboard (aiohttp) — P1 acceptance formati (SHADOW).

Her kart: debug kimligi (market/slug/token'lar), canonical TTE, reference/PTB, UP+DOWN
bid/ask/mid (0.505 fallback YOK), ayrisik yaslar (transport/source/book/clob), 7 boyut
quality + prediction_ready, karar+sebep. Footer: P1 metrikleri + SUSPICIOUS_IDENTICAL_QUOTES.
"""
from __future__ import annotations

import asyncio
import logging

from aiohttp import web

log = logging.getLogger("direction_engine.web")


async def run_web(engine, cfg, stop: asyncio.Event) -> None:  # noqa: ANN001
    app = web.Application()

    async def index(_req: web.Request) -> web.Response:
        return web.Response(text=_HTML, content_type="text/html")

    async def state(_req: web.Request) -> web.Response:
        return web.json_response(engine.snapshot())

    async def health(_req: web.Request) -> web.Response:
        return web.json_response({"ok": True, "mode": "SHADOW", "phase": "P1-hardened"})

    app.add_routes(
        [web.get("/", index), web.get("/api/state", state), web.get("/health", health)]
    )

    aiorunner = web.AppRunner(app)
    await aiorunner.setup()
    site = web.TCPSite(aiorunner, cfg.web_host, cfg.web_port)
    await site.start()
    log.info("web dashboard: http://%s:%d", cfg.web_host, cfg.web_port)
    try:
        await stop.wait()
    finally:
        await aiorunner.cleanup()
        log.info("web dashboard durdu")


_HTML = """<!DOCTYPE html>
<html lang="tr"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Direction Engine — P1 Data Plumbing</title>
<style>
:root{--bg:#0a0f1a;--panel:#111a2b;--panel2:#0d1524;--line:#22304a;--tx:#eef2f8;--mut:#8ea3c4;--blue:#4b9cff;--grn:#16c88a;--red:#ef4d4d;--amb:#f2a93b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font-family:Inter,Segoe UI,Arial,sans-serif}
header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:12px;flex-wrap:wrap;background:#0a101c}
h1{margin:0;font-size:18px;color:var(--blue)}
.pill{padding:3px 9px;border-radius:6px;font-size:12px;font-weight:800;background:#17345d;color:#bcd7ff}
.muted{color:var(--mut);font-size:12px}
.wrap{max-width:1500px;margin:auto;padding:14px}
.banner{background:#2a1d05;border:1px solid #6b4c0d;color:#ffe0a1;padding:9px 13px;border-radius:8px;margin-bottom:12px;font-size:13px}
.sus{background:#3a0d0d;border-color:#7a1414;color:#ffc4c4}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:11px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:12px;font-size:12px}
.card.dead{opacity:.5}
.card h2{margin:0 0 6px;font-size:14px;color:var(--blue);display:flex;justify-content:space-between;align-items:center;gap:6px}
.dtag{padding:2px 8px;border-radius:5px;font-size:11px;font-weight:800}
.d-UP{background:#087b58;color:#9ff6d8}.d-DOWN{background:#8f2020;color:#ffc0c0}.d-ABSTAIN{background:#39465b;color:#d5deed}
.qrow{display:flex;gap:3px;margin:6px 0;flex-wrap:wrap}
.q{padding:2px 5px;border-radius:4px;font-size:10px;font-weight:700}
.q-OK{background:#0c5c3f;color:#8ff0cf}.q-WARN{background:#5a4a05;color:#ffe7a1}.q-FAIL{background:#7a1414;color:#ffc4c4}.q-WAITING{background:#2a3550;color:#9db4d5}
.pr-y{color:var(--grn);font-weight:800}.pr-n{color:var(--amb);font-weight:800}
.row{display:flex;justify-content:space-between;padding:2px 0;border-bottom:1px solid #1a2540}
.row span{color:var(--mut)}.row b{font-weight:700}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:11px}
.pos{color:var(--grn)}.neg{color:var(--red)}.neu{color:var(--mut)}
.why{margin-top:6px;font-size:10px;color:#9db4d5;min-height:14px}
.dstat{font-size:10px;padding:2px 6px;border-radius:4px;background:#16233c;color:#8fb4e6}
.foot{margin-top:12px;font-size:12px;color:var(--mut);display:grid;grid-template-columns:repeat(auto-fill,minmax:180px);gap:6px}
.foot div{background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:6px 9px;display:inline-block;margin:2px}
</style></head><body>
<header>
  <h1>Direction Engine — P1 Veri Plumbing</h1>
  <span class="pill" id="mode">SHADOW · P1</span>
  <span class="muted" id="conn">baglaniyor...</span>
  <span class="muted" id="clock"></span>
  <span class="muted" id="uptime"></span>
</header>
<div class="wrap">
  <div class="banner sus" id="sus" style="display:none"></div>
  <div class="banner" id="banner" style="display:none"></div>
  <div class="grid" id="grid"></div>
  <div id="foot" class="foot"></div>
  <div class="muted" id="stamp" style="margin-top:8px"></div>
</div>
<script>
const $=id=>document.getElementById(id);
function n(v,d){return v==null?'—':Number(v).toFixed(d)}
function cls(v){return v>0?'pos':v<0?'neg':'neu'}
function qchip(k,v){return `<span class="q q-${v}">${k}:${v}</span>`}
function card(c){
  const dead=c.active?'':' dead';
  const dec=c.decision||'ABSTAIN';
  if(!c.active){
    return `<div class="card${dead}"><h2>${c.combo}<span class="dstat">${c.discovery_status||'NOT_FOUND'}</span><span class="dtag d-ABSTAIN">${dec}</span></h2>
      <div class="why">${(c.why||[]).join(' · ')}</div></div>`;
  }
  const Q=c.quality||{};
  const qrow=['time','market','tokens','clob','reference','clock','model']
     .map(k=>qchip(k[0].toUpperCase()+(k=='tokens'?'k':k=='clock'?'k':''),Q[k]||'?')).join('');
  const pr=c.prediction_ready?'<span class="pr-y">READY</span>':'<span class="pr-n">NOT READY</span>';
  return `<div class="card${dead}">
    <h2>${c.combo} <span class="dstat">${c.time_status||''}</span><span class="dtag d-${dec}">${dec}</span></h2>
    <div class="qrow">${qrow} · pred:${pr}</div>
    <div class="row mono"><span>market/slug</span><b>${c.market_id} · ${(c.slug||'').slice(-22)}</b></div>
    <div class="row mono"><span>UP tok / DOWN tok</span><b>${c.up_token} / ${c.down_token}</b></div>
    <div class="row"><span>TTE</span><b>${n(c.tte_sec,0)} sn</b></div>
    <div class="row"><span>resolution</span><b>${c.resolution_type||'?'}${c.resolution_symbol?(' · '+c.resolution_symbol):''}</b></div>
    <div class="row"><span>OFFICIAL PTB</span><b>${c.official_reference_open==null?'<span class="neg">— (PTB_MISSING)</span>':n(c.official_reference_open,2)+' <span class="neu" style="font-size:10px">'+(c.official_reference_source||'')+'</span>'}</b></div>
    <div class="row"><span>proxy (Binance)</span><b class="neu">${n(c.proxy_reference_open,2)}</b></div>
    <div class="row"><span>current / ref age</span><b>${n(c.reference_current,2)} · ${c.reference_current_age_ms!=null?n(c.reference_current_age_ms,0)+'ms':'—'}</b></div>
    <div class="row"><span>spot / distance</span><b>${n(c.spot_price,2)} / <span class="${cls(c.distance_bps)}">${n(c.distance_bps,1)}bps</span></b></div>
    <div class="row"><span>UP  bid/ask/mid</span><b>${n(c.up_bid,3)} / ${n(c.up_ask,3)} / <b>${n(c.up_mid,3)}</b></b></div>
    <div class="row"><span>DOWN bid/ask/mid</span><b>${n(c.down_bid,3)} / ${n(c.down_ask,3)} / ${n(c.down_mid,3)}</b></div>
    <div class="row"><span>age transport/source/book</span><b>${n(c.transport_age_ms,0)} / ${n(c.source_age_ms,0)} / ${n(c.book_age_ms,0)} ms</b></div>
    <div class="row"><span>decision · reason</span><b>${dec} · ${c.abstain_reason||''}</b></div>
    <div class="row"><span>regime · predictability</span><b>${c.regime||'—'} · HEURISTIC ${n(c.predictability_heuristic,2)}</b></div>
    <div class="why">${(c.why||[]).join(' · ')}</div>
  </div>`;
}
async function tick(){
  let d; try{ d=await (await fetch('/api/state',{cache:'no-store'})).json() }catch(e){ $('conn').textContent='baglanti yok'; return }
  $('mode').textContent=(d.mode||'SHADOW')+' · '+(d.phase||'P1');
  $('uptime').textContent='calisma: '+Math.round(d.uptime_sec||0)+'s';
  $('conn').innerHTML='Binance '+(d.binance_connected?'<span class="pos">bagli</span>':'<span class="neg">yok</span>');
  $('clock').innerHTML='clock '+(d.clock_synced?'<span class="pos">sync</span>':'<span class="neg">UNSYNC</span>')+(d.clock_offset_ms!=null?(' ('+Math.round(d.clock_offset_ms)+'ms)'):'');
  $('grid').innerHTML=(d.cards||[]).map(card).join('');
  const f=d.footer||{};
  const items=[
    ['aktif market',f.markets_active],['kesfedilen',f.markets_discovered_total],
    ['snapshot',f.snapshots_total],['etiketli',f.snapshots_labeled],
    ['resolved',f.resolved_total],['label_mismatch',f.label_mismatch],
    ['CLOB transport',f.clob_transport_healthy],['CLOB quote',f.clob_quote_healthy],
    ['PTB (official)',f.ptb_states_healthy],
    ['discovery_err',f.discovery_errors],['dq_err',f.data_quality_errors]];
  items.push(['book_ev',f.clob_book_events],['pchg_ev',f.clob_price_change_events],['bba_ev',f.clob_best_bid_ask_events],['quote_upd',f.clob_quote_updates]);
  const sf=d.safety||{};
  items.push(['phase',sf.phase],['training',sf.model_training_enabled?'ON':'OFF'],['model_save',sf.model_save_calls],['calib_writes',sf.calibration_writes],['orders',sf.live_orders]);
  $('foot').innerHTML=items.map(x=>`<div><b>${x[1]==null?'—':x[1]}</b> ${x[0]}</div>`).join('');
  if(f.suspicious_identical_quotes){$('sus').style.display='block';$('sus').innerHTML='⚠ <b>SUSPICIOUS_IDENTICAL_QUOTES</b>: birden fazla markette ayni up_mid — CLOB/token mapping supheli.';}
  else{$('sus').style.display='none';}
  const need=d.min_markets_for_stats||30;
  if((f.resolved_total||0)<need){
    $('banner').style.display='block';
    $('banner').innerHTML=`P1: yalniz DOGRU HAM VERI toplaniyor. resolved=<b>${f.resolved_total||0}</b> (esik ${need}). Model egitilmedi -> karar ABSTAIN(MODEL_NOT_TRAINED). Hicbir winrate/edge uretilmez.`;
  }else{$('banner').style.display='none';}
  $('stamp').textContent='guncelleme: '+new Date().toLocaleTimeString('tr-TR');
}
setInterval(tick,1500); tick();
</script></body></html>"""
