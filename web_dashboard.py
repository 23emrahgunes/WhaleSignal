"""Web dashboard (aiohttp) — net PnL, aktif market ve gostergeleri gorsel sunar.

Ekstra bagimlilik yok (aiohttp zaten kurulu). `/` HTML sayfasini, `/api/state`
`StrategyRunner.snapshot()` JSON'unu doner. Sayfa periyodik olarak API'yi cekip
gunceller.
"""
from __future__ import annotations

import asyncio
import logging

from aiohttp import web

log = logging.getLogger("dual_arbitraj.web")


async def run_web(runner, cfg, stop: asyncio.Event) -> None:
    app = web.Application()

    async def index(_req: web.Request) -> web.Response:
        return web.Response(text=_HTML, content_type="text/html")

    async def state(_req: web.Request) -> web.Response:
        return web.json_response(runner.snapshot())

    async def health(_req: web.Request) -> web.Response:
        return web.json_response({"ok": True, "mode": cfg.exec_mode.value})

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
<title>Dual-Arbitraj Panel</title>
<style>
:root{--bg:#0a0f1a;--panel:#111a2b;--panel2:#0d1524;--line:#22304a;--tx:#eef2f8;--mut:#8ea3c4;--blue:#4b9cff;--grn:#16c88a;--red:#ef4d4d;--amb:#f2a93b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font-family:Inter,Segoe UI,Arial,sans-serif}
header{padding:18px 22px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:16px;flex-wrap:wrap;background:#0a101c}
h1{margin:0;font-size:20px;color:var(--blue)}
.pill{padding:4px 10px;border-radius:6px;font-size:12px;font-weight:800}
.sim{background:#17345d;color:#bcd7ff}.dry{background:#5a4a05;color:#ffe7a1}.live{background:#7a1414;color:#ffc4c4}
.wrap{max-width:1200px;margin:auto;padding:18px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px}
.card h2{margin:0 0 12px;font-size:15px;color:var(--blue);border-bottom:1px solid var(--line);padding-bottom:8px}
.mini{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:11px}
.mini span{display:block;color:#9db4d5;font-size:11px;text-transform:uppercase;margin-bottom:5px}
.mini strong{font-size:18px;font-weight:750}
.big{font-size:34px;font-weight:800}
.pos{color:var(--grn)}.neg{color:var(--red)}.neu{color:var(--mut)}
.ok{color:var(--grn)}.bad{color:var(--red)}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{padding:8px 6px;border-bottom:1px solid #202c41;text-align:left;white-space:nowrap}
th{color:#9eb5d5;font-weight:600}
.tag{padding:3px 7px;border-radius:5px;font-size:11px;font-weight:700}
.tg-open{background:#087b58;color:#9ff6d8}.tg-close{background:#39465b;color:#d5deed}.tg-adv{background:#8f2020;color:#ffc0c0}
.muted{color:var(--mut);font-size:12px}
</style></head><body>
<header>
  <h1>Dual-Arbitraj — Cift 40&cent; Kutu</h1>
  <span id="mode" class="pill sim">SIM</span>
  <span id="conn" class="muted">baglaniyor...</span>
  <span id="uptime" class="muted"></span>
</header>
<div class="wrap">
  <div class="card">
    <div class="grid">
      <div class="mini"><span>Net PnL (simule)</span><strong id="pnl" class="big neu">$0.00</strong></div>
      <div class="mini"><span>Kutu / Tamamlanan / Tek-bacak</span><strong id="boxes">0 / 0 / 0</strong></div>
      <div class="mini"><span>Tamamlanma Orani</span><strong id="comp">—</strong></div>
      <div class="mini"><span>Dolum Orani</span><strong id="fill">—</strong></div>
      <div class="mini"><span>Sharpe</span><strong id="sharpe">—</strong></div>
    </div>
  </div>

  <div class="card">
    <h2>Aktif Market</h2>
    <div class="grid">
      <div class="mini"><span>Soru</span><strong id="q" style="font-size:13px">—</strong></div>
      <div class="mini"><span>Kalan Sure</span><strong id="rem">—</strong></div>
      <div class="mini"><span>UP mid / DOWN mid</span><strong id="mids">—</strong></div>
      <div class="mini"><span>Giris Karari</span><strong id="entry">—</strong></div>
    </div>
    <div id="reasons" class="muted" style="margin-top:10px"></div>
  </div>

  <div class="card">
    <h2>Gostergeler (giris sartlari)</h2>
    <div class="grid">
      <div class="mini"><span>OBI (|x| &lt; <span id="thObi"></span>)</span><strong id="obi">—</strong></div>
      <div class="mini"><span>ATR% (&lt; <span id="thAtr"></span>)</span><strong id="atr">—</strong></div>
      <div class="mini"><span>ADX (&lt; <span id="thAdx"></span>)</span><strong id="adx">—</strong></div>
      <div class="mini"><span>Fiyat Hizi / Doyum</span><strong id="vel">—</strong></div>
      <div class="mini"><span>Implied Vol (DVOL)</span><strong id="iv">—</strong></div>
      <div class="mini"><span>Box Durumu (guard)</span><strong id="box">—</strong></div>
    </div>
  </div>

  <div class="card">
    <h2>Olaylar</h2>
    <div class="scroll"><table><thead><tr><th>Saat</th><th>Olay</th><th>Detay</th><th>PnL</th></tr></thead>
    <tbody id="events"><tr><td colspan="4" class="muted">Henuz olay yok...</td></tr></tbody></table></div>
  </div>
  <div class="muted">Otomatik yenilenir · <span id="stamp"></span></div>
</div>
<script>
const $=id=>document.getElementById(id);
function usd(v){const n=Number(v||0);return (n<0?'-$':'$')+Math.abs(n).toFixed(2)}
function cls(v){return v>0?'pos':v<0?'neg':'neu'}
function tsz(t){return t?new Date(t*1000).toLocaleTimeString('tr-TR'):'—'}
function okbad(cond){return cond?'ok':'bad'}
async function tick(){
  let d; try{ d=await (await fetch('/api/state',{cache:'no-store'})).json() }catch(e){ $('conn').textContent='baglanti yok'; return }
  const m=$('mode'); m.textContent=d.mode; m.className='pill '+d.mode.toLowerCase();
  $('uptime').textContent='calisma: '+Math.round(d.uptime_sec)+'s';
  const c=d.connection||{};
  $('conn').innerHTML = (c.book_up?'UP✓':'UP✗')+' '+(c.book_down?'DOWN✓':'DOWN✗')+' · mum '+(c.candles||0)+(c.stale_sec!=null?(' · '+c.stale_sec+'s once'):'');
  const s=d.stats||{};
  const pnl=$('pnl'); pnl.textContent=usd(s.totalPnl); pnl.className='big '+cls(s.totalPnl);
  $('boxes').textContent=(s.boxes||0)+' / '+(s.completed||0)+' / '+(s.stranded||0);
  $('comp').textContent=((s.completionRate||0)*100).toFixed(0)+'%';
  $('fill').textContent=((s.fillRate||0)*100).toFixed(0)+'%';
  $('sharpe').textContent=(s.sharpe||0).toFixed(2);
  const mk=d.market;
  $('q').textContent=mk?(mk.question||mk.condition_id||'—'):'MARKET YOK';
  $('rem').textContent=mk?Math.round(mk.remaining_sec)+' sn':'—';
  const a=d.analytics||{}, th=d.thresholds||{};
  $('mids').textContent=(a.up_mid!=null?a.up_mid.toFixed(3):'—')+' / '+(a.down_mid!=null?a.down_mid.toFixed(3):'—');
  const e=d.entry||{};
  $('entry').innerHTML = e.allowed?'<span class="ok">GIRIS UYGUN</span>':'<span class="bad">GIRIS YOK</span>';
  $('reasons').textContent = (e.reasons&&e.reasons.length)?('Engel: '+e.reasons.join(' · ')):(a.ready?'':'veri bekleniyor');
  $('thObi').textContent=th.obi_max; $('thAtr').textContent=th.atr_max_pct; $('thAdx').textContent=th.adx_max;
  const obi=$('obi'); obi.textContent=(a.obi!=null?a.obi.toFixed(3):'—'); obi.className='mini-v '+okbad(Math.abs(a.obi)<th.obi_max);
  const atr=$('atr'); atr.textContent=(a.atr_pct!=null?(a.atr_pct*100).toFixed(3)+'%':'—'); atr.className=okbad(a.atr_pct<th.atr_max_pct);
  const adx=$('adx'); adx.textContent=(a.adx!=null?a.adx.toFixed(1):'—'); adx.className=okbad(a.adx<th.adx_max);
  $('vel').textContent=(a.price_velocity!=null?a.price_velocity.toFixed(3):'—')+(a.saturation?' · DOYUM':'');
  $('iv').textContent=(a.implied_vol||0).toFixed(1);
  const b=d.box;
  $('box').textContent = b?((b.up_filled?'UP✓':'UP·')+' '+(b.down_filled?'DOWN✓':'DOWN·')+' ['+b.guard+']'):'kutu yok';
  const tb=$('events');
  if(d.events&&d.events.length){
    tb.innerHTML=d.events.map(ev=>{
      let tag='tg-close';
      if(ev.kind==='BOX_ACILDI'||ev.kind==='BACAK_DOLDU') tag='tg-open';
      else if(ev.detail&&ev.detail.includes('ADVERSE')) tag='tg-adv';
      return `<tr><td>${tsz(ev.ts)}</td><td><span class="tag ${tag}">${ev.kind}</span></td><td>${ev.detail}</td><td class="${cls(ev.pnl)}">${ev.pnl?usd(ev.pnl):'—'}</td></tr>`;
    }).join('');
  }
  $('stamp').textContent=new Date().toLocaleTimeString('tr-TR');
}
setInterval(tick,1500); tick();
</script></body></html>"""
