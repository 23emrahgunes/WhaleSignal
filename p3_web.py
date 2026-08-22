"""Read-only dashboard/API for the P3 structural arbitrage lab."""
from __future__ import annotations

import asyncio
import json
import math
import statistics
import time

from aiohttp import web

from p3_config import P3Settings
from p3_dry_run import build_dry_summary
from p3_schema import connect_p3, ensure_p3_schema, integrity_check


LATEST_SCAN_META_KEY = "latest_scan_stats_json"


def _latest_scan(conn) -> dict:  # noqa: ANN001
    row = conn.execute("SELECT value FROM p3_meta WHERE key=?", (LATEST_SCAN_META_KEY,)).fetchone()
    if row is None:
        return {}
    try:
        payload = json.loads(str(row["value"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _nearest_rank(values: list[int], percentile: float):  # noqa: ANN201
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil(float(percentile) * len(ordered))))
    return ordered[rank - 1]


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
            SELECT delay_ms,COUNT(*) AS n,SUM(both_fill) AS both_fill,
                   SUM(CASE WHEN outcome LIKE 'ONE_LEG%' THEN 1 ELSE 0 END) AS one_leg,
                   AVG(CASE WHEN cycle_net_pnl_usdc IS NOT NULL THEN cycle_net_pnl_usdc END) AS avg_pnl
            FROM p3_replays GROUP BY delay_ms ORDER BY delay_ms
            """
        ).fetchall()
        window_rows = conn.execute(
            "SELECT id,opened_ts_ms,closed_ts_ms,last_seen_ts_ms,status FROM p3_windows ORDER BY id"
        ).fetchall()
        closed_lifetimes = [
            int(row["closed_ts_ms"]) - int(row["opened_ts_ms"])
            for row in window_rows if row["closed_ts_ms"] is not None
        ]
        recent = [dict(row) for row in conn.execute(
            """
            SELECT id,strategy,condition_id,combo_key,detected_ts_ms,quantity_shares,
                   capital_usdc,net_profit_usdc,net_roi,source_skew_ms,max_book_age_ms,
                   up_vwap,down_vwap,up_fee_usdc,down_fee_usdc
            FROM p3_opportunities ORDER BY detected_ts_ms DESC,id DESC LIMIT 100
            """
        ).fetchall()]
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
                "pair_completion_rate": float(row["both_fill"] or 0) / int(row["n"]) if int(row["n"]) else 0.0,
                "one_leg": int(row["one_leg"] or 0),
                "avg_cycle_pnl_usdc": float(row["avg_pnl"]) if row["avg_pnl"] is not None else None,
            }
            for row in replay_rows
        }
        lifetime = {
            "closed_windows": len(closed_lifetimes),
            "open_windows": sum(1 for row in window_rows if row["status"] == "OPEN"),
            "median_ms": statistics.median(closed_lifetimes) if closed_lifetimes else None,
            "p90_ms": _nearest_rank(closed_lifetimes, 0.90),
            "max_ms": max(closed_lifetimes) if closed_lifetimes else None,
        }
        scanner = _latest_scan(conn)
        dry = build_dry_summary(conn, settings)
        current_reasons: list[str] = []
        transport = scanner.get("book_transport") or {}
        if scanner:
            if int(scanner.get("conditions") or 0) == 0:
                current_reasons.append("NO_ACTIVE_CONDITIONS")
            if int(scanner.get("valid_pairs") or 0) != int(scanner.get("conditions") or 0):
                current_reasons.append("NOT_ALL_BOOK_PAIRS_VALID")
            if int(scanner.get("missing_fee") or 0) > 0:
                current_reasons.append("FEE_LINEAGE_INCOMPLETE")
            if not bool(transport.get("connected")):
                current_reasons.append("BOOK_TRANSPORT_NOT_LIVE")
        if current_reasons:
            dry["readiness"]["reasons"].extend(current_reasons)
            dry["readiness"]["status"] = "NOT_READY"
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
            "scanner": scanner,
            "dry_run": dry,
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
        return web.json_response({
            "ok": True, "mode": "SHADOW_PAPER_ONLY", "execution_enabled": False,
            "private_key_loaded": False, "signing_enabled": False, "order_submission_enabled": False,
        })

    async def summary(_request: web.Request) -> web.Response:
        return web.json_response(build_summary(settings))

    async def opportunities(request: web.Request) -> web.Response:
        limit = max(1, min(500, int(request.query.get("limit", "100"))))
        payload = build_summary(settings)
        return web.json_response({"paperOnly": True, "rows": payload["recent"][:limit]})

    app.add_routes([web.get("/", index), web.get("/health", health), web.get("/api/summary", summary), web.get("/api/opportunities", opportunities)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.web_host, settings.web_port)
    await site.start()
    try:
        await stop.wait()
    finally:
        await runner.cleanup()


_HTML = r"""<!doctype html><html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>P3 Arbitrage Lab</title><style>
:root{--bg:#07101b;--panel:#101c2c;--line:#233650;--text:#eef5ff;--mut:#8ea5c3;--green:#20d095;--red:#f06b72;--blue:#65a9ff;--amber:#f1bd58}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:13px Inter,Arial,sans-serif}header{padding:14px 18px;border-bottom:1px solid var(--line);background:#0a1523;display:flex;gap:10px;align-items:center}h1{font-size:18px;margin:0;color:var(--blue)}.pill{padding:5px 8px;border-radius:6px;background:#17375d;font-weight:800}.wrap{max-width:1700px;margin:auto;padding:14px}.notice{border:1px solid #5f4a19;background:#251d09;padding:10px;border-radius:8px;color:#ffe1a0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:8px;margin-top:12px}.metric,.box{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:10px}.metric b{display:block;font-size:20px}.metric span{color:var(--mut)}.boxes{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}.box h2{font-size:14px;margin:0 0 8px}table{width:100%;border-collapse:collapse}th,td{padding:7px;border-bottom:1px solid #1e3048;text-align:right}th:first-child,td:first-child{text-align:left}th{color:var(--mut)}.pos{color:var(--green)}.neg{color:var(--red)}.warn{color:var(--amber)}.mono{font-family:ui-monospace,Consolas,monospace;font-size:11px}@media(max-width:900px){.boxes{grid-template-columns:1fr}}</style></head><body>
<header><h1>P3 Arbitrage Lab</h1><span class="pill">STRUCTURAL</span><span class="pill">DRY / SHADOW ONLY</span><span id="state" style="color:var(--mut)">yükleniyor…</span></header><div class="wrap"><div class="notice"><b>Window bazlı DRY araştırma.</b> Observation sayısı trade sayısı değildir. Kümülatif PnL yalnız her bağımsız window'un ilk entry replay'inden hesaplanır; gerçek emir yoktur.</div><div class="grid" id="metrics"></div><div class="box" style="margin-top:12px"><h2>DRY Bankroll / Readiness</h2><div class="grid" id="dry"></div><div id="reasons" class="mono warn" style="margin-top:10px"></div></div><div class="box" style="margin-top:12px"><h2>Scanner Funnel / Book Transport</h2><div class="grid" id="scanner"></div></div><div class="boxes"><div class="box"><h2>Stratejiler</h2><table><thead><tr><th>Strateji</th><th>Observation</th><th>Peak PnL</th><th>Avg ROI</th></tr></thead><tbody id="strategies"></tbody></table></div><div class="box"><h2>İki-Bacak Replay — observation-level</h2><table><thead><tr><th>Delay</th><th>N</th><th>Pair Fill</th><th>One Leg</th><th>Avg PnL</th></tr></thead><tbody id="replays"></tbody></table></div></div><div class="box" style="margin-top:12px"><h2>Bağımsız DRY Attempts</h2><div style="overflow:auto;max-height:360px"><table><thead><tr><th>Window</th><th>Combo</th><th>Status</th><th>Capital</th><th>Theoretical</th><th>Replay</th><th>Cycle PnL</th><th>Bankroll</th></tr></thead><tbody id="attempts"></tbody></table></div></div><div class="box" style="margin-top:12px"><h2>Son Fırsat Gözlemleri</h2><div style="overflow:auto;max-height:420px"><table><thead><tr><th>Combo</th><th>q</th><th>Capital</th><th>Net PnL</th><th>ROI</th><th>Last-change Skew</th></tr></thead><tbody id="recent"></tbody></table></div></div></div><script>
const $=id=>document.getElementById(id),n=(v,d=4)=>v==null?'—':Number(v).toFixed(d),pc=v=>v==null?'—':(Number(v)*100).toFixed(2)+'%',m=(v,l,k='')=>`<div class="metric"><b class="${k}">${v??0}</b><span>${l}</span></div>`;async function tick(){try{const d=await(await fetch('/api/summary',{cache:'no-store'})).json(),s=d.scanner||{},t=s.book_transport||{},x=d.dry_run||{},rd=x.readiness||{};$('state').textContent='OK · '+new Date().toLocaleTimeString();$('metrics').innerHTML=m(d.opportunities,'Observation')+m(d.windows,'Independent Window')+m(d.replays,'Replay')+m(n(d.lifetime?.median_ms,0)+' ms','Median lifetime')+m(n(d.lifetime?.p90_ms,0)+' ms','P90 lifetime');$('dry').innerHTML=m('$'+n(x.cumulative_pnl_usdc),'Cumulative DRY PnL',x.cumulative_pnl_usdc>=0?'pos':'neg')+m('$'+n(x.bankroll_usdc),'DRY Bankroll')+m(x.attempts_executed??0,'Independent Attempts')+m(pc(x.pair_completion_rate),'Pair Fill')+m(pc(x.pair_completion_wilson_lower_95),'Wilson Lower 95%')+m(pc(x.one_leg_rate),'One-leg Rate',x.one_leg_rate>0.03?'warn':'')+m('$'+n(x.max_drawdown_usdc),'Max Drawdown')+m(rd.status||'NOT_READY','Readiness',rd.status==='DRY_VALIDATED'?'pos':'warn')+m((x.latency_ms??0)+' ms','DRY latency')+m('$'+n(x.max_capital_per_cycle_usdc,2),'Cycle Cap');$('reasons').textContent=(rd.reasons||[]).join(' | ')||'Readiness gates passed.';$('scanner').innerHTML=m(s.conditions??0,'Aktif condition')+m(s.valid_pairs??0,'Geçerli book pair',s.valid_pairs?'pos':'warn')+m(s.missing_book??0,'Missing book',s.missing_book?'neg':'')+m(s.transport_stale??0,'Transport stale',s.transport_stale?'neg':'')+m(s.session_incomplete??0,'Session incomplete',s.session_incomplete?'warn':'')+m(s.missing_fee??0,'Missing fee',s.missing_fee?'neg':'')+m(t.connected?'LIVE':'DOWN','Book socket',t.connected?'pos':'neg')+m(t.subscribed_tokens??0,'Subscribed token');$('strategies').innerHTML=Object.entries(d.strategies||{}).map(([k,v])=>`<tr><td class="mono">${k}</td><td>${v.opportunities}</td><td class="pos">$${n(v.peak_profit_usdc)}</td><td>${pc(v.avg_roi)}</td></tr>`).join('');$('replays').innerHTML=Object.entries(d.replay_by_delay||{}).map(([k,v])=>`<tr><td>${k}ms</td><td>${v.n}</td><td>${pc(v.pair_completion_rate)}</td><td>${v.one_leg}</td><td class="${(v.avg_cycle_pnl_usdc||0)>=0?'pos':'neg'}">$${n(v.avg_cycle_pnl_usdc)}</td></tr>`).join('');$('attempts').innerHTML=(x.recent_attempts||[]).map(v=>`<tr><td>${v.window_id}</td><td>${v.combo_key}</td><td>${v.dry_status}</td><td>$${n(v.capital_usdc)}</td><td>$${n(v.theoretical_net_profit_usdc)}</td><td>${v.replay_outcome||'—'}</td><td class="${(v.cycle_net_pnl_usdc||0)>=0?'pos':'neg'}">$${n(v.cycle_net_pnl_usdc)}</td><td>$${n(v.bankroll_after_usdc)}</td></tr>`).join('');$('recent').innerHTML=(d.recent||[]).map(v=>`<tr><td>${v.combo_key}</td><td>${n(v.quantity_shares,2)}</td><td>$${n(v.capital_usdc)}</td><td class="pos">$${n(v.net_profit_usdc)}</td><td>${pc(v.net_roi)}</td><td>${v.source_skew_ms}ms</td></tr>`).join('')}catch(e){$('state').textContent='HATA '+e}}setInterval(tick,3000);tick();</script></body></html>"""
