"""Authenticated DUAL40 operator dashboard on the existing P3 port 8093."""
from __future__ import annotations

import asyncio
from html import escape
import time
from typing import Any, Callable

from aiohttp import web

from p3_config import P3Settings
from p3_dual40_analytics import build_dual40_summary
from p3_live_preflight import run_live_preflight
from p3_live_state import LiveState, MODE_DRY
from p3_schema import connect_p3, integrity_check
from p3_web_auth import (
    AuthenticationError,
    LoginRateLimited,
    OperatorSession,
    SESSION_COOKIE,
    WebAuthManager,
)


_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    ),
}


def _headers(response: web.StreamResponse) -> web.StreamResponse:
    for key, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    return response


def _live_status(settings: P3Settings, state: LiveState | None) -> dict[str, Any]:
    if state is None:
        return {
            "mode": MODE_DRY,
            "live_feature_enabled": bool(settings.live_feature_enabled),
            "auto_execute_enabled": bool(settings.live_auto_execute_enabled),
            "reason": "no_live_state_provider",
        }
    return state.public_dict()


def _summary(
    settings: P3Settings,
    live_state: LiveState | None,
    dual40_engine: Any | None,
) -> dict[str, Any]:
    dual = (
        dual40_engine.public_status()
        if dual40_engine is not None
        else build_dual40_summary(settings.p3_db_path, limit=100)
    )
    live = _live_status(settings, live_state)
    executing = bool(live_state and live_state.can_auto_execute())
    conn = connect_p3(settings.p3_db_path)
    try:
        db_integrity = integrity_check(conn)
    finally:
        conn.close()
    return {
        "ok": True,
        "strategy_mode": settings.strategy_mode,
        "mode": live.get("mode", MODE_DRY),
        "execution_enabled": executing,
        "order_submission_enabled": executing,
        "signing_enabled": executing,
        "wallet_required": live.get("mode") != MODE_DRY,
        "live": live,
        "db_integrity": db_integrity,
        "dual40": dual,
        "now_ms": int(time.time() * 1000),
    }


def _login_html(*, error: str = "") -> str:
    error_html = f'<div class="err">{escape(error)}</div>' if error else ""
    return f"""<!doctype html><html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>DUAL40 Giriş</title>
<style>body{{margin:0;background:#07101b;color:#eef5ff;font:14px Arial,sans-serif;display:grid;place-items:center;min-height:100vh}}.card{{width:min(410px,92vw);background:#101c2c;border:1px solid #29415f;border-radius:14px;padding:24px}}h1{{font-size:20px;color:#65a9ff}}label{{display:block;margin:14px 0 5px;color:#9bb0ca}}input{{width:100%;box-sizing:border-box;padding:12px;border-radius:8px;border:1px solid #35506f;background:#07101b;color:#fff}}button{{width:100%;margin-top:18px;padding:12px;border:0;border-radius:8px;background:#315c8f;color:#fff;font-weight:800;cursor:pointer}}.err{{background:#491d26;color:#ffb4bb;padding:9px;border-radius:7px;margin:8px 0}}.mut{{color:#8ea5c3;font-size:12px;line-height:1.5}}</style></head><body><form class="card" method="post" action="/login" autocomplete="off"><h1>DUAL40 Operatör Girişi</h1>{error_html}<div class="mut">40¢ post-only maker stratejisi, LIVE kontrolü ve hard-stop bilgileri bu oturumun arkasındadır.</div><label>Kullanıcı adı</label><input name="username" autocomplete="username" required autofocus><label>Parola</label><input name="password" type="password" autocomplete="current-password" required><button type="submit">GİRİŞ YAP</button></form></body></html>"""


