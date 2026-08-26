"""DEEP_VALUE_WATCH web decoration for the P2.5 dashboard.

The base P2.5 web server remains authoritative for routes and JSON APIs. This module
only decorates the existing main HTML with a per-market deep-value status panel.
No execution, credentials, signing or write endpoints are introduced.
"""
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
@media(max-width:700px){.dipgrid{grid-template-columns:repeat(2,minmax(0,1fr))}}
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
"""


def enhance_main_html(html: str) -> str:
    """Inject the dip hunter exactly once into the existing dashboard HTML."""
    if "DİP AVCISI ·" in html:
        return html
    enhanced = html.replace("</style>", _DIP_CSS + "\n</style>", 1)
    enhanced = enhanced.replace("function card(c){", _DIP_JS + "\nfunction card(c){", 1)
    marker = " ${paperLine(c)}"
    replacement = " ${deepValueBox(c)}\n ${c.paper_entry_mode==='DEEP_VALUE_WATCH'?'':paperLine(c)}"
    if marker not in enhanced:
        raise RuntimeError("P2.5 dashboard paperLine marker bulunamadi")
    return enhanced.replace(marker, replacement, 1)


async def run_web(engine, cfg, stop: asyncio.Event) -> None:  # noqa: ANN001
    """Run the normal P2.5 web server with the deep-value HTML decorator."""
    original = base_web._main_html_with_paper_link

    def decorated() -> str:
        return enhance_main_html(original())

    base_web._main_html_with_paper_link = decorated
    try:
        await base_web.run_web(engine, cfg, stop)
    finally:
        base_web._main_html_with_paper_link = original
