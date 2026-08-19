"""P2.5 SHADOW dashboard and JSON state API."""
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
<title>Direction Engine P2.5 — SHADOW</title>
<style>
:root{--bg:#080d17;--panel:#10192a;--line:#22314a;--tx:#eef3fb;--mut:#91a6c6;--blue:#57a2ff;--green:#18cb8d;--red:#ef5d62;--amber:#efb44c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:13px Inter,Segoe UI,Arial,sans-serif}
header{position:sticky;top:0;z-index:2;background:#0a101c;border-bottom:1px solid var(--line);padding:12px 18px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
h1{font-size:18px;margin:0;color:var(--blue)}.pill{padding:4px 9px;border-radius:6px;background:#17345d;color:#c9deff;font-weight:800}.mut{color:var(--mut)}
.wrap{max-width:1650px;margin:auto;padding:14px}.banner{padding:9px 12px;border:1px solid #695019;background:#291f08;color:#ffe0a1;border-radius:8px;margin-bottom:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:11px}.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px}.dead{opacity:.45}
.card h2{font-size:15px;color:var(--blue);margin:0 0 8px;display:flex;justify-content:space-between}.tag{padding:2px 7px;border-radius:5px;font-size:11px}.UP{background:#09684c;color:#a8f7dc}.DOWN{background:#7d2428;color:#ffd0d1}.ABSTAIN{background:#354158;color:#dfe7f4}
.row{display:flex;justify-content:space-between;gap:10px;border-bottom:1px solid #1b2740;padding:3px 0}.row span{color:var(--mut)}.mono{font-family:ui-monospace,Consolas,monospace;font-size:11px}
.qs{display:flex;gap:3px;flex-wrap:wrap;margin:5px 0}.q{padding:2px 5px;border-radius:4px;font-size:10px;font-weight:700}.q-OK{background:#0b5a3e;color:#a1f2d4}.q-WARN{background:#5b4907;color:#ffe8a6}.q-FAIL{background:#711c21;color:#ffc8ca}.q-WAITING{background:#29364f;color:#a9bddb}
.good{color:var(--green)}.bad{color:var(--red)}.amb{color:var(--amber)}.why{color:#9db1d0;font-size:11px;margin-top:7px;min-height:26px}
.foot{display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:7px;margin-top:12px}.metric{background:#0d1524;border:1px solid var(--line);border-radius:7px;padding:7px}.metric b{display:block;font-size:15px;color:#ddebff}
</style>
</head>
<body>
<header>
<h1>Direction Engine — P2.5</h1>
<span class="pill" id="phase">SHADOW</span>
<span class="mut" id="conn">bağlanıyor…</span>
<span class="mut" id="up"></span>
</header>
<div class="wrap">
<div class="banner" id="banner">P2.5 yalnız SHADOW tahmin üretir. Emir, imza ve private key yoktur.</div>
<div class="grid" id="grid"></div>
<div class="foot" id="foot"></div>
</div>
<script>
const $=id=>document.getElementById(id);
const n=(v,d=3)=>v==null?'—':Number(v).toFixed(d);
const pc=v=>v==null?'—':(Number(v)*100).toFixed(1)+'%';
function qchip(k,v){return `<span class="q q-${v||'WAITING'}">${k}:${v||'?'}</span>`}
function card(c){
 if(!c.active)return `<div class="card dead"><h2>${c.combo}<span class="tag ABSTAIN">NO MARKET</span></h2></div>`;
 const q=c.quality||{};
 const qs=[['T',q.time],['M',q.market],['Tk',q.tokens],['C',q.clob],['R',q.reference],['Ck',q.clock],['Md',q.model]].map(x=>qchip(x[0],x[1])).join('');
 const f=c.feature||{};
 const dec=c.decision||'ABSTAIN';
 const p=c.p_up==null?'—':pc(c.p_up);
 return `<div class="card">
 <h2>${c.combo}<span class="tag ${dec}">${dec}</span></h2>
 <div class="qs">${qs}</div>
 <div class="row mono"><span>market / TTE</span><b>${c.market_id} · ${n(c.tte_sec,0)}s</b></div>
 <div class="row"><span>PTB / distance</span><b>${n(c.official_reference_open,4)} · ${n(c.distance_bps,2)}bps</b></div>
 <div class="row"><span>UP / DOWN mid</span><b>${n(c.up_mid)} / ${n(c.down_mid)}</b></div>
 <div class="row"><span>feature ready / coverage</span><b class="${f.ready?'good':'amb'}">${f.ready?'READY':'WARMUP'} · ${pc(f.coverage)}</b></div>
 <div class="row"><span>history / ret 1s·15s·60s</span><b>${n(f.history_sec,0)}s · ${n(f.ret_1s_bps,2)}/${n(f.ret_15s_bps,2)}/${n(f.ret_60s_bps,2)}bps</b></div>
 <div class="row"><span>flow / persist / flip</span><b>${n(f.flow_5s,2)} · ${n(f.momentum_persist,2)} · ${n(f.flip_rate,2)}</b></div>
 <div class="row"><span>OBI / OFI / PTB z</span><b>${n(f.obi20,2)} / ${n(f.ofi,2)} / ${n(f.ptb_z,2)}</b></div>
 <div class="row"><span>regime / predictability</span><b>${c.regime||'—'} · ${pc(c.predictability)}</b></div>
 <div class="row"><span>conflict / consensus</span><b>${n(c.conflict_score,2)} / ${n(c.directional_consensus,2)}</b></div>
 <div class="row"><span>P(UP) B2 raw→cal</span><b>${pc(c.p_up_raw)} → <span class="good">${p}</span></b></div>
 <div class="row"><span>B1 / PTB / market</span><b>${pc(c.p_up_external)} / ${pc(c.p_up_ptb)} / ${pc(c.p_up_market)}</b></div>
 <div class="row"><span>threshold / calibration</span><b>${pc(c.threshold)} · ${c.threshold_source||'—'} · ${c.calibration_source||'—'}</b></div>
 <div class="row"><span>reason</span><b>${c.abstain_reason||'NONE'}</b></div>
 <div class="why">${(c.why||[]).join(' · ')}</div>
 </div>`;
}
async function tick(){
 let d;
 try{const r=await fetch('/api/state',{cache:'no-store'});d=await r.json()}catch(e){$('conn').textContent='API yok';return}
 $('phase').textContent=(d.mode||'SHADOW')+' · '+(d.phase||'');
 $('conn').innerHTML='Binance '+(d.binance_connected?'<span class="good">bağlı</span>':'<span class="bad">yok</span>')+' · clock '+(d.clock_synced?'<span class="good">sync</span>':'<span class="bad">UNSYNC</span>');
 $('up').textContent='uptime '+Math.round(d.uptime_sec||0)+'s';
 $('grid').innerHTML=(d.cards||[]).map(card).join('');
 const f=d.footer||{},s=d.safety||{},a=(d.forecast_analytics||{}).overall||{};
 const items=[
 ['active',f.markets_active],['PTB',f.ptb_states_healthy],['CLOB',f.clob_quote_healthy],
 ['features ready',f.features_ready],['model ready',f.model_ready_cards],
 ['snapshots',f.snapshots_total],['forecasts',f.forecasts],['labeled forecasts',f.labeled_forecasts],
 ['resolved',f.resolved_total],['coverage',a.coverage==null?'—':pc(a.coverage)],
 ['accuracy',a.accuracy==null?'—':pc(a.accuracy)],['Brier B2',a.brier_b2],
 ['training',s.model_training_enabled?'ON':'OFF'],['calibration',s.calibration_enabled?'ON':'OFF'],
 ['orders',s.live_orders],['execution',s.execution_enabled?'ON':'OFF']
 ];
 $('foot').innerHTML=items.map(x=>`<div class="metric"><b>${x[1]==null?'—':x[1]}</b>${x[0]}</div>`).join('');
}
setInterval(tick,1500);tick();
</script>
</body>
</html>"""