def build_web_app(
    settings: P3Settings,
    *,
    live_state: LiveState | None,
    dual40_engine: Any | None,
    auth_manager: WebAuthManager | None = None,
    preflight_fn: Callable[..., dict[str, Any]] = run_live_preflight,
) -> web.Application:
    auth = auth_manager or WebAuthManager(settings)

    @web.middleware
    async def security_and_auth(request: web.Request, handler):  # noqa: ANN001
        public = request.path in {"/health", "/login"}
        session: OperatorSession | None = None
        if auth.enabled and not public:
            session = auth.session_from_request(request)
            if session is None:
                if request.path.startswith("/api/"):
                    return _headers(
                        web.json_response(
                            {"ok": False, "error": "AUTH_REQUIRED"},
                            status=401,
                        )
                    )
                return _headers(web.HTTPSeeOther("/login"))
            request["p3_operator_session"] = session
        response = await handler(request)
        return _headers(response)

    app = web.Application(
        middlewares=[security_and_auth],
        client_max_size=16 * 1024,
    )
    app["p3_auth_manager"] = auth

    async def login_get(request: web.Request) -> web.Response:
        if not auth.enabled:
            return web.HTTPSeeOther("/")
        if auth.session_from_request(request) is not None:
            return web.HTTPSeeOther("/")
        return web.Response(text=_login_html(), content_type="text/html")

    async def login_post(request: web.Request) -> web.Response:
        if not auth.enabled:
            return web.HTTPSeeOther("/")
        remote = request.remote or "unknown"
        try:
            form = await request.post()
            session = auth.authenticate(
                str(form.get("username") or ""),
                str(form.get("password") or ""),
                remote=remote,
            )
        except LoginRateLimited:
            return web.Response(
                text=_login_html(error="Çok fazla hatalı giriş. Bir süre sonra tekrar dene."),
                content_type="text/html",
                status=429,
            )
        except AuthenticationError:
            return web.Response(
                text=_login_html(error="Kullanıcı adı veya parola hatalı."),
                content_type="text/html",
                status=401,
            )
        response = web.HTTPSeeOther("/")
        response.set_cookie(
            SESSION_COOKIE,
            session.token,
            max_age=int(settings.web_session_ttl_sec),
            httponly=True,
            secure=bool(settings.web_cookie_secure),
            samesite="Strict",
            path="/",
        )
        return response

    def session_of(request: web.Request) -> OperatorSession | None:
        value = request.get("p3_operator_session")
        return value if isinstance(value, OperatorSession) else None

    def csrf_ok(request: web.Request) -> bool:
        return bool(
            auth.enabled
            and auth.validate_csrf(
                session_of(request),
                request.headers.get("X-P3-CSRF"),
            )
        )

    async def index(request: web.Request) -> web.Response:
        session = session_of(request)
        csrf = session.csrf_token if session is not None else ""
        html = (
            _HTML.replace("__P3_CSRF__", csrf)
            .replace("__P3_OPERATOR__", escape(settings.web_username if session else ""))
            .replace("__P3_REFRESH_MS__", str(int(settings.web_refresh_ms)))
        )
        return web.Response(text=html, content_type="text/html")

    async def health(_request: web.Request) -> web.Response:
        live = _live_status(settings, live_state)
        executing = bool(live_state and live_state.can_auto_execute())
        return web.json_response(
            {
                "ok": True,
                "strategy": settings.strategy_mode,
                "mode": live.get("mode", MODE_DRY),
                "execution_enabled": executing,
                "order_submission_enabled": executing,
            }
        )

    async def summary(_request: web.Request) -> web.Response:
        payload = await asyncio.to_thread(
            _summary,
            settings,
            live_state,
            dual40_engine,
        )
        return web.json_response(payload)

    async def session_status(request: web.Request) -> web.Response:
        session = session_of(request)
        if auth.enabled and session is None:
            return web.json_response({"ok": False, "error": "AUTH_REQUIRED"}, status=401)
        return web.json_response(
            {
                "ok": True,
                "authenticated": bool(session) if auth.enabled else False,
                "session": auth.public_session(session) if session is not None else None,
                "auth_required": bool(auth.enabled),
            }
        )

    async def live_probe(request: web.Request) -> web.Response:
        if not auth.enabled:
            return web.json_response(
                {"ok": False, "error": "AUTH_REQUIRED_FOR_LIVE_CONTROL"},
                status=403,
            )
        if not csrf_ok(request):
            return web.json_response({"ok": False, "error": "CSRF_REJECTED"}, status=403)
        if live_state is None:
            return web.json_response({"ok": False, "error": "LIVE_STATE_UNAVAILABLE"}, status=503)
        result = await asyncio.to_thread(preflight_fn, settings, for_arming=False)
        live_state.remember_preflight(result)
        return web.json_response(result)

    async def live_arm(request: web.Request) -> web.Response:
        if not auth.enabled:
            return web.json_response(
                {"ok": False, "error": "AUTH_REQUIRED_FOR_LIVE_CONTROL"},
                status=403,
            )
        if not csrf_ok(request):
            return web.json_response({"ok": False, "error": "CSRF_REJECTED"}, status=403)
        if live_state is None:
            return web.json_response({"ok": False, "error": "LIVE_STATE_UNAVAILABLE"}, status=503)
        result = await asyncio.to_thread(preflight_fn, settings, for_arming=True)
        live_state.remember_preflight(result)
        if not result.get("ok"):
            return web.json_response(
                {
                    "ok": False,
                    "armed": False,
                    "preflight": result,
                    "state": live_state.public_dict(),
                },
                status=409,
            )
        snapshot = live_state.arm(result)
        return web.json_response(
            {
                "ok": True,
                "armed": True,
                "state": live_state.public_dict(),
                "armed_at_ms": snapshot.armed_at_ms,
            }
        )

    async def live_disarm(request: web.Request) -> web.Response:
        if not auth.enabled:
            return web.json_response(
                {"ok": False, "error": "AUTH_REQUIRED_FOR_LIVE_CONTROL"},
                status=403,
            )
        if not csrf_ok(request):
            return web.json_response({"ok": False, "error": "CSRF_REJECTED"}, status=403)
        if live_state is None:
            return web.json_response({"ok": False, "error": "LIVE_STATE_UNAVAILABLE"}, status=503)
        live_state.disarm("operator_8093")
        return web.json_response({"ok": True, "state": live_state.public_dict()})

    async def logout(request: web.Request) -> web.Response:
        if auth.enabled and not csrf_ok(request):
            return web.json_response({"ok": False, "error": "CSRF_REJECTED"}, status=403)
        if live_state is not None and live_state.snapshot().mode != MODE_DRY:
            live_state.disarm("operator_logout")
        auth.revoke_request(request)
        response = web.json_response({"ok": True, "redirect": "/login"})
        response.del_cookie(SESSION_COOKIE, path="/")
        return response

    app.add_routes(
        [
            web.get("/login", login_get),
            web.post("/login", login_post),
            web.get("/", index),
            web.get("/health", health),
            web.get("/api/summary", summary),
            web.get("/api/session", session_status),
            web.post("/api/live/probe", live_probe),
            web.post("/api/live/arm", live_arm),
            web.post("/api/live/disarm", live_disarm),
            web.post("/logout", logout),
        ]
    )
    return app


