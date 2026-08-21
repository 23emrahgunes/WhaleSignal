"""P2.5 web server with a dedicated paper-trade records page.

The existing live forecast dashboard remains at ``/``. Paper simulation records
are exposed read-only at ``/paper-trades`` with JSON and CSV APIs. No write,
credential, signing or execution route exists.
"""
from __future__ import annotations

import asyncio
import os
import time

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
    # Avoid overlapping expensive /api/state requests on a growing SQLite DB.
    return html.replace(
        "setInterval(tick,1500);tick();",
        "setInterval(tick,3000);tick();",
        1,
    )


def _paper_records_html() -> str:
    """Return the records page defaulting to actual entries only.

    Diagnostic SKIPPED attempts remain in SQLite and aggregate skip counters, but
    the user-facing market table shows only OPEN/SETTLED paper positions.
    """
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
    return html


def _health_payload(engine, cfg) -> dict:  # noqa: ANN001
    """Build a constant-time liveness payload without engine.snapshot/SQLite."""
    latest = getattr(engine, "latest", {}) or {}
    markets_active = sum(
        1
        for card in latest.values()
        if isinstance(card, dict) and card.get("active")
    )
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
        "live_orders": 0,
        "execution_enabled": False,
    }


async def run_web(engine, cfg, stop: asyncio.Event) -> None:  # noqa: ANN001
    app = web.Application()

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
                payload = engine.snapshot()
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
            "execution": False,
            "live_orders": 0,
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

    app.add_routes(
        [
            web.get("/", index),
            web.get("/paper", paper_alias),
            web.get("/paper-trades", paper_page),
            web.get("/api/state", state),
            web.get("/api/paper-trades", paper_records),
            web.get("/api/paper-summary", paper_summary),
            web.get("/api/paper-trades.csv", paper_csv),
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
