"""Directional Edge V2 web UI with DRY-first BTC/ETH/SOL/XRP 5m LIVE control.

The control plane is deliberately fail-closed: a real no-order DRY probe must pass
before LIVE can be armed. Read APIs remain public as before; mutations require the
operator password, same-origin/XHR checks and rate limiting.
"""
from __future__ import annotations

import asyncio
import hmac
import time
from urllib.parse import urlsplit

from aiohttp import web

import p25_web_records as base
from p25_deep_value_web import enhance_main_html
from p25_paper_records import (
    PaperRecordFilters,
    export_paper_records_csv,
    query_paper_records,
)


_ALL5M_JS = r"""
<script>
let all5mState={armed:false,dry_ready:false,halted:false,max_stake_usdc:1.10,max_price_drift_pct:.10,max_limit_price:.83,min_arm_collateral_usdc:4.40,positive_depth_only:true,last_reason:'IDLE'};
function all5mRender(s){
 all5mState=s||all5mState;
 const dry=document.getElementById('all5mDryBtn'),live=document.getElementById('all5mLiveBtn'),m=document.getElementById('all5mLiveMeta');
 if(!dry||!live||!m)return;
 const armed=!!s.armed,halted=!!s.halted,dryReady=!!s.dry_ready;
 const cap=Number(s.max_stake_usdc==null?1.10:s.max_stake_usdc).toFixed(2);
 const drift=(Number(s.max_price_drift_pct==null?.10:s.max_price_drift_pct)*100).toFixed(0);
 const px=(Number(s.max_limit_price==null?.83:s.max_limit_price)*100).toFixed(0);
 const col=Number(s.min_arm_collateral_usdc==null?4.40:s.min_arm_collateral_usdc).toFixed(2);
 dry.disabled=armed||halted;
 live.disabled=(!armed&&!dryReady)||halted;
 if(armed){live.textContent='🔴 TÜM 5m CANLI · DURDUR';live.style.background='#6f2027';live.style.borderColor='#a13b43';}
 else{live.textContent='🟢 TÜM 5m CANLIYA GEÇ';live.style.background=dryReady?'#12543f':'#3c4658';live.style.borderColor=dryReady?'#23795e':'#596579';}
 dry.textContent=dryReady?'✅ DRY PASS · TEKRAR TEST':'🟡 TÜM 5m DRY TEST';
 const dryTxt=dryReady?'DRY PASS':'DRY GEREKLİ';
 m.textContent=`BTC/ETH/SOL/XRP 5m · ${dryTxt} · FAK $1 · depth >0 · drift ≤%${drift} · hard ${px}¢ · arm bakiye ≥$${col} · ${s.last_reason||'IDLE'}`;
}
async function all5mPoll(){
 try{const r=await fetch('/api/all5m-live/status',{cache:'no-store'});if(r.ok)all5mRender(await r.json());}catch(e){}
}
function all5mPassword(){return prompt('Operatör şifresi (P25_LIVE_CONTROL_PASSWORD / P3_WEB_PASSWORD):');}
async function all5mPost(path,confirmText){
 const password=all5mPassword();if(!password)return null;
 const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'DirectionEngine-All5m'},body:JSON.stringify({password,confirm:confirmText})});
 const d=await r.json();if(d.status)all5mRender(d.status);return {r,d};
}
async function all5mDry(){
 if(!confirm('BTC/ETH/SOL/XRP 5m için gerçek DRY bağlantı testi çalışsın mı?\n\nGeoblock + kimlik + CLOB bakiye + 8 adet UP/DOWN order-book isteği yapılır.\nEMİR OLUŞTURULMAZ, post_orders ÇAĞRILMAZ.'))return;
 try{
  const x=await all5mPost('/api/all5m-live/dry','ALL 5M DRY');if(!x)return;
  const d=x.d,net=(d.checks||{}).network||{},acc=(d.checks||{}).account||{},geo=(d.checks||{}).geoblock||{};
  alert(`${d.ok?'✅ DRY PASS':'❌ DRY FAIL'}\n${d.reason||''}\nÜlke: ${geo.country||'—'}\nAuthenticated account request: ${acc.authenticated_request_ok?'PASS':'FAIL'}\nCollateral: $${Number(acc.collateral_usdc||0).toFixed(2)}\nBook requests: ${net.book_requests_ok||0}/${net.book_requests_expected||8}\npost_orders çağrıldı mı: ${net.post_orders_called?'EVET':'HAYIR'}`);
 }catch(e){alert('DRY kontrol hatası: '+e);}
 await all5mPoll();
}
async function all5mToggle(){
 const s=all5mState||{};
 if(s.armed){
  if(!confirm('BTC/ETH/SOL/XRP 5m LIVE oturumu durdurulsun mu?'))return;
  try{const x=await all5mPost('/api/all5m-live/disarm','ALL 5M DURDUR');if(x)alert((x.d.ok?'BAŞARILI: ':'RED: ')+(x.d.reason||''));}catch(e){alert('LIVE durdurma hatası: '+e);}
 }else{
  if(!s.dry_ready){alert('Önce DRY TEST PASS olmalı.');return;}
  const cap=Number(s.max_stake_usdc||1.10).toFixed(2),px=(Number(s.max_limit_price||.83)*100).toFixed(0);
  if(!confirm(`BTC/ETH/SOL/XRP 5m CANLI oturum açılacak.\n\nYalnız yeni Directional Edge V2 PAPER OPEN sinyalleri izlenir.\nHer market condition için en fazla 1 FAK $1 BUY denemesi.\nProtected fiyat altında herhangi bir pozitif likidite varsa FAK gönderilir; ne kadar dolarsa alınır, kalan miktar iptal edilir.\nMaksimum hard cap $${cap}/emir, fiyat cap ${px}¢.\nDoğrulanmış PARTIAL FILL normaldir ve LIVE devam eder.\nBelirsiz exposure/network hatası olursa tüm LIVE fail-closed HALT olur.\n\nDevam edilsin mi?`))return;
  try{const x=await all5mPost('/api/all5m-live/arm','ALL 5M CANLI');if(x)alert((x.d.ok?'✅ CANLI AKTİF: ':'❌ RED: ')+(x.d.reason||''));}catch(e){alert('LIVE arm hatası: '+e);}
 }
 await all5mPoll();
}
setInterval(all5mPoll,2000);all5mPoll();
</script>
"""


