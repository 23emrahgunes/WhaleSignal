"""Authenticated DUAL40 operator dashboard on the existing P3 port 8093."""
from __future__ import annotations

import asyncio
from html import escape
import threading
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


class _DatabaseIntegrityCache:
    def __init__(self, path: str, *, ttl_sec: float = 60.0) -> None:
        self.path = str(path)
        self.ttl_sec = max(1.0, float(ttl_sec))
        self._value = "checking"
        self._checked_at = 0.0
        self._lock = threading.Lock()

    def get(self) -> str:
        now = time.monotonic()
        if now - self._checked_at < self.ttl_sec:
            return self._value

        # An integrity scan may briefly outlive an HTTP request. A concurrent
        # refresh should still return the operational data instead of queueing
        # behind the same scan.
        if not self._lock.acquire(blocking=False):
            return self._value
        try:
            now = time.monotonic()
            if now - self._checked_at < self.ttl_sec:
                return self._value
            try:
                conn = connect_p3(self.path, read_only=True)
                try:
                    self._value = integrity_check(conn)
                finally:
                    conn.close()
            except Exception as exc:  # noqa: BLE001
                self._value = f"ERROR:{type(exc).__name__}"
            self._checked_at = time.monotonic()
            return self._value
        finally:
            self._lock.release()


def _summary(
    settings: P3Settings,
    live_state: LiveState | None,
    dual40_engine: Any | None,
    integrity_cache: _DatabaseIntegrityCache | None = None,
) -> dict[str, Any]:
    dual = (
        dual40_engine.public_status()
        if dual40_engine is not None
        else build_dual40_summary(settings.p3_db_path, limit=100)
    )
    live = _live_status(settings, live_state)
    executing = bool(live_state and live_state.can_auto_execute())
    db_integrity = (
        integrity_cache.get()
        if integrity_cache is not None
        else _DatabaseIntegrityCache(settings.p3_db_path).get()
    )
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
    integrity_cache = _DatabaseIntegrityCache(settings.p3_db_path)

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
            integrity_cache,
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


