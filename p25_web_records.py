"""P2.5 web server with paper records and guarded XRP 5m LIVE controls.

Read APIs remain public as before. The two LIVE mutation routes are deliberately
password-gated, same-origin/XHR-gated and rate-limited. The operator password is
read from P25_LIVE_CONTROL_PASSWORD or the existing P3_WEB_PASSWORD; it is never
returned by an API or embedded into HTML.
"""
from __future__ import annotations

import asyncio
import hmac
import os
import time
from urllib.parse import urlsplit

from aiohttp import web

from p25_paper_records import (
    PaperRecordFilters,
    export_paper_records_csv,
    query_paper_records,
)
from p25_paper_records_page import PAPER_RECORDS_HTML
from p25_web import _HTML


def _env_seconds(name: str, default: float, minimum: float) -> float:
    raw = os.getenv(name)
    try:
        value = float(raw) if raw is not None else float(default)
    except ValueError:
        value = float(default)
    return max(float(minimum), value)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _control_password() -> str:
    return str(
        os.getenv("P25_LIVE_CONTROL_PASSWORD")
        or os.getenv("P3_WEB_PASSWORD")
        or ""
    )


def _main_html_with_paper_link() -> str:
    marker = '<span class="pill" id="paperpill">PAPER</span>'
    link = (
        marker
        + '<a href="/paper-trades" style="border:1px solid #4f6f9d;'
        'background:#173457;color:#e7f1ff;border-radius:6px;padding:5px 9px;'
        'font-weight:800;font-size:11px;text-decoration:none">'
        'PAPER KAYITLARI →</a>'
    )
    html = _HTML.replace(marker, link, 1)
    return html.replace(
        "setInterval(tick,1500);tick();",
        "setInterval(tick,3000);tick();",
        1,
    )


_PAPER_LIVE_JS = r"""
<script>
let xrpLivePaperState={armed:false,arm_consumed:false,max_stake_usdc:1.10,max_price_drift_pct:.10};
function xrpLivePaperRender(s){
 xrpLivePaperState=s||xrpLivePaperState;
 const b=document.getElementById('xrpLivePaperBtn');
 const m=document.getElementById('xrpLivePaperMeta');
 if(!b||!m)return;
 const armed=!!s.armed, consumed=!!s.arm_consumed;
 const cap=Number(s.max_stake_usdc==null?1.10:s.max_stake_usdc).toFixed(2);
 const drift=(Number(s.max_price_drift_pct==null?.10:s.max_price_drift_pct)*100).toFixed(0);
 if(armed&&!consumed){b.textContent='🔴 XRP 5m CANLI · DURDUR';b.style.background='#6f2027';b.style.borderColor='#a13b43';}
 else if(consumed){b.textContent='XRP 5m YENİDEN CANLIYA GEÇ';b.style.background='#5b450d';b.style.borderColor='#8c6c1d';}
 else{b.textContent='🟢 XRP 5m CANLIYA GEÇ';b.style.background='#12543f';b.style.borderColor='#23795e';}
 m.textContent=`max $${cap} · fiyat sapması ≤ %${drift} · ${s.last_reason||'IDLE'}`;
}
async function xrpLivePaperPoll(){
 try{const r=await fetch('/api/xrp5m-live/status',{cache:'no-store'});if(r.ok)xrpLivePaperRender(await r.json());}catch(e){}
}
async function xrpLivePaperToggle(){
 const s=xrpLivePaperState||{};
 const action=(s.armed&&!s.arm_consumed)?'disarm':'arm';
 if(action==='arm'){
   const cap=Number(s.max_stake_usdc==null?1.10:s.max_stake_usdc).toFixed(2);
   const drift=(Number(s.max_price_drift_pct==null?.10:s.max_price_drift_pct)*100).toFixed(0);
   if(!confirm(`XRP 5 dakika gerçek para pilotu ARM edilecek.\n\nMaksimum notional: $${cap}\nPaper girişe göre fiyat sapması: en fazla %${drift}\nBir ARM = en fazla bir network submit cycle.\n\nDevam edilsin mi?`))return;
 }else if(!confirm('XRP 5m LIVE ARM durdurulsun mu?'))return;
 const password=prompt('Operatör şifresi (P3_WEB_PASSWORD / P25_LIVE_CONTROL_PASSWORD):');
 if(!password)return;
 try{
   const r=await fetch('/api/xrp5m-live/'+action,{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'DirectionEngine-XRP5m'},body:JSON.stringify({password,confirm:action==='arm'?'XRP 5M CANLI':'XRP 5M DURDUR'})});
   const d=await r.json();
   if(d.status)xrpLivePaperRender(d.status);
   alert((d.ok?'BAŞARILI: ':'RED: ')+(d.reason||d.error||('HTTP '+r.status)));
 }catch(e){alert('XRP LIVE kontrol hatası: '+e);}
 await xrpLivePaperPoll();
}
setInterval(xrpLivePaperPoll,3000);xrpLivePaperPoll();
</script>
"""


