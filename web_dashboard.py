"""Web dashboard (aiohttp) — 12 kart + WHY + veri sagligi (SHADOW).

`/` HTML sayfasi, `/api/state` ShadowEngine.snapshot() JSON'u, `/health` saglik.
Ekstra bagimlilik yok. Sayfa periyodik olarak API'yi cekip 12 karti gunceller.
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
        return web.json_response({"ok": True, "mode": "SHADOW", "phase": "P1"})

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
<title>Direction Engine — 12 Kombinasyon</title>
<style>
:root{--bg:#0a0f1a;--panel:#111a2b;--panel2:#0d1524;--line:#22304a;--tx:#eef2f8;--mut:#8ea3c4;--blue:#4b9cff;--grn:#16c88a;--red:#ef4d4d;--amb:#f2a93b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font-family:Inter,Segoe UI,Arial,sans-serif}
header{padding:16px 22px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:14px;flex-wrap:wrap;background:#0a101c}
h1{margin:0;font-size:19px;color:var(--blue)}
.pill{padding:4px 10px;border-radius:6px;font-size:12px;font-weight:800;background:#17345d;color:#bcd7ff}
.muted{color:var(--mut);font-size:12px}
.wrap{max-width:1320px;margin:auto;padding:16px}
.banner{background:#2a1d05;border:1px solid #6b4c0d;color:#ffe0a1;padding:10px 14px;border-radius:8px;margin-bottom:14px;font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}
.card.dead{opacity:.5}
.card h2{margin:0 0 8px;font-size:15px;color:var(--blue);display:flex;justify-content:space-between;align-items:center}
.dtag{padding:3px 9px;border-radius:6px;font-size:12px;font-weight:800}
.d-UP{background:#087b58;color:#9ff6d8}.d-DOWN{background:#8f2020;color:#ffc0c0}.d-ABSTAIN{background:#39465b;color:#d5deed}
.row{display:flex;justify-content:space-between;font-size:12px;padding:3px 0;border-bottom:1px solid #1a2540}
.row span{color:var(--mut)}.row b{font-weight:700}
.pos{color:var(--grn)}.neg{color:var(--red)}.neu{color:var(--mut)}
.ok{color:var(--grn)}.bad{color:var(--red)}
.why{margin-top:8px;font-size:11px;color:#9db4d5;min-height:16px}
.rtype{font-size:10px;padding:2px 6px;border-radius:4px;background:#16233c;color:#8fb4e6}
.foot{margin-top:14px;font-size:12px;color:var(--mut);display:flex;gap:18px;flex-wrap:wrap}
</style></head><body>
<header>
  <h1>Direction Engine vNext — BTC/ETH/SOL/XRP &times; 5m/15m/1h</h1>
  <span class="pill" id="mode">SHADOW · P1</span>
  <span class="muted" id="conn">baglaniyor...</span>
  <span class="muted" id="uptime"></span>
</header>
<div class="wrap">
  <div class="banner" id="banner" style="display:none"></div>
  <div class="grid" id="grid"></div>
  <div class="foot">
    <span id="rec_markets">market: —</span>
    <span id="rec_resolved">resolved: —</span>
    <span id="rec_snaps">snapshot: —</span>
    <span id="rec_labeled">etiketli: —</span>
    <span id="model">model: —</span>
    <span id="calib">kalibrasyon: —</span>
    <span id="stamp"></span>
  </div>
</div>
<script>
const $=id=>document.getElementById(id);
function num(v,d){return v==null?'—':Number(v).toFixed(d)}
function cls(v){return v>0?'pos':v<0?'neg':'neu'}
function card(c){
  const dead=c.active?'':' dead';
  const dec=c.decision||'ABSTAIN';
  const rtype=c.resolution_type?`<span class="rtype">${c.resolution_type}${c.resolution_meta_ok===false?' ⚠':''}</span>`:'';
  const fr=c.freshness||{};
  const frtxt = c.active ? ((fr.ok?'<span class="ok">taze</span>':'<span class="bad">BAYAT</span>')+
     ` · spot ${fr.spot_age_ms!=null?fr.spot_age_ms+'ms':'—'} · book ${fr.book_age_ms!=null?fr.book_age_ms+'ms':'—'}`) : '';
  let body;
  if(!c.active){
    body=`<div class="why">${(c.why||['market YOK']).join(' · ')}</div>`;
  }else{
    const db=c.distance_bps;
    const pu=c.p_up, edge=c.price_edge;
    body=`
    <div class="row"><span>kalan</span><b>${num(c.seconds_remaining,0)} sn</b></div>
    <div class="row"><span>P(UP) / güven</span><b class="${pu>0.55?'pos':pu<0.45?'neg':'neu'}">${num(pu,3)} / ${num(c.confidence,2)}</b></div>
    <div class="row"><span>predictability / rejim</span><b>${num(c.predictability,2)} · ${c.regime||'—'}</b></div>
    <div class="row"><span>spot / PTB</span><b>${num(c.spot_price,2)} / ${num(c.reference_price,2)}</b></div>
    <div class="row"><span>mesafe (bps)</span><b class="${cls(db)}">${num(db,1)}</b></div>
    <div class="row"><span>UP mid / edge</span><b>${num(c.up_mid,3)} / <span class="${cls(edge)}">${edge==null?'—':num(edge,3)}</span></b></div>
    <div class="row"><span>veri</span><b>${frtxt}</b></div>
    <div class="why">${dec==='ABSTAIN'?('ABSTAIN: '+(c.abstain_reason||'')+' · '):''}${(c.why||[]).join(' · ')}</div>`;
  }
  return `<div class="card${dead}">
     <h2>${c.combo} ${rtype}<span class="dtag d-${dec}">${dec}</span></h2>${body}</div>`;
}
async function tick(){
  let d; try{ d=await (await fetch('/api/state',{cache:'no-store'})).json() }catch(e){ $('conn').textContent='baglanti yok'; return }
  $('mode').textContent=(d.mode||'SHADOW')+' · '+(d.phase||'P1');
  $('uptime').textContent='calisma: '+Math.round(d.uptime_sec||0)+'s';
  $('conn').innerHTML='Binance '+(d.binance_connected?'<span class="ok">bagl1</span>':'<span class="bad">yok</span>');
  $('grid').innerHTML=(d.cards||[]).map(card).join('');
  const r=d.recorder||{};
  $('rec_markets').textContent='market: '+(r.markets||0)+' ('+(r.meta_ok_markets||0)+' meta✓)';
  $('rec_resolved').textContent='resolved: '+(r.resolved_markets||0);
  $('rec_snaps').textContent='snapshot: '+(r.snapshots||0);
  $('rec_labeled').textContent='etiketli: '+(r.labeled_snapshots||0);
  const mdl=d.model||{}, wc=mdl.with_clob||{};
  $('model').innerHTML='model(CLOB): '+(wc.shared_markets||0)+' market '+(wc.shared_ready?'<span class="ok">hazır</span>':'<span class="bad">öğreniyor</span>')+' (eşik '+(mdl.min_markets_predict||20)+')';
  const cal=(d.calibration||{}).overall||{};
  $('calib').textContent = cal.insufficient===false ?
      ('acc '+(cal.accuracy!=null?(cal.accuracy*100).toFixed(0)+'%':'—')+' · Brier '+num(cal.brier,3)+' · n='+cal.n_decided) :
      ('kalibrasyon: yetersiz veri (n='+(cal.n_decided||0)+')');
  const need=d.min_markets_for_stats||30;
  if((r.resolved_markets||0)<need){
    $('banner').style.display='block';
    $('banner').innerHTML=`⚠ Yetersiz veri: yalnizca <b>${r.resolved_markets||0}</b> resmi resolved market kayitli (esik ${need}). `+
      `Bu esige kadar <b>hicbir winrate/accuracy/edge iddiasi</b> uretilmez. P1: yalniz veri toplaniyor, karar daima ABSTAIN.`;
  }else{$('banner').style.display='none';}
  $('stamp').textContent=new Date().toLocaleTimeString('tr-TR');
}
setInterval(tick,1500); tick();
</script></body></html>"""
