"""P2.5 aiohttp dashboard for the SHADOW direction pipeline."""
from __future__ import annotations

import asyncio
import logging

from aiohttp import web

log = logging.getLogger("direction_engine.web")


async def run_web(engine, cfg, stop: asyncio.Event) -> None:  # noqa: ANN001
    app = web.Application()

    async def index(_request: web.Request) -> web.Response:
        return web.Response(text=_HTML, content_type="text/html")

    async def state(_request: web.Request) -> web.Response:
        return web.json_response(engine.snapshot())

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({
            "ok": True,
            "mode": "SHADOW",
            "phase": cfg.phase,
            "execution": False,
            "live_orders": 0,
        })

    app.add_routes([
        web.get("/", index),
        web.get("/api/state", state),
        web.get("/health", health),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.web_host, cfg.web_port)
    await site.start()
    log.info("web dashboard: http://%s:%d", cfg.web_host, cfg.web_port)
    try:
        await stop.wait()
    finally:
        await runner.cleanup()
        log.info("web dashboard stopped")


_HTML = r"""<!DOCTYPE html>
<html lang="tr"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Direction Engine — P2.5 SHADOW</title>
<style>
:root{--bg:#080d17;--panel:#101a2a;--panel2:#0c1422;--line:#23324b;--tx:#edf3fb;--mut:#8fa5c5;--blue:#4b9cff;--grn:#18c98c;--red:#ef6262;--amb:#f4b23e;--vio:#a98bff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font-family:Inter,Segoe UI,Arial,sans-serif}
header{padding:13px 18px;border-bottom:1px solid var(--line);display:flex;gap:11px;align-items:center;flex-wrap:wrap;position:sticky;top:0;background:#09101d;z-index:4}
h1{margin:0;font-size:17px;color:var(--blue)}.pill{font-size:11px;font-weight:800;padding:3px 8px;border-radius:5px;background:#17345d;color:#c1dcff}.muted{color:var(--mut);font-size:11px}
.wrap{max-width:1840px;margin:auto;padding:12px}.banner{padding:9px 12px;border-radius:7px;margin-bottom:10px;font-size:12px;background:#261c08;border:1px solid #71520f;color:#ffe2a7}.danger{background:#391010;border-color:#802020;color:#ffcdcd}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(355px,1fr));gap:10px}.card{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:11px;font-size:11px}.card.dead{opacity:.48}.card h2{margin:0 0 6px;color:var(--blue);font-size:14px;display:flex;align-items:center;gap:6px}.spacer{flex:1}.tag{padding:2px 7px;border-radius:5px;font-weight:800;font-size:10px}.UP{background:#087653;color:#a8f7dc}.DOWN{background:#8b2424;color:#ffd0d0}.ABSTAIN{background:#3a465a;color:#e0e7f2}.ready{color:var(--grn);font-weight:800}.wait{color:var(--amb);font-weight:800}
.qrow{display:flex;gap:3px;flex-wrap:wrap;margin:5px 0}.q{padding:2px 4px;border-radius:3px;font-size:9px;font-weight:750}.q-OK{background:#0d5f42;color:#9ef3d4}.q-WARN{background:#62520b;color:#ffebb0}.q-FAIL{background:#7d1d1d;color:#ffd0d0}.q-WAITING{background:#2c3851;color:#aec2df}
.row{display:flex;justify-content:space-between;gap:8px;padding:2px 0;border-bottom:1px solid #1a2740}.row span{color:var(--mut)}.row b{text-align:right;font-weight:700;overflow-wrap:anywhere}.mono{font-family:ui-monospace,Consolas,monospace;font-size:10px}.section{margin-top:7px;color:var(--vio);font-weight:800;font-size:10px;letter-spacing:.04em}.pos{color:var(--grn)}.neg{color:var(--red)}.neu{color:var(--mut)}.why{margin-top:6px;color:#9eb3d1;font-size:9px;min-height:12px;line-height:1.35}
.foot{margin-top:12px;display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:5px}.foot div{background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:6px 8px;color:var(--mut);font-size:10px}.foot b{color:var(--tx);font-size:12px}.metrics{margin-top:10px;background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:9px;font-size:11px}.metrics h3{margin:0 0 6px;color:var(--vio)}
</style></head><body>
<header><h1>Direction Engine</h1><span class="pill" id="phase">SHADOW</span><span class="muted" id="conn">bağlanıyor…</span><span class="muted" id="clock"></span><span class="muted" id="uptime"></span></header>
<div class="wrap"><div class="banner danger" id="danger" style="display:none"></div><div class="banner" id="banner"></div><div class="grid" id="grid"></div><div class="metrics" id="metrics"></div><div class="foot" id="foot"></div><div class="muted" id="stamp" style="margin-top:8px"></div></div>
<script>
const $=id=>document.getElementById(id);const num=(v,d=2)=>v==null?'—':Number(v).toFixed(d);const sign=v=>v>0?'pos':v<0?'neg':'neu';
function qchip(k,v){return `<span class="q q-${v}">${k}:${v}</span>`}
function row(k,v,klass=''){return `<div class="row"><span>${k}</span><b class="${klass}">${v}</b></div>`}
function card(c){
 const dec=c.decision||'ABSTAIN'; if(!c.active)return `<div class="card dead"><h2>${c.combo}<span class="spacer"></span><span class="tag ABSTAIN">${c.discovery_status||'NOT_FOUND'}</span></h2><div class="why">${(c.why||[]).join(' · ')}</div></div>`;
 const Q=c.quality||{};const qrow=['time','market','tokens','clob','reference','clock','model'].map(k=>qchip(k[0].toUpperCase()+(k==='tokens'||k==='clock'?'k':''),Q[k]||'?')).join('');
 const f=c.features||{},r=c.regime_diagnostics||{},b=c.baselines||{};
 let h=`<div class="card"><h2>${c.combo}<span class="spacer"></span><span class="tag ${dec}">${dec}</span></h2><div class="qrow">${qrow}</div>`;
 h+=`<div>${c.data_ready?'<span class="ready">DATA READY</span>':'<span class="wait">DATA WAIT</span>'} · ${c.feature_ready?'<span class="ready">FEATURE READY</span>':'<span class="wait">FEATURE WARMUP</span>'}</div>`;
 h+=`<div class="section">MARKET / PLUMBING</div>`+row('market / slug',`${c.market_id} · ${(c.slug||'').slice(-25)}`,'mono')+row('TTE',`${num(c.tte_sec,0)} sn`)+row('resolution',`${c.resolution_type||'?'} · ${c.resolution_symbol||'—'}`)+row('official PTB',`${num(c.official_reference_open,4)} · ${c.official_reference_source||'—'}`)+row('spot / distance',`${num(c.spot_price,4)} / ${num(c.distance_bps,2)} bps`,sign(c.distance_bps))+row('UP bid/ask/mid',`${num(c.up_bid,3)} / ${num(c.up_ask,3)} / ${num(c.up_mid,3)}`)+row('DOWN bid/ask/mid',`${num(c.down_bid,3)} / ${num(c.down_ask,3)} / ${num(c.down_mid,3)}`)+row('ages t/s/book',`${num(c.transport_age_ms,0)} / ${num(c.source_age_ms,0)} / ${num(c.book_age_ms,0)} ms`);
 h+=`<div class="section">P2.1 FEATURES</div>`+row('coverage / history',`${num(f.coverage,3)} / ${num(f.history_sec,0)}s`)+row('return 1s / 15s / 60s',`${num(f.ret_1s_bps,2)} / ${num(f.ret_15s_bps,2)} / ${num(f.ret_60s_bps,2)} bps`)+row('momentum persist / flip',`${num(f.momentum_persist,3)} / ${num(f.flip_rate,3)}`)+row('flow 5s / RV60',`${num(f.flow_5s,3)} / ${num(f.rv_60s_bps,2)} bps`)+row('PTB z / slope',`${num(f.ptb_z,3)} / ${num(f.distance_slope,3)}`)+row('OBI20 / OFI',`${num(f.obi20,3)} / ${num(f.ofi,3)}`);
 h+=`<div class="section">P2.2 REGIME / PREDICTABILITY</div>`+row('regime',`${c.regime||'UNKNOWN'}`)+row('predictability',num(c.predictability,3))+row('direction / agreement / conflict',`${num(r.direction_score,3)} / ${num(r.agreement,3)} / ${num(r.conflict,3)}`);
 h+=`<div class="section">P2.3–P2.5 SHADOW FORECAST</div>`+row('B2 raw / B1 no-CLOB',`${num(c.p_up_raw,4)} / ${num(c.p_up_no_clob,4)}`)+row('P(UP) final / confidence',`${num(c.p_up,4)} / ${num(c.confidence,3)}`)+row('baselines coin/PTB/market',`${num(b.coinflip,3)} / ${num(b.ptb_diffusion,3)} / ${num(b.market_implied,3)}`)+row('model',`${c.model_source||'none'} · ${c.model_version||'—'}`)+row('calibration',`${c.calibration_ready?'READY':'WAIT'} · ${c.calibration_source||'—'} · n=${c.calibration_markets||0}`)+row('threshold',`${c.threshold_ready?'READY':'WAIT'} · margin=${num(c.decision_margin,3)} · ${c.threshold_source||'—'}`)+row('decision · reason',`${dec} · ${c.abstain_reason||''}`);
 h+=`<div class="why">${(c.why||[]).join(' · ')}</div></div>`;return h;
}
async function tick(){let d;try{const res=await fetch('/api/state',{cache:'no-store'});if(!res.ok)throw new Error(res.status);d=await res.json()}catch(e){$('conn').textContent='API yok';return}
 $('phase').textContent=`${d.mode||'SHADOW'} · ${d.phase||''}`;$('conn').innerHTML=`Binance ${d.binance_connected?'<span class="pos">bağlı</span>':'<span class="neg">yok</span>'}`;$('clock').innerHTML=`clock ${d.clock_synced?'<span class="pos">sync</span>':'<span class="neg">UNSYNC</span>'} ${d.clock_offset_ms==null?'':'('+Math.round(d.clock_offset_ms)+'ms)'}`;$('uptime').textContent=`çalışma: ${Math.round(d.uptime_sec||0)}s`;$('grid').innerHTML=(d.cards||[]).map(card).join('');
 const f=d.footer||{},s=d.safety||{};if(f.suspicious_identical_quotes){$('danger').style.display='block';$('danger').textContent='SUSPICIOUS_IDENTICAL_QUOTES: birden fazla markette aynı UP midpoint.'}else $('danger').style.display='none';
 const need=d.min_markets_for_stats||30;$('banner').innerHTML=`P2.5 SHADOW · resolved=<b>${f.resolved_total||0}</b> · model updates=<b>${f.model_updates||0}</b> · calibration updates=<b>${f.calibration_updates||0}</b>. ${f.resolved_total<need?'İstatistik için veri yetersiz; winrate/edge iddiası yok.':'Ölçümler örnek sayılarıyla birlikte gösterilir.'} Canlı emir: <b>0</b>.`;
 const a=(d.forecast_analytics||{}).overall||{};$('metrics').innerHTML=`<h3>Shadow analytics</h3>Forecast=${a.n_forecasts||0} · decided=${a.n_decided||0} · coverage=${num(a.coverage,3)} · accuracy=${a.accuracy==null?'yetersiz':num(a.accuracy,3)} · calibrated Brier=${a.model_calibrated?num(a.model_calibrated.brier,4):'—'} · market Brier=${a.market_implied?num(a.market_implied.brier,4):'—'}`;
 const items=[['active',f.markets_active],['snapshot',f.snapshots_total],['labeled snapshot',f.snapshots_labeled],['forecast',f.forecasts_total],['labeled forecast',f.forecasts_labeled],['decided forecast',f.forecasts_decided],['resolved',f.resolved_total],['official-only',f.official_only],['mismatch',f.label_mismatch],['model updates',f.model_updates],['calibration updates',f.calibration_updates],['CLOB transport',f.clob_transport_healthy],['CLOB quote',f.clob_quote_healthy],['PTB',f.ptb_states_healthy],['feature ready',f.feature_states_ready],['decision ready',f.decision_states_ready],['phase',s.phase],['training',s.model_training_enabled?'ON':'OFF'],['calibration',s.calibration_enabled?'ON':'OFF'],['orders',s.live_orders]];$('foot').innerHTML=items.map(x=>`<div><b>${x[1]==null?'—':x[1]}</b> ${x[0]}</div>`).join('');$('stamp').textContent='güncelleme: '+new Date().toLocaleTimeString('tr-TR');}
setInterval(tick,1500);tick();
</script></body></html>"""
