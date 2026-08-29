"""DEEP_VALUE_WATCH web decoration plus guarded XRP 5m LIVE operator control."""
from __future__ import annotations

import asyncio

import p25_web_records as base_web


_DIP_CSS = r"""
.dipwatch{border:1px solid #4b3f1f;background:linear-gradient(180deg,#171407,#111521);border-radius:9px;padding:9px;margin:7px 0}
.dipwatch.hit{border-color:#168760;background:linear-gradient(180deg,#08281f,#101923)}
.dipwatch.blocked{border-color:#7d3438;background:linear-gradient(180deg,#251012,#111521)}
.dipwatch.near{border-color:#8d6b20;background:linear-gradient(180deg,#251d08,#111521)}
.diphead{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:6px}
.diptitle{font-weight:900;color:#ffd36f;letter-spacing:.2px}.dipwatch.hit .diptitle{color:#6af0bf}.dipwatch.blocked .diptitle{color:#ff9498}
.dipstate{font-size:10px;font-weight:900;padding:3px 7px;border-radius:5px;background:#26334a;color:#dfe9fa;white-space:nowrap}
.dipwatch.hit .dipstate{background:#09684c;color:#b8f9e2}.dipwatch.blocked .dipstate{background:#74252a;color:#ffd4d6}.dipwatch.near .dipstate{background:#5e4809;color:#ffe8a6}
.dipgrid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:5px}.dipmetric{background:#0b1422;border:1px solid #21304a;border-radius:6px;padding:6px;min-width:0}.dipmetric span{display:block;color:#849ab9;font-size:10px;margin-bottom:2px}.dipmetric b{display:block;color:#edf5ff;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dipreason{margin-top:6px;color:#9fb1cc;font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.xrp-live-wrap{display:flex;align-items:center;gap:7px;margin-left:auto;flex-wrap:wrap}.xrp-live-btn{border:1px solid #23795e;background:#12543f;color:#eafff7;border-radius:7px;padding:7px 10px;font-weight:900;cursor:pointer;font-size:11px}.xrp-live-btn.on{background:#6f2027;border-color:#a13b43;color:#ffe4e6}.xrp-live-btn.used{background:#5b450d;border-color:#8c6c1d;color:#fff0b8}.xrp-live-meta{color:#91a6c6;font-size:10px;max-width:390px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
@media(max-width:700px){.dipgrid{grid-template-columns:repeat(2,minmax(0,1fr))}.xrp-live-wrap{width:100%;margin-left:0}.xrp-live-meta{max-width:100%}}
"""


