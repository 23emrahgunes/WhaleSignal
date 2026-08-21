"""Read-only dashboard/API for the P3 structural arbitrage lab."""
from __future__ import annotations

import asyncio
import json
import statistics
import time

from aiohttp import web

from p3_config import P3Settings
from p3_schema import connect_p3, ensure_p3_schema, integrity_check


def build_summary(settings: P3Settings) -> dict:
    conn = connect_p3(settings.p3_db_path)
    ensure_p3_schema(conn)
    try:
        strategy_rows = conn.execute(
            """
            SELECT strategy,COUNT(*) AS opportunities,
                   MAX(net_profit_usdc) AS peak_profit,
                   AVG(net_profit_usdc) AS avg_profit,
                   AVG(net_roi) AS avg_roi,
                   SUM(CASE WHEN net_profit_usdc>0 THEN 1 ELSE 0 END) AS positive
            FROM p3_opportunities GROUP BY strategy ORDER BY strategy
            """
        ).fetchall()
        replay_rows = conn.execute(
            """
            SELECT delay_ms,COUNT(*) AS n,
                   SUM(both_fill) AS both_fill,
                   SUM(CASE WHEN outcome LIKE 'ONE_LEG%' THEN 1 ELSE 0 END) AS one_leg,
                   AVG(CASE WHEN cycle_net_pnl_usdc IS NOT NULL THEN cycle_net_pnl_usdc END) AS avg_pnl
            FROM p3_replays GROUP BY delay_ms ORDER BY delay_ms
            """
        ).fetchall()
        window_rows = conn.execute(
            "SELECT opened_ts_ms,closed_ts_ms,status FROM p3_windows ORDER BY id"
        ).fetchall()
        closed_lifetimes = [
            int(row["closed_ts_ms"]) - int(row["opened_ts_ms"])
            for row in window_rows
            if row["closed_ts_ms"] is not None
        ]
        recent = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id,strategy,condition_id,combo_key,detected_ts_ms,quantity_shares,
                       net_profit_usdc,net_roi,source_skew_ms,max_book_age_ms,
                       up_vwap,down_vwap,up_fee_usdc,down_fee_usdc
                FROM p3_opportunities ORDER BY detected_ts_ms DESC,id DESC LIMIT 100
                """
            ).fetchall()
        ]
        strategies = {
            str(row["strategy"]): {
                "opportunities": int(row["opportunities"]),
                "positive": int(row["positive"] or 0),
                "peak_profit_usdc": float(row["peak_profit"] or 0.0),
                "avg_profit_usdc": float(row["avg_profit"] or 0.0),
                "avg_roi": float(row["avg_roi"] or 0.0),
            }
            for row in strategy_rows
        }
        replays = {
            str(int(row["delay_ms"])): {
                "n": int(row["n"]),
                "both_fill": int(row["both_fill"] or 0),
                "pair_completion_rate": (
                    float(row["both_fill"] or 0) / int(row["n"]) if int(row["n"]) else 0.0
                ),
                "one_leg": int(row["one_leg"] or 0),
                "avg_cycle_pnl_usdc": (
                    float(row["avg_pnl"]) if row["avg_pnl"] is not None else None
                ),
            }
            for row in replay_rows
        }
        lifetime = {
            "closed_windows": len(closed_lifetimes),
            "open_windows": sum(1 for row in window_rows if row["status"] == "OPEN"),
            "median_ms": statistics.median(closed_lifetimes) if closed_lifetimes else None,
            "p90_ms": (
                sorted(closed_lifetimes)[max(0, int(len(closed_lifetimes) * 0.9) - 1)]
                if closed_lifetimes else None
            ),
            "max_ms": max(closed_lifetimes) if closed_lifetimes else None,
        }
        return {
            "ok": True,
            "mode": "SHADOW_PAPER_ONLY",
            "execution_enabled": False,
            "private_key_loaded": False,
            "signing_enabled": False,
            "order_submission_enabled": False,
            "db_integrity": integrity_check(conn),
            "opportunities": int(conn.execute("SELECT COUNT(*) FROM p3_opportunities").fetchone()[0]),
            "windows": int(conn.execute("SELECT COUNT(*) FROM p3_windows").fetchone()[0]),
            "replays": int(conn.execute("SELECT COUNT(*) FROM p3_replays").fetchone()[0]),
            "strategies": strategies,
            "lifetime": lifetime,
            "replay_by_delay": replays,
            "recent": recent,
            "now_ms": int(time.time() * 1000),
        }
    finally:
        conn.close()


async def run_web(settings: P3Settings, stop: asyncio.Event) -> None:
    app = web.Application()

    async def index(_request: web.Request) -> web.Response:
        return web.Response(text=_HTML, content_type="text/html")

    async def health(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "mode": "SHADOW_PAPER_ONLY",
                "execution_enabled": False,
                "private_key_loaded": False,
                "signing_enabled": False,
                "order_submission_enabled": False,
            }
        )

    async def summary(_request: web.Request) -> web.Response:
        return web.json_response(build_summary(settings))

    async def opportunities(request: web.Request) -> web.Response:
        limit = max(1, min(500, int(request.query.get("limit", "100"))))
        payload = build_summary(settings)
        return web.json_response({"paperOnly": True, "rows": payload["recent"][:limit]})

    app.add_routes(
        [
            web.get("/", index),
            web.get("/health", health),
            web.get("/api/summary", summary),
            web.get("/api/opportunities", opportunities),
        ]
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.web_host, settings.web_port)
    await site.start()
    try:
        await stop.wait()
    finally:
        await runner.cleanup()


_HTML = r"""<!doctype html><html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>P3 Arbitrage Lab</title><style>
:root{--bg:#07101b;--panel:#101c2c;--line:#233650;--text:#eef5ff;--mut:#8ea5c3;--green:#20d095;--red:#f06b72;--blue:#65a9ff;--amber:#f1bd58}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:13px Inter,Arial,sans-serif}header{padding:14px 18px;border-bottom:1px solid var(--line);background:#0a1523;display:flex;gap:10px;align-items:center}h1{font-size:18px;margin:0;color:var(--blue)}.pill{padding:5px 8px;border-radius:6px;background:#17375d;font-weight:800}.wrap{max-width:1700px;margin:auto;padding:14px}.notice{border:1px solid #5f4a19;background:#251d09;padding:10px;border-radius:8px;color:#ffe1a0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:8px;margin-top:12px}.metric,.box{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:10px}.metric b{display:block;font-size:20px}.metric span{color:var(--mut)}.boxes{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}.box h2{font-size:14px;margin:0 0 8px}table{width:100%;border-collapse:collapse}th,td{padding:7px;border-bottom:1px solid #1e3048;text-align:right}th:first-child,td:first-child{text-align:left}th{color:var(--mut)}.pos{color:var(--green)}.neg{color:var(--red)}.mono{font-family:ui-monospace,Consolas,monospace;font-size:11px}@media(max-width:900px){.boxes{grid-template-columns:1fr}}</style></head><body>
<header><h1>P3 Arbitrage Lab</h1><span class="pill">STRUCTURAL</span><span class="pill">SHADOW ONLY</span><span id="state" style="color:var(--mut)">yükleniyor…</span></header><div class="wrap"><div class="notice"><b>Model-free complete-set araştırması.</b> Bu panel fırsat, yaşam süresi ve iki-bacak replay ölçer; private key, signing veya gerçek emir yoktur.</div><div class="grid" id="metrics"></div><div class="boxes"><div class="box"><h2>Stratejiler</h2><table><thead><tr><th>Strateji</th><th>Fırsat</th><th>Peak PnL</th><th>Avg ROI</th></tr></thead><tbody id="strategies"></tbody></table></div><div class="box"><h2>İki-Bacak Replay</h2><table><thead><tr><th>Delay</th><th>N</th><th>Pair Fill</th><th>One Leg</th><th>Avg PnL</th></tr></thead><tbody id="replays"></tbody></table></div></div><div class="box" style="margin-top:12px"><h2>Son Fırsatlar</h2><div style="overflow:auto;max-height:520px"><table><thead><tr><th>Combo</th><th>Strateji</th><th>q</th><th>Net PnL</th><th>ROI</th><th>Skew</th><th>Book Age</th></tr></thead><tbody id="recent"></tbody></table></div></div></div><script>
const $=id=>document.getElementById(id);const n=(v,d=4)=>v==null?'—':Number(v).toFixed(d);const pc=v=>v==null?'—':(Number(v)*100).toFixed(2)+'%';async function tick(){try{const r=await fetch('/api/summary',{cache:'no-store'});const d=await r.json();$('state').textContent='OK · '+new Date().toLocaleTimeString();$('metrics').innerHTML=[['opportunities','Fırsat'],['windows','Window'],['replays','Replay']].map(([k,l])=>`<div class="metric"><b>${d[k]??0}</b><span>${l}</span></div>`).join('')+`<div class="metric"><b>${n(d.lifetime?.median_ms,0)} ms</b><span>Median lifetime</span></div><div class="metric"><b>${n(d.lifetime?.p90_ms,0)} ms</b><span>P90 lifetime</span></div>`;$('strategies').innerHTML=Object.entries(d.strategies||{}).map(([k,v])=>`<tr><td class="mono">${k}</td><td>${v.opportunities}</td><td class="${v.peak_profit_usdc>=0?'pos':'neg'}">$${n(v.peak_profit_usdc)}</td><td>${pc(v.avg_roi)}</td></tr>`).join('');$('replays').innerHTML=Object.entries(d.replay_by_delay||{}).map(([k,v])=>`<tr><td>${k}ms</td><td>${v.n}</td><td>${pc(v.pair_completion_rate)}</td><td>${v.one_leg}</td><td>$${n(v.avg_cycle_pnl_usdc)}</td></tr>`).join('');$('recent').innerHTML=(d.recent||[]).map(v=>`<tr><td>${v.combo_key}</td><td class="mono">${v.strategy}</td><td>${n(v.quantity_shares,2)}</td><td class="${v.net_profit_usdc>=0?'pos':'neg'}">$${n(v.net_profit_usdc)}</td><td>${pc(v.net_roi)}</td><td>${v.source_skew_ms}ms</td><td>${v.max_book_age_ms}ms</td></tr>`).join('')}catch(e){$('state').textContent='HATA '+e}}setInterval(tick,3000);tick();</script></body></html>"""