def _all5m_controls_html(*, paper: bool) -> str:
    suffix = "Paper" if paper else ""
    return (
        '<div class="xrp-live-wrap">'
        f'<button id="all5mDryBtn{suffix}" class="xrp-live-btn" onclick="all5mDry()" '
        'style="background:#6a5110;border-color:#9a781d">🟡 TÜM 5m DRY TEST</button>'
        f'<button id="all5mLiveBtn{suffix}" class="xrp-live-btn" onclick="all5mToggle()" disabled>'
        '🟢 TÜM 5m CANLIYA GEÇ</button>'
        f'<span id="all5mLiveMeta{suffix}" class="xrp-live-meta">'
        'BTC/ETH/SOL/XRP 5m · önce DRY TEST</span></div>'
    )


def _normalize_control_ids(html: str) -> str:
    # JS uses non-suffixed ids. Paper page gets the same ids because it is its own document.
    return html.replace("all5mDryBtnPaper", "all5mDryBtn").replace(
        "all5mLiveBtnPaper", "all5mLiveBtn"
    ).replace("all5mLiveMetaPaper", "all5mLiveMeta")


def _main_html() -> str:
    html = enhance_main_html(base._main_html_with_paper_link())
    old = (
        '<div class="xrp-live-wrap"><button id="xrpLiveBtn" class="xrp-live-btn" '
        'onclick="xrpLiveToggle()">🟢 XRP 5m CANLIYA GEÇ</button>'
        '<span id="xrpLiveMeta" class="xrp-live-meta">max $1.10 · sapma ≤ %10</span></div>'
    )
    html = html.replace(old, _all5m_controls_html(paper=False), 1)
    html = html.replace(
        "renderXrpLive(d.xrp5m_live_pilot||{});",
        "all5mRender(d.all5m_live||d.xrp5m_live_pilot||{});",
        1,
    )
    html = html.replace(
        '<b>XRP 5m LIVE</b> yalnız operatör ARM ederse aynı paper OPEN tetikleyicisini FOK emirle izler; maksimum notional $1.10 ve paper fill’e göre en fazla %10 fiyat sapması uygulanır.',
        '<b>ALL 5m LIVE</b> DRY PASS sonrası operatör ARM ederse BTC/ETH/SOL/XRP 5m yeni paper OPEN tetikleyicilerini $1 FAK emirle izler. Protected fiyat altında pozitif likidite varsa ne kadar dolarsa alınır; doğrulanmış kısmi fill normaldir. DRY sırasında gerçek auth/book istekleri yapılır ama emir gönderilmez.',
        1,
    )
    return html.replace("</body>", _ALL5M_JS + "\n</body>", 1)