_DIP_JS = r"""
function deepValueBox(c){
 if(c.paper_entry_mode!=='DEEP_VALUE_WATCH')return '';
 const paper=c.paper_trade||null;
 const side=(c.forecast_direction||'').toUpperCase();
 const askRaw=side==='UP'?c.up_ask:side==='DOWN'?c.down_ask:null;
 const ask=askRaw==null?null:Number(askRaw);
 const pUp=c.forecast_p_up==null?null:Number(c.forecast_p_up);
 const prob=pUp==null?null:(side==='UP'?pUp:side==='DOWN'?1-pUp:null);
 const minAsk=Number(c.paper_deep_value_min_ask==null?0.01:c.paper_deep_value_min_ask);
 const maxAsk=Number(c.paper_deep_value_max_ask==null?0.10:c.paper_deep_value_max_ask);
 const stake=Number(c.paper_deep_value_stake_usdc==null?1:c.paper_deep_value_stake_usdc);
 const slip=Number(c.paper_deep_value_slippage==null?0.005:c.paper_deep_value_slippage);
 const minValue=Number(c.paper_deep_value_min_value_multiple==null?1.5:c.paper_deep_value_min_value_multiple);
 const fill=ask==null?null:Math.min(.999,ask+slip);
 const shares=fill&&fill>0?stake/fill:null;
 const value=prob!=null&&fill&&fill>0?prob/fill:null;
 const distance=ask==null?null:(ask-maxAsk);
 const reason=String(c.paper_deep_value_watch_reason||'');
 const isTrade=paper&&paper.entry_mode==='DEEP_VALUE_WATCH'&&['OPEN','SETTLED'].includes(paper.status);
 const isSettled=isTrade&&paper.status==='SETTLED';
 let state='DİP BEKLENİYOR',klass='';
 if(isTrade){state=isSettled?(paper.correct?'DİP TUTTU':'DİP KAÇTI'):'🔥 DİP YAKALANDI';klass=paper.correct===0?'blocked':'hit';}
 else if(side!=='UP'&&side!=='DOWN'){state='TAHMİN BEKLENİYOR';}
 else if(ask==null){state='ASK BEKLENİYOR';}
 else if(ask<minAsk){state='FİYAT ALT SINIRINDA';klass='blocked';}
 else if(ask<=maxAsk){
   if(reason.startsWith('DEPTH_')||reason.includes('FEE_')||reason.includes('BOOK_')){state='DİP VAR · FILL BLOKE';klass='blocked';}
   else if(reason&&reason!=='OPEN'&&reason!=='OK'){state='DİP VAR · MODEL GATE';klass='blocked';}
   else{state='DİP BÖLGESİ';klass='near';}
 }else if(distance!=null&&distance<=0.03){state='DİBE YAKIN';klass='near';}
 const askText=ask==null?'—':(ask*100).toFixed(1)+'¢';
 const targetText='≤ '+(maxAsk*100).toFixed(1)+'¢';
 const distText=distance==null?'—':distance<=0?'HEDEFTE':('+'+(distance*100).toFixed(1)+'¢');
 const shareText=shares==null?'—':shares.toFixed(2);
 const probText=prob==null?'—':(prob*100).toFixed(1)+'%';
 const valueText=value==null?'—':value.toFixed(2)+'x';
 const depthAge=paper&&paper.depth_age_ms!=null?Number(paper.depth_age_ms):c.paper_deep_value_depth_age_ms;
 let depthText='BEKLENİYOR';
 if(isTrade&&paper.depth_capacity_shares!=null)depthText='HAZIR '+Number(paper.depth_capacity_shares).toFixed(1)+' sh';
 else if(reason.startsWith('DEPTH_INSUFFICIENT'))depthText='YETERSİZ';
 else if(reason.startsWith('DEPTH_STALE'))depthText='STALE';
 else if(reason==='DEPTH_MISSING'||reason==='ASK_DEPTH_EMPTY')depthText='EKSİK';
 const ageText=depthAge==null?'—':Number(depthAge).toFixed(0)+' ms';
 const pnlText=isSettled&&paper.realized_pnl!=null?usd(paper.realized_pnl):isTrade?'AÇIK':'—';
 const band=paper&&paper.price_band?paper.price_band:(c.paper_deep_value_price_band||'—');
 const reasonText=isTrade?'DEEP_VALUE_WATCH · '+(paper.price_band||band):reason||'WAITING_FOR_DIP';
 return `<div class="dipwatch ${klass}">
   <div class="diphead"><span class="diptitle">DİP AVCISI · ${side||'—'}</span><span class="dipstate">${state}</span></div>
   <div class="dipgrid">
    <div class="dipmetric"><span>Canlı ask</span><b>${askText}</b></div>
    <div class="dipmetric"><span>Dip hedefi</span><b>${targetText} · ${distText}</b></div>
    <div class="dipmetric"><span>Model olasılığı</span><b>${probText}</b></div>
    <div class="dipmetric"><span>$${stake.toFixed(2)} teorik share</span><b>${shareText}</b></div>
    <div class="dipmetric"><span>Value / min</span><b>${valueText} / ${minValue.toFixed(2)}x</b></div>
    <div class="dipmetric"><span>Full depth / yaş</span><b>${depthText} · ${ageText}</b></div>
    <div class="dipmetric"><span>Fiyat bandı</span><b>${band}</b></div>
    <div class="dipmetric"><span>Paper durum</span><b>${pnlText}</b></div>
    <div class="dipmetric"><span>Fill tahmini</span><b>${fill==null?'—':(fill*100).toFixed(1)+'¢'}</b></div>
   </div>
   <div class="dipreason">${reasonText}</div>
 </div>`;
}

let xrpLiveState={armed:false,arm_consumed:false,max_stake_usdc:1.10,max_price_drift_pct:0.10,last_reason:'IDLE'};
function renderXrpLive(s){
 xrpLiveState=s||xrpLiveState;
 const b=document.getElementById('xrpLiveBtn'),m=document.getElementById('xrpLiveMeta');
 if(!b||!m)return;
 const armed=!!s.armed,consumed=!!s.arm_consumed;
 const cap=Number(s.max_stake_usdc==null ? 1.10 : s.max_stake_usdc).toFixed(2);
 const drift=(Number(s.max_price_drift_pct==null ? 0.10 : s.max_price_drift_pct)*100).toFixed(0);
 b.className='xrp-live-btn'+(armed&&!consumed?' on':consumed?' used':'');
 if(armed&&!consumed)b.textContent='🔴 XRP 5m CANLI · DURDUR';
 else if(consumed)b.textContent='XRP 5m YENİDEN CANLIYA GEÇ';
 else b.textContent='🟢 XRP 5m CANLIYA GEÇ';
 m.textContent=`max $${cap} · sapma ≤ %${drift} · ${s.last_reason||'IDLE'}`;
}
async function xrpLiveToggle(){
 const s=xrpLiveState||{};
 const action=(s.armed&&!s.arm_consumed)?'disarm':'arm';
 if(action==='arm'){
   const cap=Number(s.max_stake_usdc==null ? 1.10 : s.max_stake_usdc).toFixed(2);
   const drift=(Number(s.max_price_drift_pct==null ? 0.10 : s.max_price_drift_pct)*100).toFixed(0);
   if(!confirm(`XRP 5 dakika gerçek para pilotu ARM edilecek.\n\nMaksimum notional: $${cap}\nPaper fill'e göre izin verilen fiyat sapması: en fazla %${drift}\nBir ARM = en fazla bir gerçek network submit cycle.\n\nDevam edilsin mi?`))return;
 }else if(!confirm('XRP 5m LIVE ARM durdurulsun mu?'))return;
 const password=prompt('Operatör şifresi (P3_WEB_PASSWORD / P25_LIVE_CONTROL_PASSWORD):');
 if(!password)return;
 try{
   const r=await fetch('/api/xrp5m-live/'+action,{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'DirectionEngine-XRP5m'},body:JSON.stringify({password,confirm:action==='arm'?'XRP 5M CANLI':'XRP 5M DURDUR'})});
   const d=await r.json();
   if(d.status)renderXrpLive(d.status);
   alert((d.ok?'BAŞARILI: ':'RED: ')+(d.reason||d.error||('HTTP '+r.status)));
 }catch(e){alert('XRP LIVE kontrol hatası: '+e);}
}
"""


