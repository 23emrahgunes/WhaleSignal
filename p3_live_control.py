"""Localhost-only operator control plane for guarded P3 LIVE mode."""
from __future__ import annotations

import asyncio
import json
from html import escape

from aiohttp import web

from p3_config import P3Settings
from p3_live_preflight import run_live_preflight
from p3_live_state import LiveState


def _authorized(request: web.Request, state: LiveState) -> bool:
    supplied = request.headers.get("X-P3-Control-Token", "")
    return bool(supplied) and supplied == state.control_token


def _html(settings: P3Settings, state: LiveState) -> str:
    token = json.dumps(state.control_token)
    host = escape(settings.live_control_host)
    port = int(settings.live_control_port)
    return f"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>P3 Canlı Kontrol</title>
<style>
body{{margin:0;background:#07101b;color:#eef5ff;font:14px Inter,Arial,sans-serif}}.wrap{{max-width:900px;margin:40px auto;padding:18px}}.box{{background:#101c2c;border:1px solid #29415f;border-radius:12px;padding:16px;margin-bottom:12px}}h1{{color:#65a9ff}}button{{border:0;border-radius:9px;padding:12px 16px;font-weight:800;margin:5px;cursor:pointer}}.probe{{background:#315c8f;color:#fff}}.live{{background:#b91c1c;color:#fff}}.dry{{background:#177f62;color:#fff}}pre{{white-space:pre-wrap;background:#07101b;padding:12px;border-radius:8px;max-height:420px;overflow:auto}}.warn{{color:#f1bd58}}.good{{color:#20d095}}.bad{{color:#f06b72}}
</style></head><body><div class="wrap"><h1>P3 — Yerel Canlı Kontrol</h1>
<div class="box"><b>Bu sayfa yalnız {host}:{port} üzerinde dinler.</b><p class="warn">8093 ana panelde emir butonu yoktur. Süreç her restart/deploy sonrası otomatik DRY başlar.</p><div id="status">Yükleniyor…</div></div>
<div class="box"><button class="probe" onclick="act('/api/probe')">BAĞLANTI / KİMLİK TESTİ (EMİR YOK)</button><button class="live" onclick="confirmLive()">CANLIYA GEÇ</button><button class="dry" onclick="act('/api/disarm')">DRY'A DÖN</button><pre id="out"></pre></div>
</div><script>
const TOKEN={token};
async function getStatus(){{let r=await fetch('/api/status',{{cache:'no-store'}});let j=await r.json();document.getElementById('status').innerHTML='<b>Mod: '+j.state.mode+'</b> · özellik='+(j.state.live_feature_enabled?'AÇIK':'KAPALI')+' · otomatik emir='+(j.state.auto_execute_enabled?'AÇIK':'KAPALI');}}
async function act(path){{let r=await fetch(path,{{method:'POST',headers:{{'X-P3-Control-Token':TOKEN}}}});let j=await r.json();document.getElementById('out').textContent=JSON.stringify(j,null,2);await getStatus();}}
function confirmLive(){{if(confirm('CANLI moda geçmek istiyor musun? Preflight geçmezse sistem DRY kalır.')) act('/api/arm');}}
setInterval(getStatus,2000);getStatus();
</script></body></html>"""


async def run_live_control(settings: P3Settings, state: LiveState, stop: asyncio.Event) -> None:
    if settings.live_control_host not in {"127.0.0.1", "::1", "localhost"}:
        raise RuntimeError("LIVE control server must be loopback-only")

    app = web.Application(client_max_size=16 * 1024)

    async def index(_request: web.Request) -> web.Response:
        return web.Response(text=_html(settings, state), content_type="text/html")

    async def status(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "state": state.public_dict()})

    async def probe(request: web.Request) -> web.Response:
        if not _authorized(request, state):
            raise web.HTTPForbidden(text="control token invalid")
        result = await asyncio.to_thread(run_live_preflight, settings, for_arming=False)
        state.remember_preflight(result)
        return web.json_response(result)

    async def arm(request: web.Request) -> web.Response:
        if not _authorized(request, state):
            raise web.HTTPForbidden(text="control token invalid")
        result = await asyncio.to_thread(run_live_preflight, settings, for_arming=True)
        state.remember_preflight(result)
        if not result.get("ok"):
            return web.json_response(
                {"ok": False, "armed": False, "preflight": result, "state": state.public_dict()},
                status=409,
            )
        snap = state.arm(result)
        return web.json_response({"ok": True, "armed": True, "state": state.public_dict(), "armed_at_ms": snap.armed_at_ms})

    async def disarm(request: web.Request) -> web.Response:
        if not _authorized(request, state):
            raise web.HTTPForbidden(text="control token invalid")
        state.disarm()
        return web.json_response({"ok": True, "state": state.public_dict()})

    app.add_routes(
        [
            web.get("/", index),
            web.get("/api/status", status),
            web.post("/api/probe", probe),
            web.post("/api/arm", arm),
            web.post("/api/disarm", disarm),
        ]
    )
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, settings.live_control_host, settings.live_control_port)
    await site.start()
    try:
        await stop.wait()
    finally:
        state.disarm("control_shutdown")
        await runner.cleanup()