_HTML = r"""<!doctype html><html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="p3-csrf" content="__P3_CSRF__"><title>DUAL40 Operasyon Paneli</title>
<style>:root{--bg:#0a0c0f;--surface:#11151a;--surface-2:#171c22;--line:#2b323b;--line-soft:#20262d;--text:#edf1f5;--mut:#9ba6b2;--green:#35d6a0;--red:#ff6b72;--blue:#67aaf9;--amber:#f5bd4f;--cyan:#47c8d1}*{box-sizing:border-box;letter-spacing:0}body{margin:0;background:var(--bg);color:var(--text);font:13px Inter,Segoe UI,Arial,sans-serif;min-width:320px}.topbar{position:sticky;top:0;z-index:10;min-height:58px;padding:10px max(16px,calc((100vw - 1660px)/2));border-bottom:1px solid var(--line);background:rgba(10,12,15,.96);display:flex;align-items:center;gap:12px}.brand{display:flex;align-items:center;gap:10px;min-width:0}.brand-mark{width:30px;height:30px;border:1px solid #3a4652;border-radius:6px;display:grid;place-items:center;color:var(--green);font-weight:900}.brand-copy h1{font-size:16px;line-height:1.1;margin:0}.brand-copy span{display:block;color:var(--mut);font-size:11px;margin-top:3px}.header-state{display:flex;align-items:center;gap:8px;margin-left:auto}.pill{padding:5px 8px;border-radius:5px;background:#20262d;border:1px solid #343d47;font-size:12px;font-weight:800}.ok{color:var(--green)}.bad{color:var(--red)}.warn{color:var(--amber)}.mut{color:var(--mut)}.wrap{max-width:1660px;margin:auto;padding:0 18px 28px}.notice{margin:14px 0 0;padding:10px 12px;border-left:3px solid var(--amber);background:#17191a;color:#e8d3a2;line-height:1.45}.section{padding:18px 0;border-bottom:1px solid var(--line)}.section-head{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;margin-bottom:10px}.section-head h2,.section-head h3{font-size:14px;margin:0}.section-head p{margin:3px 0 0;color:var(--mut);font-size:12px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:8px}.summary-grid{grid-template-columns:repeat(6,minmax(125px,1fr))}.metric{min-width:0;background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:10px 11px}.metric b{display:block;min-height:23px;font-size:18px;line-height:1.25;overflow-wrap:anywhere}.metric span{display:block;color:var(--mut);font-size:11px;margin-top:3px}.lane-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:9px;margin-top:10px}.lane{min-width:0;background:var(--surface);border:1px solid var(--line);border-top:3px solid var(--green);border-radius:6px;padding:12px}.lane.warn-lane{border-top-color:var(--amber)}.lane.bad-lane{border-top-color:var(--red)}.lane-head{display:flex;align-items:center;justify-content:space-between;gap:8px}.lane-asset{font-size:17px;font-weight:900}.lane-badge{font-size:10px;font-weight:900;padding:3px 6px;border-radius:4px;background:#1d2925;color:var(--green)}.warn-lane .lane-badge{background:#302817;color:var(--amber)}.bad-lane .lane-badge{background:#321b1e;color:var(--red)}.lane-values{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:12px}.lane-value b{display:block;font-size:15px;overflow-wrap:anywhere}.lane-value span{display:block;color:var(--mut);font-size:10px;margin-top:2px}.lane-foot{margin-top:10px;padding-top:8px;border-top:1px solid var(--line-soft);color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.workspace{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(310px,.7fr);gap:24px}.side-column{border-left:1px solid var(--line);padding-left:24px}.side-column .section:last-child{border-bottom:0}.status-line{display:flex;flex-wrap:wrap;gap:7px;color:var(--mut);font-size:11px;margin-bottom:9px}.status-line span{padding:4px 7px;background:var(--surface);border:1px solid var(--line-soft);border-radius:4px}.table-wrap{width:100%;overflow:auto;border:1px solid var(--line);border-radius:6px;background:var(--surface)}table{width:100%;border-collapse:collapse}th,td{padding:8px 9px;border-bottom:1px solid var(--line-soft);text-align:left;white-space:nowrap}tr:last-child td{border-bottom:0}th{position:sticky;top:0;background:#151a20;color:var(--mut);font-size:10px;text-transform:uppercase;font-weight:700}td{font-size:12px}.primary-table{min-width:760px}.primary-table th,.primary-table td{white-space:normal}.primary-table .price-pair{white-space:nowrap}.cell-main{font-weight:750}.cell-code{display:block;max-width:210px;color:var(--mut);font:10px ui-monospace,Consolas,monospace;overflow:hidden;text-overflow:ellipsis}.price-pair{font-variant-numeric:tabular-nums}.empty{padding:18px;border:1px dashed #343c45;border-radius:6px;color:var(--mut);text-align:center}.active-list{display:grid;gap:7px}.active-row{display:grid;grid-template-columns:70px minmax(230px,1.1fr) minmax(260px,1fr) auto;gap:12px;align-items:center;padding:10px 11px;background:var(--surface);border-left:3px solid var(--amber)}.active-row>b{font-size:12px}.active-row span{color:var(--mut);font-size:11px;overflow-wrap:anywhere}.active-order{display:grid;gap:2px;min-width:0}.active-order strong{font-size:12px}.active-fill{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;min-width:0}.active-fill b{display:block;color:var(--text);font-size:10px}.control-panel,.diagnostics{margin-top:14px;border:1px solid var(--line);border-radius:6px;background:var(--surface)}summary{list-style:none;cursor:pointer;padding:12px 14px;font-weight:800;display:flex;align-items:center;justify-content:space-between;gap:12px}summary::-webkit-details-marker{display:none}summary:after{content:'+';font-size:18px;color:var(--mut)}details[open]>summary:after{content:'−'}.details-body{padding:0 14px 14px;border-top:1px solid var(--line-soft)}.actions{display:flex;flex-wrap:wrap;gap:7px;align-items:center;margin-top:12px}button{border:1px solid transparent;border-radius:6px;padding:9px 12px;font-weight:800;cursor:pointer}.probe{background:#244b75;color:#fff}.live{background:#b8282e;color:#fff}.dry{background:#197357;color:#fff}.logout{background:#1a2026;border-color:#343d47;color:#dfe6ee}.liveout{white-space:pre-wrap;background:#090b0d;border:1px solid var(--line-soft);padding:10px;border-radius:5px;max-height:180px;overflow:auto;margin-top:9px}.diagnostic-group{margin-top:10px;border-top:1px solid var(--line-soft)}.diagnostic-group summary{padding:10px 0}.diagnostic-group .details-body{padding:0 0 12px;border:0}.scroll{overflow:auto;max-height:360px}.mono{font-family:ui-monospace,Consolas,monospace;font-size:11px}.operator{white-space:nowrap}.left{text-align:left!important}@media(max-width:1150px){.summary-grid{grid-template-columns:repeat(3,minmax(125px,1fr))}.lane-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.workspace{grid-template-columns:1fr}.side-column{border-left:0;padding-left:0}.desktop-optional{display:none}.active-row{grid-template-columns:70px minmax(220px,1fr) minmax(240px,1fr) auto}}@media(max-width:680px){.topbar{position:static;align-items:flex-start;flex-wrap:wrap}.header-state{width:100%;margin-left:0}.operator{display:none}.logout{margin-left:auto}.wrap{padding:0 12px 20px}.summary-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.lane-grid{grid-template-columns:1fr}.lane-values{grid-template-columns:repeat(3,minmax(0,1fr))}.section{padding:14px 0}.metric b{font-size:16px}.actions button{width:100%;margin:0}.notice{font-size:12px}.active-row{grid-template-columns:50px minmax(0,1fr) auto}.active-fill{grid-column:1/-1}}</style></head><body>
<style>.workspace,.main-column,.side-column{min-width:0}.table-wrap{max-width:100%}@media(max-width:680px){.mobile-optional{display:none}.primary-table{min-width:0;table-layout:fixed}.primary-table th,.primary-table td{padding:7px 6px}.primary-table th:nth-child(1){width:19%}.primary-table th:nth-child(2){width:18%}.primary-table th:nth-child(3){width:39%}.primary-table th:nth-child(6){width:24%}.cell-code{max-width:120px}}</style>
<header class="topbar"><div class="brand"><div class="brand-mark">D40</div><div class="brand-copy"><h1>DUAL40 Operasyon</h1><span>40¢ + 40¢ · POST-ONLY GTC</span></div></div><div class="header-state"><span id="modepill" class="pill">DRY</span><span id="state" class="mut">Yükleniyor…</span><span class="mut operator">__P3_OPERATOR__</span><button class="logout" onclick="logoutNow()">Çıkış</button></div></header><main class="wrap">
<div id="notice" class="notice"><b>DRY / PAPER.</b> Açılan cycle'da UP ve DOWN için ayrı 40¢ sanal limit emir izlenir. 41¢ near-touch yalnız tanıdır; fill kanıtı değildir.</div>
<section class="section"><div class="section-head"><div><h2>Genel Bakış</h2><p>Çalışma modu, kapılar ve asset bazlı recovery durumu</p></div></div><div class="grid summary-grid" id="status"></div><div class="lane-grid" id="lanes"></div></section>
<div class="workspace"><div class="main-column">
<section class="section"><div class="section-head"><div><h2>Market Taraması</h2><p>Şu an değerlendirmede olan 5 dakikalık marketler</p></div></div><div class="status-line" id="scanmetrics"></div><div class="table-wrap"><table class="primary-table"><thead><tr><th>Market</th><th>Karar</th><th>Neden</th><th class="mobile-optional">Hedef</th><th class="mobile-optional">TTE</th><th>UP / DOWN</th><th class="mobile-optional">Stabilite</th><th class="desktop-optional">Opening</th><th class="desktop-optional">Forecast</th><th class="mobile-optional">Lane</th></tr></thead><tbody id="candidates"></tbody></table></div></section>
<section class="section"><div class="section-head"><div><h2>Aktif Cycle'lar</h2><p>Emir veya sonuç bekleyen PAPER/LIVE cycle'ları</p></div></div><div id="active" class="active-list"><div class="empty">Aktif cycle yok.</div></div></section>
</div><aside class="side-column">
<section class="section"><div class="section-head"><div><h3>Paper Performansı</h3><p>Gerçekleşmiş sonuçların kısa özeti</p></div></div><div class="grid" id="performance"></div></section>
<section class="section"><div class="section-head"><div><h3>P2.6 Readiness</h3><p>Tahmin Paper audit durumu</p></div></div><div class="grid" id="p26metrics"></div></section>
<section class="section"><div class="section-head"><div><h3>Gate Kohortları</h3><p>SHADOW karşılaştırma özeti</p></div></div><div class="grid" id="cohorts"></div></section>
</aside></div>
<details class="control-panel"><summary><span>LIVE Kontrol</span><span class="mut">Varsayılan kapalı · işlem öncesi preflight zorunlu</span></summary><div class="details-body"><div class="actions"><button class="probe" onclick="liveAct('/api/live/probe')">Bağlantı ve kimlik testi</button><button class="live" onclick="confirmLive()">Canlıya geç</button><button class="dry" onclick="liveAct('/api/live/disarm')">DRY'a dön ve emirleri iptal et</button></div><p class="mut">LIVE arm için en az $35 teminat, aktif hard-stop olmaması, canlı P2.6 book transport ve maker-zero-fee doğrulaması gerekir. Restart her zaman DRY başlar.</p><pre id="liveout" class="liveout mono">Henüz operatör işlemi yapılmadı.</pre></div></details>
<details class="diagnostics"><summary><span>Teşhis ve Günlükler</span><span class="mut">Teknik tablolar varsayılan olarak gizlidir</span></summary><div class="details-body">
<details class="diagnostic-group"><summary>P2.6 Son Kararlar</summary><div class="details-body"><div class="scroll"><table><thead><tr><th>Market</th><th>Aşama</th><th>Karar</th><th>Neden</th><th>Would Open</th><th>Gözlem</th></tr></thead><tbody id="p26decisions"></tbody></table></div></div></details>
<details class="diagnostic-group"><summary>Market Karar Günlüğü</summary><div class="details-body"><div class="scroll"><table><thead><tr><th>Asset</th><th>Condition</th><th>Karar</th><th>Neden</th><th>Opening</th><th>Forecast</th><th>Görüldü</th><th>Cycle ID</th></tr></thead><tbody id="decisions"></tbody></table></div></div></details>
<details class="diagnostic-group"><summary>DUAL40 Cycle Günlüğü</summary><div class="details-body"><div class="scroll"><table><thead><tr><th>ID</th><th>Scope</th><th>Asset</th><th>Market</th><th>Durum</th><th>Seviye</th><th>Her Bacak</th><th>Limit</th><th>UP Fill</th><th>DOWN Fill</th><th>Matched</th><th>Residual</th><th>Sonuç</th><th>PnL</th><th>Pool Sonrası</th><th>41¢ Touch</th><th>Hata</th></tr></thead><tbody id="cycles"></tbody></table></div></div></details>
</div></details></main>
<script>
const CSRF=document.querySelector('meta[name="p3-csrf"]').content,$=x=>document.getElementById(x),n=(v,d=3)=>v==null?'—':Number(v).toFixed(d),pc=v=>v==null?'—':(Number(v)*100).toFixed(1)+'%',m=(v,l,c='')=>`<div class="metric"><b class="${c}">${v??'—'}</b><span>${l}</span></div>`,cls=v=>Number(v||0)>=0?'ok':'bad';
async function liveAct(path){try{const r=await fetch(path,{method:'POST',headers:{'X-P3-CSRF':CSRF},cache:'no-store'});let j={};try{j=await r.json()}catch(e){j={ok:false,error:'INVALID_RESPONSE'}};$('liveout').textContent=JSON.stringify(j,null,2);if(r.status===401){location='/login';return}await tick();}catch(e){$('liveout').textContent='HATA: '+e}}
function confirmLive(){if(confirm('DUAL40 CANLI moda geçsin mi? Uygun ilk stabil markette iki gerçek 40¢ POST-ONLY GTC emir açılır.'))liveAct('/api/live/arm')}
async function logoutNow(){try{await fetch('/logout',{method:'POST',headers:{'X-P3-CSRF':CSRF}})}finally{location='/login'}}
async function tick(){try{const r=await fetch('/api/summary',{cache:'no-store'});if(r.status===401){location='/login';return}const d=await r.json(),x=d.dual40||{},lv=d.live||{},st=x.state||{},ps=st.PAPER||{},ls=st.LIVE||{},perf=x.performance||{},pp=perf.PAPER||{},lp=perf.LIVE||{},pol=x.policy||{},scan=x.scan||{};$('state').textContent='OK · '+new Date().toLocaleTimeString();$('modepill').textContent=lv.mode||'DRY';$('modepill').className='pill '+(lv.mode==='LIVE_ARMED'?'bad':lv.mode==='LIVE_HALTED'?'warn':'ok');$('notice').innerHTML=lv.mode==='LIVE_ARMED'?'<b>CANLI MOD ARM EDİLDİ.</b> İlk uygun stabil markette 40¢ UP/DOWN post-only GTC emirleri gerçek CLOB’a gönderilebilir.':'<b>DRY / PAPER.</b> BTC/ETH/SOL/XRP bağımsız 5 → 10 → 30 recovery lane çalışır; 30 sonrası asset bazlı HARD STOP. 41¢ yalnız near-touch tanısıdır, fill kanıtı değildir.';
$('status').innerHTML=m(d.strategy_mode,'Strateji')+m(lv.mode||'DRY','Çalışma modu',lv.mode==='LIVE_ARMED'?'bad':'ok')+m(pol.price==null?'—':Math.round(pol.price*100)+'¢','İki taraf fiyatı')+m((pol.ladder||x.ladder||[]).join(' → '),'Merdiven')+m(ps.level_index==null?'—':(x.ladder||[])[ps.level_index]+' share','Paper seviye')+m('$'+n(ps.loss_pool_usdc),'Paper zarar havuzu',ps.loss_pool_usdc>0?'warn':'ok')+m(ps.hard_stopped?'HARD STOP':'AÇIK','Paper kilidi',ps.hard_stopped?'bad':'ok')+m(ls.level_index==null?'—':(x.ladder||[])[ls.level_index]+' share','LIVE seviye')+m('$'+n(ls.loss_pool_usdc),'LIVE zarar havuzu',ls.loss_pool_usdc>0?'warn':'ok')+m(ls.hard_stopped?'HARD STOP':'AÇIK','LIVE kilidi',ls.hard_stopped?'bad':'ok')+m('$'+n(pol.full_ladder_capital_usdc),'Tam merdiven minimumu')+m('$'+n(pol.minimum_live_collateral_usdc),'LIVE arm minimumu')+m(d.db_integrity==='ok'?'SAĞLAM':d.db_integrity,'Veritabanı',d.db_integrity==='ok'?'ok':'bad');
$('performance').innerHTML=m(pp.cycles??0,'Paper cycle')+m(pp.settled??0,'Paper settled')+m((pp.wins??0)+'/'+(pp.losses??0),'Paper W/L')+m('$'+n(pp.realized_pnl_usdc),'Paper PnL',cls(pp.realized_pnl_usdc))+m(pc(pp.pair_completion_rate),'Paper çift dolum')+m(pc(pp.single_leg_rate),'Paper tek bacak')+m('$'+n(pp.max_drawdown_usdc),'Paper max DD',pp.max_drawdown_usdc>0?'warn':'ok')+m(lp.cycles??0,'LIVE cycle')+m('$'+n(lp.realized_pnl_usdc),'LIVE PnL',cls(lp.realized_pnl_usdc))+m(pc(lp.pair_completion_rate),'LIVE çift dolum');
$('active').textContent=x.active_cycle?JSON.stringify(x.active_cycle,null,2):'Aktif cycle yok.';const tr=scan.transport||{};$('scanmetrics').innerHTML=m(tr.ok?'CANLI':'YOK','Book transport',tr.ok?'ok':'bad')+m(scan.active_markets??0,'Aktif 5m market')+m(scan.eligible_markets??0,'Uygun market')+m(scan.scope||'—','Tarama scope')+m(JSON.stringify(scan.reason_counts||{}),'Red nedenleri');$('candidates').innerHTML=(scan.candidates||[]).map(v=>`<tr><td>${v.combo_key}</td><td class="${v.eligible?'ok':'bad'}">${v.eligible?'EVET':'HAYIR'}</td><td>${v.reason||'—'}</td><td>${n(v.score)}</td><td>${n(v.stable_for_sec,1)}s</td><td>${n(v.tte_sec,1)}s</td><td>${n(v.up_mid)}</td><td>${n(v.down_mid)}</td><td>${n(v.mid_range)}</td><td>${n(v.net_drift)}</td><td>${n(v.slope_per_sec,4)}</td><td>${n(v.one_way_ratio)}</td><td>${n(v.queue_ahead_up_at_40,1)} / ${n(v.queue_ahead_down_at_40,1)}</td></tr>`).join('');
$('cycles').innerHTML=(x.cycles||[]).map(v=>`<tr><td>${v.id}</td><td>${v.scope}</td><td>${v.combo_key}</td><td>${v.status}</td><td>${v.level_index}</td><td>${n(v.target_shares,1)}</td><td>${n(v.up_filled_shares,3)}</td><td>${n(v.down_filled_shares,3)}</td><td>${n(v.matched_shares,3)}</td><td>${v.residual_side||'—'} ${n(v.residual_shares,3)}</td><td>${v.official_result||'—'}</td><td class="${cls(v.realized_pnl_usdc)}">${v.realized_pnl_usdc==null?'—':'$'+n(v.realized_pnl_usdc)}</td><td>${v.loss_pool_after_usdc==null?'—':'$'+n(v.loss_pool_after_usdc)}</td><td>${v.near_touch_up_41?'UP ':''}${v.near_touch_down_41?'DN':''}</td><td>${v.error_code||'—'}</td></tr>`).join('');}catch(e){$('state').textContent='HATA · '+e}}
setInterval(tick,__P3_REFRESH_MS__);tick();</script></body></html>"""
