"""Dedicated read-only HTML page for paper-trade records."""

PAPER_RECORDS_HTML = r"""<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Direction Engine — Paper Kayıtları</title>
<style>
:root{--bg:#070c15;--panel:#0f1828;--panel2:#0b1321;--line:#22324c;--text:#eef4ff;--muted:#8fa5c6;--blue:#5ba4ff;--green:#18c98b;--red:#ef6468;--amber:#efb84e;--purple:#b18dff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:13px Inter,Segoe UI,Arial,sans-serif}a{color:inherit;text-decoration:none}
header{position:sticky;top:0;z-index:5;background:#09111e;border-bottom:1px solid var(--line);padding:12px 18px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
h1{font-size:18px;margin:0;color:var(--blue)}.spacer{flex:1}.pill,.nav{border-radius:6px;padding:5px 9px;font-weight:800;font-size:11px}.pill{background:#17365f;color:#cfe3ff}.paper{background:#5c4610;color:#ffe7a0}.off{background:#552026;color:#ffd0d3}.nav{border:1px solid #315077;background:#10233c;color:#dbeaff}.nav:hover{background:#173457}
.wrap{max-width:1880px;margin:auto;padding:14px}.notice{border:1px solid #73581d;background:#291f09;color:#ffe3a3;border-radius:8px;padding:10px 12px;margin-bottom:12px}.notice b{color:#fff0bd}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:8px}.metric{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:10px}.metric b{display:block;font-size:19px;color:#e8f1ff;margin-bottom:3px}.metric span{color:var(--muted);font-size:11px}.positive{color:var(--green)!important}.negative{color:var(--red)!important}.warning{color:var(--amber)!important}
.section{background:var(--panel2);border:1px solid var(--line);border-radius:11px;padding:12px;margin-top:12px}.section h2{font-size:16px;margin:0 0 10px}.sub{color:var(--muted);font-size:11px;margin-top:-5px;margin-bottom:10px}
.filters{display:grid;grid-template-columns:repeat(7,minmax(110px,1fr));gap:8px;align-items:end}.field label{display:block;color:var(--muted);font-size:11px;margin-bottom:4px}.field input,.field select{width:100%;background:#0b1424;border:1px solid #2a3c59;color:var(--text);border-radius:7px;padding:8px}.actions{display:flex;gap:7px;flex-wrap:wrap}.btn{border:1px solid #34557f;background:#163153;color:#e7f1ff;border-radius:7px;padding:8px 11px;font-weight:800;cursor:pointer}.btn:hover{background:#1d416e}.btn.secondary{background:#121e31;border-color:#2b3d59;color:#bcd0ed}.btn.csv{background:#164b3a;border-color:#247459;color:#c5ffe9}
.split{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.tablebox{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:9px;overflow:hidden}.tablebox h3{font-size:13px;margin:0 0 8px;color:#dce9fb}.tablewrap{overflow:auto;max-height:530px}table{width:100%;border-collapse:collapse;min-width:780px}th,td{padding:8px 8px;border-bottom:1px solid #1d2a41;text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}th{position:sticky;top:0;background:#111d30;color:#9eb4d4;font-size:11px;z-index:1}td{font-size:12px}.records table{min-width:1510px}.muted{color:var(--muted)}.mono{font-family:ui-monospace,Consolas,monospace;font-size:11px}.tag{display:inline-block;border-radius:5px;padding:3px 6px;font-size:10px;font-weight:800}.tag.OPEN{background:#5a430c;color:#ffe49b}.tag.SETTLED{background:#104d3b;color:#b9f8df}.tag.SKIPPED{background:#303b50;color:#c8d3e4}.tag.UP{background:#08694c;color:#b7f9df}.tag.DOWN{background:#7a2529;color:#ffd0d2}.hit{color:var(--green);font-weight:800}.miss{color:var(--red);font-weight:800}.skip{color:#a7b5c9}.pnlpos{color:var(--green);font-weight:800}.pnlneg{color:var(--red);font-weight:800}.empty{text-align:center!important;padding:30px!important;color:var(--muted)}
.pager{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:10px;flex-wrap:wrap}.pager .info{color:var(--muted)}.pager button:disabled{opacity:.35;cursor:not-allowed}.live{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:11px}.dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green)}
@media(max-width:1150px){.filters{grid-template-columns:repeat(4,minmax(120px,1fr))}.split{grid-template-columns:1fr}}@media(max-width:650px){header{padding:9px}.wrap{padding:8px}.filters{grid-template-columns:repeat(2,minmax(0,1fr))}.actions{grid-column:1/-1}.metrics{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body>
<header>
<h1>Direction Engine — Paper Kayıtları</h1>
<span class="pill paper">PAPER MODE</span>
<span class="pill off">CANLI İŞLEM KAPALI</span>
<div class="live"><span class="dot"></span><span id="refreshState">yükleniyor</span></div>
<div class="spacer"></div>
<a class="nav" href="/">← Tahmin Paneli</a>
<a class="nav" href="/api/paper-trades" target="_blank">JSON API</a>
</header>
<div class="wrap">
<div class="notice"><b>Burada market başına tek canonical paper giriş kaydı görünür.</b> OPEN sanal pozisyonu, SETTLED resmî sonucu ve PnL’yi, SKIPPED ise neden işlem açılmadığını gösterir. Hiçbir kayıt gerçek emir değildir.</div>

<div class="metrics" id="summaryMetrics"></div>

<section class="section">
<h2>Filtreler</h2>
<div class="filters">
  <div class="field"><label>Kripto</label><select id="asset"><option value="ALL">Tümü</option><option>BTC</option><option>ETH</option><option>SOL</option><option>XRP</option></select></div>
  <div class="field"><label>Timeframe</label><select id="horizon"><option value="ALL">Tümü</option><option value="5m">5 dakika</option><option value="15m">15 dakika</option><option value="1h">1 saat</option></select></div>
  <div class="field"><label>Durum</label><select id="status"><option value="ALL">Tümü</option><option>OPEN</option><option>SETTLED</option><option>SKIPPED</option></select></div>
  <div class="field"><label>Taraf</label><select id="side"><option value="ALL">Tümü</option><option>UP</option><option>DOWN</option></select></div>
  <div class="field"><label>Resmî sonuç</label><select id="result"><option value="ALL">Tümü</option><option>UP</option><option>DOWN</option></select></div>
  <div class="field"><label>Sayfa boyutu</label><select id="limit"><option>25</option><option selected>50</option><option>100</option><option>200</option></select></div>
  <div class="field"><label>Market / ID / skip ara</label><input id="q" maxlength="120" placeholder="BTC:5m, slug, LOW_CONFIDENCE"></div>
  <div class="actions"><button class="btn" id="apply">Uygula</button><button class="btn secondary" id="reset">Temizle</button><a class="btn csv" id="csv" href="/api/paper-trades.csv">CSV indir</a></div>
</div>
</section>

<section class="section">
<h2>Performans Özeti</h2>
<div class="sub">Kripto ve market/timeframe bazında yalnız paper işlemler hesaplanır.</div>
<div class="split">
 <div class="tablebox"><h3>Kripto Bazlı</h3><div class="tablewrap"><table><thead><tr><th>Kripto</th><th>Trade</th><th>Settled</th><th>W/L</th><th>Hit</th><th>PnL</th><th>ROI</th></tr></thead><tbody id="assetRows"></tbody></table></div></div>
 <div class="tablebox"><h3>Market / Timeframe Bazlı</h3><div class="tablewrap"><table><thead><tr><th>Market</th><th>Trade</th><th>Settled</th><th>W/L</th><th>Hit</th><th>PnL</th><th>ROI</th></tr></thead><tbody id="comboRows"></tbody></table></div></div>
</div>
</section>

<section class="section records">
<h2>Market Bazlı Paper Kayıtları</h2>
<div class="sub" id="recordInfo">yükleniyor…</div>
<div class="tablebox"><div class="tablewrap"><table>
<thead><tr>
<th>Zaman</th><th>Market</th><th>Durum</th><th>Taraf</th><th>Tahmin olasılığı</th><th>Güven</th><th>Grade / forecast</th><th>Bid / Ask / Fill</th><th>Stake / Shares</th><th>Resmî sonuç</th><th>Tuttu mu?</th><th>PnL</th><th>ROI</th><th>Skip nedeni</th><th>Slug / condition</th>
</tr></thead><tbody id="records"></tbody>
</table></div></div>
<div class="pager"><button class="btn secondary" id="prev">← Önceki</button><span class="info" id="pageInfo"></span><button class="btn secondary" id="next">Sonraki →</button></div>
</section>
</div>
<script>
const $=id=>document.getElementById(id);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pc=v=>v==null?'—':(Number(v)*100).toFixed(1)+'%';
const num=(v,d=3)=>v==null?'—':Number(v).toFixed(d);
const usd=v=>v==null?'—':(Number(v)>=0?'+':'')+'$'+Number(v).toFixed(3);
const date=v=>{if(!v)return '—';try{return new Intl.DateTimeFormat('tr-TR',{dateStyle:'short',timeStyle:'medium'}).format(new Date(v));}catch{return '—'}};
let offset=0;
let pagination={total:0,limit:50,offset:0,has_previous:false,has_next:false};
function params(includeOffset=true){const p=new URLSearchParams();for(const id of ['asset','horizon','status','side','result']){const v=$(id).value;if(v&&v!=='ALL')p.set(id,v)}const q=$('q').value.trim();if(q)p.set('q',q);p.set('limit',$('limit').value||'50');if(includeOffset)p.set('offset',String(offset));return p}
function metric(value,label,klass=''){return `<div class="metric"><b class="${klass}">${value??'—'}</b><span>${label}</span></div>`}
function performanceRows(map){const entries=Object.entries(map||{});if(!entries.length)return `<tr><td class="empty" colspan="7">Henüz sonuç yok</td></tr>`;return entries.map(([key,m])=>`<tr><td><b>${esc(key)}</b></td><td>${m.trades??0}</td><td>${m.settled??0}</td><td>${m.wins??0}/${m.losses??0}</td><td>${pc(m.hit_rate)}</td><td class="${Number(m.realized_pnl_usdc||0)>=0?'pnlpos':'pnlneg'}">${usd(m.realized_pnl_usdc)}</td><td>${pc(m.roi)}</td></tr>`).join('')}
function recordRow(r){const resultClass=r.outcome_label==='TUTTU'?'hit':r.outcome_label==='KACTI'?'miss':r.status==='OPEN'?'open':'skip';const pnlClass=Number(r.realized_pnl||0)>=0?'pnlpos':'pnlneg';const probability=r.selected_probability==null?'—':pc(r.selected_probability);const grade=`${esc(r.forecast_grade||'—')} / ${esc(r.forecast_status||'—')}`;const prices=`${num(r.entry_bid)} / ${num(r.entry_ask)} / <b>${num(r.fill_price)}</b>`;const amounts=`$${num(r.stake_usdc,2)} / ${num(r.shares,4)}`;const ids=`<span class="mono">${esc(r.slug||'—')}<br>${esc((r.condition_id||'').slice(-12))}</span>`;return `<tr><td>${date(r.attempted_at_iso)}</td><td><b>${esc(r.combo_key)}</b><br><span class="muted">T-${esc(r.checkpoint_sec)}s</span></td><td><span class="tag ${esc(r.status)}">${esc(r.status)}</span></td><td>${r.side?`<span class="tag ${esc(r.side)}">${esc(r.side)}</span>`:'—'}</td><td>${probability}</td><td>${pc(r.forecast_confidence)}</td><td>${grade}</td><td>${prices}</td><td>${amounts}</td><td>${esc(r.official_result||'—')}</td><td class="${resultClass}">${esc(r.outcome_label)}</td><td class="${pnlClass}">${usd(r.realized_pnl)}</td><td>${pc(r.roi)}</td><td class="skip">${esc(r.skip_reason||'—')}</td><td>${ids}</td></tr>`}
async function loadSummary(){const res=await fetch('/api/paper-summary',{cache:'no-store'});if(!res.ok)throw new Error('summary HTTP '+res.status);const data=await res.json();const p=data.paper_trading||{},o=p.overall||{};$('summaryMetrics').innerHTML=[metric(o.attempts??0,'Giriş denemesi'),metric(o.trades??0,'Paper trade'),metric(o.open??0,'Açık pozisyon','warning'),metric(o.settled??0,'Settled'),metric(`${o.wins??0}/${o.losses??0}`,'Win / Loss'),metric(pc(o.hit_rate),'Hit rate'),metric(usd(o.realized_pnl_usdc),'Realized PnL',Number(o.realized_pnl_usdc||0)>=0?'positive':'negative'),metric('$'+num(o.equity_usdc,2),'Paper equity'),metric('$'+num(o.open_exposure_usdc,2),'Açık exposure'),metric(o.skipped??0,'Skipped')].join('');$('assetRows').innerHTML=performanceRows(p.per_asset);$('comboRows').innerHTML=performanceRows(p.per_combo)}
async function loadRecords(){const p=params(true);$('csv').href='/api/paper-trades.csv?'+params(false).toString();const res=await fetch('/api/paper-trades?'+p.toString(),{cache:'no-store'});if(!res.ok){const body=await res.text();throw new Error(`records HTTP ${res.status}: ${body}`)}const data=await res.json();pagination=data.pagination||pagination;const rows=data.records||[];$('records').innerHTML=rows.length?rows.map(recordRow).join(''):`<tr><td class="empty" colspan="15">Henüz bu filtreye uyan paper kayıt yok. İlk kayıt 5m markette T-60, 15m markette T-240, 1h markette T-600 checkpointinde oluşur.</td></tr>`;$('recordInfo').textContent=`${pagination.total||0} toplam kayıt · bu sayfada ${rows.length}`;const page=Math.floor((pagination.offset||0)/(pagination.limit||50))+1;const pages=Math.max(1,Math.ceil((pagination.total||0)/(pagination.limit||50)));$('pageInfo').textContent=`Sayfa ${page} / ${pages}`;$('prev').disabled=!pagination.has_previous;$('next').disabled=!pagination.has_next;$('refreshState').textContent='son güncelleme '+new Date().toLocaleTimeString('tr-TR')}
async function refresh(){try{await Promise.all([loadSummary(),loadRecords()])}catch(err){$('refreshState').textContent='hata';console.error(err)}}
function apply(){offset=0;const url=new URL(location.href);url.search=params(false).toString();history.replaceState(null,'',url);refresh()}
function restore(){const p=new URLSearchParams(location.search);for(const id of ['asset','horizon','status','side','result','limit']){if(p.has(id))$(id).value=p.get(id)}if(p.has('q'))$('q').value=p.get('q')}
$('apply').onclick=apply;$('reset').onclick=()=>{for(const id of ['asset','horizon','status','side','result'])$(id).value='ALL';$('limit').value='50';$('q').value='';offset=0;history.replaceState(null,'',location.pathname);refresh()};$('prev').onclick=()=>{offset=Math.max(0,(pagination.previous_offset??0));loadRecords()};$('next').onclick=()=>{if(pagination.next_offset!=null){offset=pagination.next_offset;loadRecords()}};$('q').addEventListener('keydown',e=>{if(e.key==='Enter')apply()});restore();refresh();setInterval(refresh,10000);
</script>
</body>
</html>"""