def _paper_html() -> str:
    html = base._paper_records_html()
    old = (
        '<button id="xrpLivePaperBtn" onclick="xrpLivePaperToggle()" '
        'style="border:1px solid #23795e;background:#12543f;color:#eafff7;'
        'border-radius:6px;padding:6px 10px;font-weight:900;cursor:pointer">'
        '🟢 XRP 5m CANLIYA GEÇ</button>'
        '<span id="xrpLivePaperMeta" style="color:#8fa5c6;font-size:11px">'
        'max $1.10 · fiyat sapması ≤ %10</span>'
    )
    replacement = _normalize_control_ids(_all5m_controls_html(paper=True))
    html = html.replace(old, replacement, 1)
    return html.replace("</body>", _ALL5M_JS + "\n</body>", 1)


def _controller(engine):  # noqa: ANN001,ANN201
    getter = getattr(engine, "all5m_live_controller", None)
    if callable(getter):
        value = getter()
        if value is not None:
            return value
    legacy = getattr(engine, "xrp5m_live_pilot", None)
    return legacy() if callable(legacy) else None


def _live_status(engine) -> dict:  # noqa: ANN001
    controller = _controller(engine)
    if controller is None:
        return {
            "feature_enabled": False,
            "armed": False,
            "halted": False,
            "scope": "BTC/ETH/SOL/XRP:5m",
            "assets": ["BTC", "ETH", "SOL", "XRP"],
            "max_stake_usdc": 1.10,
            "max_price_drift_pct": 0.10,
            "max_limit_price": 0.83,
            "min_arm_collateral_usdc": 4.40,
            "min_fak_depth_usdc": 1e-9,
            "positive_depth_only": True,
            "order_mode": "MARKET_BUY_FAK_USDC",
            "partial_fill_ok": True,
            "dry_ready": False,
            "last_reason": "CONTROLLER_NOT_ATTACHED",
        }
    return dict(controller.status())


def _control_password() -> str:
    return base._control_password()


def _health_payload(engine, cfg) -> dict:  # noqa: ANN001
    latest = getattr(engine, "latest", {}) or {}
    live = _live_status(engine)
    return {
        "ok": True,
        "mode": "SHADOW",
        "phase": str(getattr(cfg, "phase", "P2.5")),
        "markets_active": sum(
            1 for card in latest.values() if isinstance(card, dict) and card.get("active")
        ),
        "paper_trading_enabled": bool(getattr(cfg, "paper_trading_enabled", False)),
        "paper_records_page": "/paper-trades",
        "paper_records_api": "/api/paper-trades",
        "paper_summary_api": "/api/paper-summary",
        "all5m_live_status_api": "/api/all5m-live/status",
        "xrp5m_live_status_api": "/api/xrp5m-live/status",
        "live_orders": int(live.get("network_cycles") or 0),
        "execution_enabled": bool(live.get("armed")) and not bool(live.get("halted")),
        "dry_ready": bool(live.get("dry_ready")),
    }