def _paper_records_html() -> str:
    """Return records page defaulting to actual entries plus guarded LIVE control."""
    html = PAPER_RECORDS_HTML
    html = html.replace(
        '<div class="field"><label>Durum</label><select id="status">'
        '<option value="ALL">Tümü</option><option>OPEN</option>'
        '<option>SETTLED</option><option>SKIPPED</option></select></div>',
        '<div class="field"><label>Durum</label><select id="status">'
        '<option value="TRADED" selected>Giriş yapılanlar</option>'
        '<option>OPEN</option><option>SETTLED</option></select></div>',
        1,
    )
    html = html.replace(
        'placeholder="BTC:5m, slug, LOW_CONFIDENCE"',
        'placeholder="BTC:5m, slug, condition ID"',
        1,
    )
    html = html.replace(
        '<h2>Market Bazlı Paper Kayıtları</h2>',
        '<h2>Market Bazlı Paper İşlemler</h2>',
        1,
    )
    html = html.replace(
        'Henüz bu filtreye uyan paper kayıt yok. İlk kayıt 5m markette T-60, '
        '15m markette T-240, 1h markette T-600 checkpointinde oluşur.',
        'Henüz giriş yapılan paper işlem yok. İlk gerçek paper giriş 5m markette '
        'T-60, 15m markette T-240, 1h markette T-600 checkpointinde oluşur.',
        1,
    )
    html = html.replace(
        "for(const id of ['asset','horizon','status','side','result'])"
        "$(id).value='ALL';$('limit').value='50';",
        "for(const id of ['asset','horizon','side','result'])$(id).value='ALL';"
        "$('status').value='TRADED';$('limit').value='50';",
        1,
    )
    html = html.replace(
        '<span class="pill off">CANLI İŞLEM KAPALI</span>',
        '<button id="xrpLivePaperBtn" onclick="xrpLivePaperToggle()" '
        'style="border:1px solid #23795e;background:#12543f;color:#eafff7;'
        'border-radius:6px;padding:6px 10px;font-weight:900;cursor:pointer">'
        '🟢 XRP 5m CANLIYA GEÇ</button>'
        '<span id="xrpLivePaperMeta" style="color:#8fa5c6;font-size:11px">'
        'max $1.10 · fiyat sapması ≤ %10</span>',
        1,
    )
    return html.replace("</body>", _PAPER_LIVE_JS + "\n</body>", 1)


def _live_status(engine) -> dict:  # noqa: ANN001
    getter = getattr(engine, "xrp5m_live_pilot", None)
    pilot = getter() if callable(getter) else getattr(engine, "_xrp5m_live_pilot", None)
    if pilot is None:
        return {
            "feature_enabled": False,
            "armed": False,
            "scope": "XRP:5m",
            "max_stake_usdc": 1.10,
            "max_price_drift_pct": 0.10,
            "one_cycle_per_arm": True,
            "arm_consumed": False,
            "last_reason": "PILOT_NOT_ATTACHED",
        }
    return dict(pilot.status())


def _health_payload(engine, cfg) -> dict:  # noqa: ANN001
    latest = getattr(engine, "latest", {}) or {}
    markets_active = sum(
        1
        for card in latest.values()
        if isinstance(card, dict) and card.get("active")
    )
    live = _live_status(engine)
    armed_ready = bool(live.get("armed")) and not bool(live.get("arm_consumed"))
    return {
        "ok": True,
        "mode": "SHADOW",
        "phase": str(getattr(cfg, "phase", "P2.5")),
        "markets_active": markets_active,
        "paper_trading_enabled": bool(
            getattr(cfg, "paper_trading_enabled", False)
        ),
        "paper_records_page": "/paper-trades",
        "paper_records_api": "/api/paper-trades",
        "paper_records_default_status": "TRADED",
        "paper_summary_api": "/api/paper-summary",
        "xrp5m_live_status_api": "/api/xrp5m-live/status",
        "live_orders": int(live.get("network_cycles") or 0),
        "execution_enabled": armed_ready,
    }