async def run_web(
    settings: P3Settings,
    stop: asyncio.Event,
    *,
    live_state: LiveState | None,
    dual40_engine: Any | None,
) -> None:
    app = build_web_app(
        settings,
        live_state=live_state,
        dual40_engine=dual40_engine,
    )
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, settings.web_host, settings.web_port)
    await site.start()
    try:
        await stop.wait()
    finally:
        if live_state is not None:
            live_state.disarm("web_shutdown")
        await runner.cleanup()


_HTML = r"""<!doctype html><html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="p3-csrf" content="__P3_CSRF__"><title>DUAL40 Maker Recovery</title>
<style>:root{--bg:#07101b;--panel:#101c2c;--line:#233650;--text:#eef5ff;--mut:#8ea5c3;--green:#20d095;--red:#f06b72;--blue:#65a9ff;--amber:#f1bd58}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:13px Inter,Arial,sans-serif}header{padding:14px 18px;border-bottom:1px solid var(--line);background:#0a1523;display:flex;gap:10px;align-items:center;flex-wrap:wrap}h1{font-size:18px;margin:0;color:var(--blue)}.pill{padding:5px 8px;border-radius:6px;background:#17375d;font-weight:800}.ok{color:var(--green)}.bad{color:var(--red)}.warn{color:var(--amber)}.wrap{max-width:1800px;margin:auto;padding:14px}.notice,.box,.metric{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:10px}.notice{color:#ffe1a0;margin-bottom:10px;line-height:1.55}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:8px;margin-top:10px}.metric b{display:block;font-size:19px}.metric span,.mut{color:var(--mut)}.box{margin-top:12px}.box h2{font-size:14px;margin:0 0 8px}table{width:100%;border-collapse:collapse}th,td{padding:7px;border-bottom:1px solid #1e3048;text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}th{color:var(--mut)}.scroll{overflow:auto;max-height:440px}.mono{font-family:ui-monospace,Consolas,monospace;font-size:11px}button{border:0;border-radius:8px;padding:10px 13px;font-weight:800;margin:3px;cursor:pointer}.probe{background:#315c8f;color:#fff}.live{background:#b91c1c;color:#fff}.dry{background:#177f62;color:#fff}.logout{margin-left:auto;background:#26384e;color:#d9e6f7}.actions{display:flex;flex-wrap:wrap;gap:5px;align-items:center}.liveout{white-space:pre-wrap;background:#07101b;padding:10px;border-radius:7px;max-height:300px;overflow:auto;margin-top:8px}.left{text-align:left!important}</style></head><body>
<header><h1>DUAL40 Maker Recovery</h1><span class="pill">40¢ + 40¢ POST-ONLY</span><span id="modepill" class="pill">DRY</span><span id="state" class="mut">yükleniyor…</span><span class="mut">Operatör: __P3_OPERATOR__</span><button class="logout" onclick="logoutNow()">ÇIKIŞ</button></header><div class="wrap">
<div id="notice" class="notice"><b>DRY / PAPER.</b> Tek global merdiven 5 → 10 → 30; 30 seviyesi de kaybederse kalıcı HARD STOP. Ekranda 41¢ görünmesi, 40¢ emrin dolduğunun kanıtı değildir: paper yalnız gerçek best ask ≤40¢ olduğunda fill sayar; LIVE yalnız token bakiye artışıyla fill doğrular.</div>
<div class="box"><h2>LIVE Kontrol — 8093</h2><div class="actions"><button class="probe" onclick="liveAct('/api/live/probe')">BAĞLANTI / KİMLİK TESTİ (EMİR YOK)</button><button class="live" onclick="confirmLive()">CANLIYA GEÇ</button><button class="dry" onclick="liveAct('/api/live/disarm')">DRY'A DÖN / EMİRLERİ İPTAL ET</button></div><div class="mut">LIVE arm için en az $35 teminat, aktif hard-stop olmaması, canlı P2.6 book transport ve maker-zero-fee doğrulaması gerekir. Restart her zaman DRY başlar.</div><pre id="liveout" class="liveout mono">Henüz operatör işlemi yapılmadı.</pre></div>
<div class="box"><h2>Durum ve Merdiven</h2><div class="grid" id="status"></div></div>
<div class="box"><h2>Paper / Live Performans</h2><div class="grid" id="performance"></div></div>
<div class="box"><h2>Aktif Cycle</h2><pre id="active" class="liveout mono">Yok</pre></div>
<div class="box"><h2>Stabilite Taraması</h2><div class="grid" id="scanmetrics"></div><div class="scroll"><table><thead><tr><th>Market</th><th>Uygun</th><th>Neden</th><th>Skor</th><th>Stabil</th><th>TTE</th><th>UP Mid</th><th>DOWN Mid</th><th>Range</th><th>Drift</th><th>Slope/s</th><th>One-way</th><th>40¢ Kuyruk UP/DN</th></tr></thead><tbody id="candidates"></tbody></table></div></div>
<div class="box"><h2>DUAL40 Cycle Günlüğü</h2><div class="scroll"><table><thead><tr><th>ID</th><th>Scope</th><th>Market</th><th>Durum</th><th>Seviye</th><th>Hedef</th><th>UP Fill</th><th>DOWN Fill</th><th>Matched</th><th>Residual</th><th>Sonuç</th><th>PnL</th><th>Pool Sonrası</th><th>41¢ Touch</th><th>Hata</th></tr></thead><tbody id="cycles"></tbody></table></div></div>
</div><script>
const CSRF=document.querySelector('meta[name="p3-csrf"]').content,$=x=>document.getElementById(x),n=(v,d=3)=>v==null?'—':Number(v).toFixed(d),pc=v=>v==null?'—':(Number(v)*100).toFixed(1)+'%',m=(v,l,c='')=>`<div class="metric"><b class="${c}">${v??'—'}</b><span>${l}</span></div>`,cls=v=>Number(v||0)>=0?'ok':'bad';
async function liveAct(path){try{const r=await fetch(path,{method:'POST',headers:{'X-P3-CSRF':CSRF},cache:'no-store'});let j={};try{j=await r.json()}catch(e){j={ok:false,error:'INVALID_RESPONSE'}};$('liveout').textContent=JSON.stringify(j,null,2);if(r.status===401){location='/login';return}await tick();}catch(e){$('liveout').textContent='HATA: '+e}}
function confirmLive(){if(confirm('DUAL40 CANLI moda geçsin mi? Uygun ilk stabil markette iki gerçek 40¢ POST-ONLY GTC emir açılır.'))liveAct('/api/live/arm')}
async function logoutNow(){try{await fetch('/logout',{method:'POST',headers:{'X-P3-CSRF':CSRF}})}finally{location='/login'}}
async function tick(){try{const r=await fetch('/api/summary',{cache:'no-store'});if(r.status===401){location='/login';return}const d=await r.json(),x=d.dual40||{},lv=d.live||{},st=x.state||{},ps=st.PAPER||{},ls=st.LIVE||{},perf=x.performance||{},pp=perf.PAPER||{},lp=perf.LIVE||{},pol=x.policy||{},scan=x.scan||{};$('state').textContent='OK · '+new Date().toLocaleTimeString();$('modepill').textContent=lv.mode||'DRY';$('modepill').className='pill '+(lv.mode==='LIVE_ARMED'?'bad':lv.mode==='LIVE_HALTED'?'warn':'ok');$('notice').innerHTML=lv.mode==='LIVE_ARMED'?'<b>CANLI MOD ARM EDİLDİ.</b> İlk uygun stabil markette 40¢ UP/DOWN post-only GTC emirleri gerçek CLOB’a gönderilebilir.':'<b>DRY / PAPER.</b> 5 → 10 → 30 global recovery; 30 sonrası HARD STOP. 41¢ yalnız near-touch tanısıdır, fill kanıtı değildir.';
$('status').innerHTML=m(d.strategy_mode,'Strateji')+m(lv.mode||'DRY','Çalışma modu',lv.mode==='LIVE_ARMED'?'bad':'ok')+m(pol.price==null?'—':Math.round(pol.price*100)+'¢','İki taraf fiyatı')+m((pol.ladder||x.ladder||[]).join(' → '),'Merdiven')+m(ps.level_index==null?'—':(x.ladder||[])[ps.level_index]+' share','Paper seviye')+m('$'+n(ps.loss_pool_usdc),'Paper zarar havuzu',ps.loss_pool_usdc>0?'warn':'ok')+m(ps.hard_stopped?'HARD STOP':'AÇIK','Paper kilidi',ps.hard_stopped?'bad':'ok')+m(ls.level_index==null?'—':(x.ladder||[])[ls.level_index]+' share','LIVE seviye')+m('$'+n(ls.loss_pool_usdc),'LIVE zarar havuzu',ls.loss_pool_usdc>0?'warn':'ok')+m(ls.hard_stopped?'HARD STOP':'AÇIK','LIVE kilidi',ls.hard_stopped?'bad':'ok')+m('$'+n(pol.full_ladder_capital_usdc),'Tam merdiven minimumu')+m('$'+n(pol.minimum_live_collateral_usdc),'LIVE arm minimumu')+m(d.db_integrity==='ok'?'SAĞLAM':d.db_integrity,'Veritabanı',d.db_integrity==='ok'?'ok':'bad');
$('performance').innerHTML=m(pp.cycles??0,'Paper cycle')+m(pp.settled??0,'Paper settled')+m((pp.wins??0)+'/'+(pp.losses??0),'Paper W/L')+m('$'+n(pp.realized_pnl_usdc),'Paper PnL',cls(pp.realized_pnl_usdc))+m(pc(pp.pair_completion_rate),'Paper çift dolum')+m(pc(pp.single_leg_rate),'Paper tek bacak')+m('$'+n(pp.max_drawdown_usdc),'Paper max DD',pp.max_drawdown_usdc>0?'warn':'ok')+m(lp.cycles??0,'LIVE cycle')+m('$'+n(lp.realized_pnl_usdc),'LIVE PnL',cls(lp.realized_pnl_usdc))+m(pc(lp.pair_completion_rate),'LIVE çift dolum');
$('active').textContent=x.active_cycle?JSON.stringify(x.active_cycle,null,2):'Aktif cycle yok.';const tr=scan.transport||{};$('scanmetrics').innerHTML=m(tr.ok?'CANLI':'YOK','Book transport',tr.ok?'ok':'bad')+m(scan.active_markets??0,'Aktif 5m market')+m(scan.eligible_markets??0,'Uygun market')+m(scan.scope||'—','Tarama scope')+m(JSON.stringify(scan.reason_counts||{}),'Red nedenleri');$('candidates').innerHTML=(scan.candidates||[]).map(v=>`<tr><td>${v.combo_key}</td><td class="${v.eligible?'ok':'bad'}">${v.eligible?'EVET':'HAYIR'}</td><td>${v.reason||'—'}</td><td>${n(v.score)}</td><td>${n(v.stable_for_sec,1)}s</td><td>${n(v.tte_sec,1)}s</td><td>${n(v.up_mid)}</td><td>${n(v.down_mid)}</td><td>${n(v.mid_range)}</td><td>${n(v.net_drift)}</td><td>${n(v.slope_per_sec,4)}</td><td>${n(v.one_way_ratio)}</td><td>${n(v.queue_ahead_up_at_40,1)} / ${n(v.queue_ahead_down_at_40,1)}</td></tr>`).join('');
$('cycles').innerHTML=(x.cycles||[]).map(v=>`<tr><td>${v.id}</td><td>${v.scope}</td><td>${v.combo_key}</td><td>${v.status}</td><td>${v.level_index}</td><td>${n(v.target_shares,1)}</td><td>${n(v.up_filled_shares,3)}</td><td>${n(v.down_filled_shares,3)}</td><td>${n(v.matched_shares,3)}</td><td>${v.residual_side||'—'} ${n(v.residual_shares,3)}</td><td>${v.official_result||'—'}</td><td class="${cls(v.realized_pnl_usdc)}">${v.realized_pnl_usdc==null?'—':'$'+n(v.realized_pnl_usdc)}</td><td>${v.loss_pool_after_usdc==null?'—':'$'+n(v.loss_pool_after_usdc)}</td><td>${v.near_touch_up_41?'UP ':''}${v.near_touch_down_41?'DN':''}</td><td>${v.error_code||'—'}</td></tr>`).join('');}catch(e){$('state').textContent='HATA · '+e}}
setInterval(tick,__P3_REFRESH_MS__);tick();</script></body></html>"""