async def run_web(engine, cfg, stop: asyncio.Event) -> None:  # noqa: ANN001
    app = web.Application(client_max_size=64 * 1024)
    state_cache_sec = base._env_seconds("P25_WEB_STATE_CACHE_SEC", 5.0, 0.5)
    summary_cache_sec = base._env_seconds("P25_WEB_ANALYTICS_CACHE_SEC", 60.0, 1.0)
    include_forecast_analytics = base._env_bool("P25_WEB_SUMMARY_FORECAST_ANALYTICS", False)
    state_cache: dict[str, object] = {"value": None, "at": 0.0}
    summary_cache: dict[str, object] = {"value": None, "at": 0.0}
    state_lock = asyncio.Lock()
    summary_lock = asyncio.Lock()
    auth_failures: dict[str, list[float]] = {}

    async def cached_state() -> dict:
        now = time.monotonic(); cached = state_cache["value"]
        if isinstance(cached, dict) and now-float(state_cache["at"])<state_cache_sec:return cached
        async with state_lock:
            now=time.monotonic();cached=state_cache["value"]
            if isinstance(cached,dict) and now-float(state_cache["at"])<state_cache_sec:return cached
            try: payload=await asyncio.to_thread(engine.snapshot)
            except Exception:
                if isinstance(cached,dict):return cached
                raise
            state_cache["value"]=payload;state_cache["at"]=now;return payload

    def build_summary() -> dict:
        engine_analytics=getattr(engine,"_paper_analytics_cached",None)
        recorder_analytics=getattr(engine.recorder,"paper_analytics",None)
        if callable(engine_analytics):paper_payload=engine_analytics()
        elif callable(recorder_analytics):paper_payload=recorder_analytics(getattr(cfg,"paper_recent_limit",50))
        else:paper_payload={"enabled":False,"paper_only":True,"overall":{},"per_asset":{},"per_horizon":{},"per_combo":{},"skip_reasons":{}}
        forecast_payload={"status":"DEFERRED","reason":"P25_WEB_SUMMARY_FORECAST_ANALYTICS=false"}
        forecast=getattr(engine.recorder,"forecast_analytics",None)
        if include_forecast_analytics and callable(forecast):forecast_payload=forecast(getattr(cfg,"min_markets_for_stats",30))
        live=_live_status(engine)
        return {"paperOnly":True,"source":"sqlite","paper_trading":paper_payload,"forecast_analytics":forecast_payload,"execution":bool(live.get("armed")) and not bool(live.get("halted")),"live_orders":int(live.get("network_cycles") or 0)}

    async def cached_summary() -> dict:
        now=time.monotonic();cached=summary_cache["value"]
        if isinstance(cached,dict) and now-float(summary_cache["at"])<summary_cache_sec:return cached
        async with summary_lock:
            try:payload=build_summary()
            except Exception:
                if isinstance(cached,dict):return cached
                raise
            summary_cache["value"]=payload;summary_cache["at"]=time.monotonic();return payload

    async def index(_r):return web.Response(text=_main_html(),content_type="text/html")
    async def paper_page(_r):return web.Response(text=_paper_html(),content_type="text/html")
    async def paper_alias(_r):raise web.HTTPFound("/paper-trades")
    async def state(_r):return web.json_response(await cached_state())
    async def paper_summary(_r):return web.json_response(await cached_summary())
    async def health(_r):return web.json_response(_health_payload(engine,cfg))
    async def live_status(_r):
        payload=_live_status(engine);payload["control_available"]=bool(_control_password());return web.json_response(payload)

    async def paper_records(request):
        try:payload=query_paper_records(engine.recorder,PaperRecordFilters.from_mapping(request.query))
        except ValueError as exc:return web.json_response({"error":"INVALID_FILTER","message":str(exc)},status=400)
        return web.json_response(payload)

    async def paper_csv(request):
        try:content=export_paper_records_csv(engine.recorder,PaperRecordFilters.from_mapping(request.query,export=True))
        except ValueError as exc:return web.json_response({"error":"INVALID_FILTER","message":str(exc)},status=400)
        return web.Response(text=content,content_type="text/csv",headers={"Content-Disposition":'attachment; filename="direction-engine-paper-trades.csv"'})

    def auth_blocked(remote: str) -> bool:
        now=time.monotonic();recent=[x for x in auth_failures.get(remote,[]) if now-x<600.0];auth_failures[remote]=recent;return len(recent)>=5

    async def authorize(request):
        remote=str(request.remote or "unknown")
        if auth_blocked(remote):return None,web.json_response({"ok":False,"reason":"CONTROL_RATE_LIMIT"},status=429)
        if request.headers.get("X-Requested-With")!="DirectionEngine-All5m":return None,web.json_response({"ok":False,"reason":"CONTROL_XHR_REQUIRED"},status=403)
        origin=request.headers.get("Origin")
        if origin:
            try:
                if urlsplit(origin).netloc!=request.host:return None,web.json_response({"ok":False,"reason":"CONTROL_ORIGIN_REJECTED"},status=403)
            except ValueError:return None,web.json_response({"ok":False,"reason":"CONTROL_ORIGIN_REJECTED"},status=403)
        try:payload=await request.json()
        except Exception:return None,web.json_response({"ok":False,"reason":"INVALID_JSON"},status=400)
        secret=_control_password()
        if not secret:return None,web.json_response({"ok":False,"reason":"CONTROL_PASSWORD_NOT_CONFIGURED"},status=503)
        if not hmac.compare_digest(secret,str(payload.get("password") or "")):
            auth_failures.setdefault(remote,[]).append(time.monotonic());return None,web.json_response({"ok":False,"reason":"CONTROL_AUTH_FAILED"},status=401)
        auth_failures.pop(remote,None);return payload,None

    async def mutate(request, action: str, confirmation: str):
        payload,error=await authorize(request)
        if error is not None:return error
        if str((payload or {}).get("confirm") or "").upper()!=confirmation:return web.json_response({"ok":False,"reason":"CONFIRMATION_REQUIRED"},status=400)
        controller=_controller(engine)
        if controller is None:return web.json_response({"ok":False,"reason":"CONTROLLER_NOT_ATTACHED"},status=503)
        if action=="dry":
            discovery=getattr(getattr(engine,"hub",None),"discovery",None)
            if discovery is None or not hasattr(discovery,"snapshot_active"):return web.json_response({"ok":False,"reason":"DISCOVERY_NOT_AVAILABLE"},status=503)
            refs=list(discovery.snapshot_active().values())
            result=await asyncio.to_thread(controller.dry_probe,refs)
        elif action=="arm":result=await asyncio.to_thread(controller.arm)
        else:result=controller.disarm()
        state_cache["at"]=0.0;summary_cache["at"]=0.0
        return web.json_response(result,status=200 if result.get("ok") else 409)

    async def dry(request):return await mutate(request,"dry","ALL 5M DRY")
    async def arm(request):return await mutate(request,"arm","ALL 5M CANLI")
    async def disarm(request):return await mutate(request,"disarm","ALL 5M DURDUR")
    async def retired(_request):return web.json_response({"ok":False,"reason":"LEGACY_XRP_CONTROL_RETIRED_USE_ALL5M_DRY_FIRST"},status=410)

    app.add_routes([
        web.get("/",index),web.get("/paper",paper_alias),web.get("/paper-trades",paper_page),
        web.get("/api/state",state),web.get("/api/paper-trades",paper_records),web.get("/api/paper-summary",paper_summary),web.get("/api/paper-trades.csv",paper_csv),
        web.get("/api/all5m-live/status",live_status),web.post("/api/all5m-live/dry",dry),web.post("/api/all5m-live/arm",arm),web.post("/api/all5m-live/disarm",disarm),
        web.get("/api/xrp5m-live/status",live_status),web.post("/api/xrp5m-live/arm",retired),web.post("/api/xrp5m-live/disarm",retired),web.get("/health",health),
    ])
    runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,cfg.web_host,cfg.web_port);await site.start()
    try:await stop.wait()
    finally:await runner.cleanup()
