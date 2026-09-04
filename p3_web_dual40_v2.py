"""CSP-safe DUAL40 dashboard wrapper.

The original dashboard embedded both its controller script and button handlers
inline. When an HTTPS reverse proxy applies a second ``script-src 'self'`` policy,
the browser enforces both policies and no script is allowed: the upstream policy
allowed inline code only, while the proxy policy allowed same-origin files only.

This wrapper keeps the existing authenticated API/control implementation, removes
all inline JavaScript and serves one same-origin external asset. It also leaves a
visible error state when the asset or summary request fails instead of remaining at
``yükleniyor...`` forever.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from aiohttp import web

from p3_config import P3Settings
from p3_live_state import LiveState
import p3_web_dual40 as legacy


_JS_PATH = Path(__file__).with_name("p3_dual40_panel.js")
_PANEL_JS = _JS_PATH.read_text(encoding="utf-8")
_ASSET_PATH = "/assets/dual40-panel.js"


def externalized_html(template: str) -> str:
    """Return the legacy HTML shell with no inline script or event handlers."""
    html = str(template)
    if f'src="{_ASSET_PATH}' in html:
        return html

    replacements = {
        'onclick="logoutNow()"': 'id="logout-btn"',
        'onclick="liveAct(\'/api/live/probe\')"': 'id="probe-btn"',
        'onclick="confirmLive()"': 'id="arm-btn"',
        'onclick="liveAct(\'/api/live/disarm\')"': 'id="disarm-btn"',
    }
    for old, new in replacements.items():
        html = html.replace(old, new)

    html = html.replace(
        '<html lang="tr">',
        '<html lang="tr" data-refresh-ms="__P3_REFRESH_MS__">',
        1,
    )

    if "<script>" not in html or "</script>" not in html:
        raise RuntimeError("DUAL40 legacy HTML script block not found")
    before, remainder = html.split("<script>", 1)
    _inline_script, after = remainder.split("</script>", 1)
    return (
        before
        + f'<script src="{_ASSET_PATH}?v=2" defer></script>'
        + after
    )


def build_web_app(
    settings: P3Settings,
    *,
    live_state: LiveState | None,
    dual40_engine: Any | None,
) -> web.Application:
    # A same-origin external script works both with the app policy and a stricter
    # reverse-proxy policy. Inline style remains allowed because the page CSS is
    # intentionally embedded and contains no executable content.
    legacy._SECURITY_HEADERS["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'unsafe-inline'; script-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    )
    legacy._HTML = externalized_html(legacy._HTML)
    app = legacy.build_web_app(
        settings,
        live_state=live_state,
        dual40_engine=dual40_engine,
    )

    async def panel_js(_request: web.Request) -> web.Response:
        return web.Response(
            text=_PANEL_JS,
            content_type="application/javascript",
            charset="utf-8",
            headers={
                "Cache-Control": "no-store, max-age=0",
                "X-Content-Type-Options": "nosniff",
            },
        )

    app.router.add_get(_ASSET_PATH, panel_js)
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