def enhance_main_html(html: str) -> str:
    """Inject dip hunter and guarded LIVE control exactly once."""
    if "DİP AVCISI ·" in html and "xrpLiveBtn" in html:
        return html
    enhanced = html.replace("</style>", _DIP_CSS + "\n</style>", 1)
    enhanced = enhanced.replace("function card(c){", _DIP_JS + "\nfunction card(c){", 1)
    marker = " ${paperLine(c)}"
    replacement = " ${deepValueBox(c)}\n ${c.paper_entry_mode==='DEEP_VALUE_WATCH'?'':paperLine(c)}"
    if marker not in enhanced:
        raise RuntimeError("P2.5 dashboard paperLine marker bulunamadi")
    enhanced = enhanced.replace(marker, replacement, 1)
    enhanced = enhanced.replace(
        '<span class="mut" id="up"></span>',
        '<span class="mut" id="up"></span>'
        '<div class="xrp-live-wrap"><button id="xrpLiveBtn" class="xrp-live-btn" '
        'onclick="xrpLiveToggle()">🟢 XRP 5m CANLIYA GEÇ</button>'
        '<span id="xrpLiveMeta" class="xrp-live-meta">max $1.10 · sapma ≤ %10</span></div>',
        1,
    )
    enhanced = enhanced.replace(
        "$('up').textContent='uptime '+Math.round(d.uptime_sec||0)+'s';",
        "$('up').textContent='uptime '+Math.round(d.uptime_sec||0)+'s';renderXrpLive(d.xrp5m_live_pilot||{});",
        1,
    )
    enhanced = enhanced.replace(
        '<div class="banner"><b>TAHMİN</b> research ensemble’dır. <b>SİNYAL</b> yalnız doğrulama geçince açılır. <b>PAPER TRADE</b> seçilen tarafı gerçek best ask + slippage ile simüle eder; emir, imza ve private key yoktur.</div>',
        '<div class="banner"><b>TAHMİN</b> research ensemble’dır. <b>PAPER</b> $1 simülasyondur. '
        '<b>XRP 5m LIVE</b> yalnız operatör ARM ederse, aynı paper OPEN tetikleyicisini gerçek FOK emirle izler; '
        'maksimum notional $1.10 ve paper fill’e göre en fazla %10 fiyat sapması uygulanır.</div>',
        1,
    )
    return enhanced


async def run_web(engine, cfg, stop: asyncio.Event) -> None:  # noqa: ANN001
    original = base_web._main_html_with_paper_link

    def decorated() -> str:
        return enhance_main_html(original())

    base_web._main_html_with_paper_link = decorated
    try:
        await base_web.run_web(engine, cfg, stop)
    finally:
        base_web._main_html_with_paper_link = original
