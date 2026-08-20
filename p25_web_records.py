"""P2.5 web server with a dedicated paper-trade records page.

The existing live forecast dashboard remains at ``/``.  Paper simulation records
are exposed read-only at ``/paper-trades`` with JSON and CSV APIs.  No write,
credential, signing or execution route exists.
"""
from __future__ import annotations

import asyncio

from aiohttp import web

from p25_paper_records import (
    PaperRecordFilters,
    export_paper_records_csv,
    query_paper_records,
)
from p25_paper_records_page import PAPER_RECORDS_HTML
from p25_web import _HTML


def _main_html_with_paper_link() -> str:
    marker = '<span class="pill" id="paperpill">PAPER</span>'
    link = (
        marker
        + '<a href="/paper-trades" style="border:1px solid #4f6f9d;'
        'background:#173457;color:#e7f1ff;border-radius:6px;padding:5px 9px;'
        'font-weight:800;font-size:11px;text-decoration:none">'
        'PAPER KAYITLARI →</a>'
    )
    return _HTML.replace(marker, link, 1)


async def run_web(engine, cfg, stop: asyncio.Event) -> None:  # noqa: ANN001
    app = web.Application()

    async def index(_request: web.Request) -> web.Response:
        return web.Response(
            text=_main_html_with_paper_link(),
            content_type="text/html",
        )

    async def paper_page(_request: web.Request) -> web.Response:
        return web.Response(text=PAPER_RECORDS_HTML, content_type="text/html")

    async def paper_alias(_request: web.Request) -> web.Response:
        raise web.HTTPFound("/paper-trades")

    async def state(_request: web.Request) -> web.Response:
        return web.json_response(engine.snapshot())

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
        analytics = getattr(engine.recorder, "paper_analytics", None)
        forecast = getattr(engine.recorder, "forecast_analytics", None)
        paper_payload = (
            analytics(getattr(cfg, "paper_recent_limit", 50))
            if callable(analytics)
            else {
                "enabled": False,
                "paper_only": True,
                "overall": {},
                "per_asset": {},
                "per_horizon": {},
                "per_combo": {},
                "skip_reasons": {},
            }
        )
        forecast_payload = (
            forecast(getattr(cfg, "min_markets_for_stats", 30))
            if callable(forecast)
            else {}
        )
        return web.json_response(
            {
                "paperOnly": True,
                "source": "sqlite",
                "paper_trading": paper_payload,
                "forecast_analytics": forecast_payload,
                "execution": False,
                "live_orders": 0,
            }
        )

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
        snapshot = engine.snapshot()
        return web.json_response(
            {
                "ok": True,
                "mode": "SHADOW",
                "phase": snapshot.get("phase"),
                "markets_active": snapshot.get("footer", {}).get(
                    "markets_active"
                ),
                "paper_trading_enabled": snapshot.get("safety", {}).get(
                    "paper_trading_enabled", False
                ),
                "paper_records_page": "/paper-trades",
                "paper_records_api": "/api/paper-trades",
                "paper_summary_api": "/api/paper-summary",
                "live_orders": snapshot.get("safety", {}).get(
                    "live_orders", 0
                ),
                "execution_enabled": snapshot.get("safety", {}).get(
                    "execution_enabled", False
                ),
            }
        )

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