async def run_web(engine, cfg, stop: asyncio.Event) -> None:  # noqa: ANN001
    app = web.Application(client_max_size=64 * 1024)

    state_cache_sec = _env_seconds("P25_WEB_STATE_CACHE_SEC", 5.0, 0.5)
    summary_cache_sec = _env_seconds(
        "P25_WEB_ANALYTICS_CACHE_SEC", 60.0, 1.0
    )
    include_forecast_analytics = _env_bool(
        "P25_WEB_SUMMARY_FORECAST_ANALYTICS", False
    )
    state_cache: dict[str, object] = {"value": None, "at": 0.0}
    summary_cache: dict[str, object] = {"value": None, "at": 0.0}
    state_lock = asyncio.Lock()
    summary_lock = asyncio.Lock()
    auth_failures: dict[str, list[float]] = {}

    async def cached_state() -> dict:
        now = time.monotonic()
        cached = state_cache["value"]
        if (
            isinstance(cached, dict)
            and now - float(state_cache["at"]) < state_cache_sec
        ):
            return cached
        async with state_lock:
            now = time.monotonic()
            cached = state_cache["value"]
            if (
                isinstance(cached, dict)
                and now - float(state_cache["at"]) < state_cache_sec
            ):
                return cached
            try:
                payload = await asyncio.to_thread(engine.snapshot)
            except Exception:
                if isinstance(cached, dict):
                    return cached
                raise
            state_cache["value"] = payload
            state_cache["at"] = now
            return payload

    def build_paper_summary() -> dict:
        engine_analytics = getattr(engine, "_paper_analytics_cached", None)
        recorder_analytics = getattr(engine.recorder, "paper_analytics", None)
        if callable(engine_analytics):
            paper_payload = engine_analytics()
        elif callable(recorder_analytics):
            paper_payload = recorder_analytics(
                getattr(cfg, "paper_recent_limit", 50)
            )
        else:
            paper_payload = {
                "enabled": False,
                "paper_only": True,
                "overall": {},
                "per_asset": {},
                "per_horizon": {},
                "per_combo": {},
                "skip_reasons": {},
            }

        forecast_payload: dict = {
            "status": "DEFERRED",
            "reason": "P25_WEB_SUMMARY_FORECAST_ANALYTICS=false",
        }
        forecast = getattr(engine.recorder, "forecast_analytics", None)
        if include_forecast_analytics and callable(forecast):
            forecast_payload = forecast(
                getattr(cfg, "min_markets_for_stats", 30)
            )

        return {
            "paperOnly": True,
            "source": "sqlite",
            "paper_trading": paper_payload,
            "forecast_analytics": forecast_payload,
            "execution": bool(_live_status(engine).get("armed")),
            "live_orders": int(_live_status(engine).get("network_cycles") or 0),
        }

    async def cached_paper_summary() -> dict:
        now = time.monotonic()
        cached = summary_cache["value"]
        if (
            isinstance(cached, dict)
            and now - float(summary_cache["at"]) < summary_cache_sec
        ):
            return cached
        async with summary_lock:
            now = time.monotonic()
            cached = summary_cache["value"]
            if (
                isinstance(cached, dict)
                and now - float(summary_cache["at"]) < summary_cache_sec
            ):
                return cached
            try:
                payload = build_paper_summary()
            except Exception:
                if isinstance(cached, dict):
                    return cached
                raise
            summary_cache["value"] = payload
            summary_cache["at"] = now
            return payload

    async def index(_request: web.Request) -> web.Response:
        return web.Response(
            text=_main_html_with_paper_link(),
            content_type="text/html",
        )

    async def paper_page(_request: web.Request) -> web.Response:
        return web.Response(text=_paper_records_html(), content_type="text/html")

    async def paper_alias(_request: web.Request) -> web.Response:
        raise web.HTTPFound("/paper-trades")

    async def state(_request: web.Request) -> web.Response:
        return web.json_response(await cached_state())

    async def paper_records(request: web.Request) -> web.Response:
        try:
            filters = PaperRecordFilters.from_mapping(request.query)
            payload = query_paper_records(engine.recorder, filters)
        except ValueError as exc:
            return web.json_response(
                {"error": "INVALID_FILTER", "message": str(exc)},
                status=400,
            )
        return web.json_response(payload)

    async def paper_summary(_request: web.Request) -> web.Response:
        return web.json_response(await cached_paper_summary())

    async def paper_csv(request: web.Request) -> web.Response:
        try:
            filters = PaperRecordFilters.from_mapping(request.query, export=True)
            content = export_paper_records_csv(engine.recorder, filters)
        except ValueError as exc:
            return web.json_response(
                {"error": "INVALID_FILTER", "message": str(exc)},
                status=400,
            )
        return web.Response(
            text=content,
            content_type="text/csv",
            headers={
                "Content-Disposition": (
                    'attachment; filename="direction-engine-paper-trades.csv"'
                )
            },
        )

    async def health(_request: web.Request) -> web.Response:
        return web.json_response(_health_payload(engine, cfg))

    async def live_status(_request: web.Request) -> web.Response:
        payload = _live_status(engine)
        payload["control_available"] = bool(_control_password())
        return web.json_response(payload)

    def _auth_blocked(remote: str) -> bool:
        now = time.monotonic()
        recent = [t for t in auth_failures.get(remote, []) if now - t < 600.0]
        auth_failures[remote] = recent
        return len(recent) >= 5

    def _auth_fail(remote: str) -> None:
        auth_failures.setdefault(remote, []).append(time.monotonic())

    async def _authorize_control(request: web.Request) -> tuple[dict | None, web.Response | None]:
        remote = str(request.remote or "unknown")
        if _auth_blocked(remote):
            return None, web.json_response(
                {"ok": False, "reason": "CONTROL_RATE_LIMIT"},
                status=429,
            )
        if request.headers.get("X-Requested-With") != "DirectionEngine-XRP5m":
            return None, web.json_response(
                {"ok": False, "reason": "CONTROL_XHR_REQUIRED"},
                status=403,
            )
        origin = request.headers.get("Origin")
        if origin:
            try:
                if urlsplit(origin).netloc != request.host:
                    return None, web.json_response(
                        {"ok": False, "reason": "CONTROL_ORIGIN_REJECTED"},
                        status=403,
                    )
            except ValueError:
                return None, web.json_response(
                    {"ok": False, "reason": "CONTROL_ORIGIN_REJECTED"},
                    status=403,
                )
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return None, web.json_response(
                {"ok": False, "reason": "INVALID_JSON"},
                status=400,
            )
        secret = _control_password()
        if not secret:
            return None, web.json_response(
                {"ok": False, "reason": "CONTROL_PASSWORD_NOT_CONFIGURED"},
                status=503,
            )
        supplied = str(payload.get("password") or "")
        if not hmac.compare_digest(secret, supplied):
            _auth_fail(remote)
            return None, web.json_response(
                {"ok": False, "reason": "CONTROL_AUTH_FAILED"},
                status=401,
            )
        auth_failures.pop(remote, None)
        return payload, None

    async def live_arm(request: web.Request) -> web.Response:
        payload, error = await _authorize_control(request)
        if error is not None:
            return error
        assert payload is not None
        if str(payload.get("confirm") or "").upper() != "XRP 5M CANLI":
            return web.json_response(
                {"ok": False, "reason": "ARM_CONFIRMATION_REQUIRED"},
                status=400,
            )
        getter = getattr(engine, "xrp5m_live_pilot", None)
        pilot = getter() if callable(getter) else None
        if pilot is None:
            return web.json_response(
                {"ok": False, "reason": "PILOT_NOT_ATTACHED"},
                status=503,
            )
        result = await asyncio.to_thread(pilot.arm)
        state_cache["at"] = 0.0
        return web.json_response(result, status=200 if result.get("ok") else 409)

    async def live_disarm(request: web.Request) -> web.Response:
        payload, error = await _authorize_control(request)
        if error is not None:
            return error
        assert payload is not None
        if str(payload.get("confirm") or "").upper() != "XRP 5M DURDUR":
            return web.json_response(
                {"ok": False, "reason": "DISARM_CONFIRMATION_REQUIRED"},
                status=400,
            )
        getter = getattr(engine, "xrp5m_live_pilot", None)
        pilot = getter() if callable(getter) else None
        if pilot is None:
            return web.json_response(
                {"ok": False, "reason": "PILOT_NOT_ATTACHED"},
                status=503,
            )
        result = pilot.disarm()
        state_cache["at"] = 0.0
        return web.json_response(result)

    app.add_routes(
        [
            web.get("/", index),
            web.get("/paper", paper_alias),
            web.get("/paper-trades", paper_page),
            web.get("/api/state", state),
            web.get("/api/paper-trades", paper_records),
            web.get("/api/paper-summary", paper_summary),
            web.get("/api/paper-trades.csv", paper_csv),
            web.get("/api/xrp5m-live/status", live_status),
            web.post("/api/xrp5m-live/arm", live_arm),
            web.post("/api/xrp5m-live/disarm", live_disarm),
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
