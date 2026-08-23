"""Read-only Turkish dashboard/API for P3 DRY research and guarded LIVE status.

The main dashboard never exposes mutating LIVE endpoints. Operator arm/disarm actions
live on the separate loopback-only control plane (default 127.0.0.1:8094).
"""
from __future__ import annotations

import asyncio
import json
import math
import statistics
import time
from typing import Any

from aiohttp import web

from p3_config import P3Settings
from p3_dry_run import build_dry_summary
from p3_live_state import LiveState, MODE_DRY
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


def _live_status(settings: P3Settings, live_state: LiveState | None) -> dict[str, Any]:
    if live_state is None:
        return {
            "mode": MODE_DRY,
            "live_feature_enabled": bool(settings.live_feature_enabled),
            "auto_execute_enabled": bool(settings.live_auto_execute_enabled),
            "armed_at_ms": None,
            "halted_at_ms": None,
            "reason": "no_live_state_provider",
            "preflight_ok": None,
            "preflight_checked_at_ms": None,
            "preflight_reasons": [],
            "control": f"{settings.live_control_host}:{settings.live_control_port}",
            "control_loopback_only": True,
        }
    payload = live_state.public_dict()
    payload["control"] = f"{settings.live_control_host}:{settings.live_control_port}"
    payload["control_loopback_only"] = True
    return payload


def build_summary(settings: P3Settings, live_state: LiveState | None = None) -> dict:
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
        live_cycles = [dict(row) for row in conn.execute(
            """
            SELECT id,session_id,window_id,strategy,condition_id,combo_key,entry_ts_ms,
                   quantity_shares,capital_usdc,status,up_order_id,down_order_id,
                   up_fill_verified,down_fill_verified,merge_tx_hash,unwind_side,
                   unwind_order_id,error_code,created_at_ms,updated_at_ms
            FROM p3_live_cycles ORDER BY id DESC LIMIT 30
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
        transport = scanner.get("book_transport") or {}
        current_reasons: list[str] = []
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

        live = _live_status(settings, live_state)
        executing = bool(live_state and live_state.can_auto_execute())
        mode = str(live.get("mode") or MODE_DRY)
        return {
            "ok": True,
            "mode": mode,
            "execution_enabled": executing,
            "private_key_loaded": False,
            "signing_enabled": executing,
            "order_submission_enabled": executing,
            "wallet_required": mode != MODE_DRY,
            "wallet_loaded": False,
            "wallet_note": (
                "DRY modunda cüzdan gerekmez. LIVE kimlik/bakiye ayrıntıları yalnız yerel kontrol panelinde doğrulanır."
            ),
            "live": live,
            "db_integrity": integrity_check(conn),
            "opportunities": int(conn.execute("SELECT COUNT(*) FROM p3_opportunities").fetchone()[0]),
            "windows": int(conn.execute("SELECT COUNT(*) FROM p3_windows").fetchone()[0]),
            "window_observations": int(conn.execute("SELECT COUNT(*) FROM p3_window_observations").fetchone()[0]),
            "replays": int(conn.execute("SELECT COUNT(*) FROM p3_replays").fetchone()[0]),
            "entry_replays": int(conn.execute("SELECT COUNT(*) FROM p3_entry_replays").fetchone()[0]),
            "live_cycles": live_cycles,
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


async def run_web(
    settings: P3Settings,
    stop: asyncio.Event,
    *,
    live_state: LiveState | None = None,
) -> None:
    app = web.Application()

    async def index(_request: web.Request) -> web.Response:
        return web.Response(text=_HTML, content_type="text/html")

    async def health(_request: web.Request) -> web.Response:
        live = _live_status(settings, live_state)
        executing = bool(live_state and live_state.can_auto_execute())
        return web.json_response({
            "ok": True,
            "mode": live.get("mode", MODE_DRY),
            "execution_enabled": executing,
            "private_key_loaded": False,
            "signing_enabled": executing,
            "order_submission_enabled": executing,
            "wallet_required": live.get("mode") != MODE_DRY,
            "wallet_loaded": False,
            "live_feature_enabled": bool(settings.live_feature_enabled),
        })

    async def summary(_request: web.Request) -> web.Response:
        return web.json_response(build_summary(settings, live_state))

    async def opportunities(request: web.Request) -> web.Response:
        limit = max(1, min(500, int(request.query.get("limit", "100"))))
        payload = build_summary(settings, live_state)
        return web.json_response({"readOnly": True, "rows": payload["recent"][:limit]})

    app.add_routes([
        web.get("/", index), web.get("/health", health),
        web.get("/api/summary", summary), web.get("/api/opportunities", opportunities),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.web_host, settings.web_port)
    await site.start()
    try:
        await stop.wait()
    finally:
        await runner.cleanup()


_HTML = r"""<!doctype html><html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>P3 Arbitraj Laboratuvarı</title>
<style>:root{--bg:#07101b;--panel:#101c2c;--line:#233650;--text:#eef5ff;--mut:#8ea5c3;--green:#20d095;--red:#f06b72;--blue:#65a9ff;--amber:#f1bd58}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:13px Inter,Arial,sans-serif}header{padding:14px 18px;border-bottom:1px solid var(--line);background:#0a1523;display:flex;gap:10px;align-items:center;flex-wrap:wrap}h1{font-size:18px;margin:0;color:var(--blue)}.pill{padding:5px 8px;border-radius:6px;background:#17375d;font-weight:800}.ok{color:var(--green)}.bad{color:var(--red)}.warn{color:var(--amber)}.wrap{max-width:1700px;margin:auto;padding:14px}.notice,.box,.metric{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:10px}.notice{color:#ffe1a0;margin-bottom:10px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:8px;margin-top:10px}.metric b{display:block;font-size:19px}.metric span,.mut{color:var(--mut)}.box{margin-top:12px}.box h2{font-size:14px;margin:0 0 8px}table{width:100%;border-collapse:collapse}th,td{padding:7px;border-bottom:1px solid #1e3048;text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}th{color:var(--mut)}.scroll{overflow:auto;max-height:420px}.mono{font-family:ui-monospace,Consolas,monospace;font-size:11px}</style></head><body>
<header><h1>P3 Arbitraj Laboratuvarı</h1><span class="pill">YAPISAL ARBİTRAJ</span><span id="modepill" class="pill">DRY / SHADOW</span><span id="state" class="mut">yükleniyor…</span></header><div class="wrap">
<div id="notice" class="notice"><b>Şu an DRY/SHADOW modundayız.</b> DRY modunda gerçek emir gönderilmez ve cüzdan gerekmez. LIVE kontrolü ana panelde yoktur; yalnız VPS içindeki yerel kontrol panelinden yapılır.</div>
<div class="box"><h2>Güvenlik ve Çalışma Modu</h2><div class="grid" id="safety"></div></div>
<div class="box"><h2>STRICT DRY — Ana Sonuçlar</h2><div class="grid" id="dry"></div><div id="reasons" class="mono warn"></div></div>
<div class="box"><h2>Canlı İşlem Günlüğü (salt okunur)</h2><div class="scroll"><table><thead><tr><th>ID</th><th>Market</th><th>Durum</th><th>Miktar</th><th>Sermaye</th><th>UP</th><th>DOWN</th><th>Merge TX</th><th>Hata</th></tr></thead><tbody id="livecycles"></tbody></table></div></div>
<div class="box"><h2>Son Bağımsız STRICT DRY İşlemler</h2><div class="scroll"><table><thead><tr><th>Window</th><th>Market</th><th>Durum</th><th>Teorik</th><th>Replay</th><th>İşlem K/Z</th><th>Bakiye</th></tr></thead><tbody id="attempts"></tbody></table></div></div>
<div class="box"><h2>Eski / Gösterge Niteliğindeki Sonuçlar</h2><div class="grid" id="legacy"></div></div>
<div class="box"><h2>Tarayıcı ve Emir Defteri Sağlığı</h2><div class="grid" id="scanner"></div></div>
</div><script>
const $=x=>document.getElementById(x),n=(v,d=4)=>v==null?'—':Number(v).toFixed(d),pc=v=>v==null?'—':(Number(v)*100).toFixed(2)+'%',m=(v,l,c='')=>`<div class="metric"><b class="${c}">${v??'—'}</b><span>${l}</span></div>`,cls=v=>Number(v||0)>=0?'ok':'bad';
const durum={DRY_EXECUTED:'DRY İŞLEM YAPILDI',SKIPPED_CONFIRMATION:'ONAY SÜRESİNİ GEÇEMEDİ',CONFIRMATION_GAP:'ONAY ZİNCİRİ KOPTU',LEGACY_CONFIRMATION_UNPROVEN:'ESKİ / STRICT KANITSIZ',SKIPPED_EDGE_GATE:'KÂR EŞİĞİ ALTINDA',SKIPPED_CAPITAL_LIMIT:'SERMAYE LİMİTİ',PENDING_ENTRY_REPLAY:'REPLAY BEKLENİYOR',PENDING_CONFIRMATION:'ONAY BEKLENİYOR'};
const replayDurum={BOTH_FILLED:'İKİ BACAK DOLDU',ONE_LEG_FILLED_UNWIND:'TEK BACAK DOLDU / KAPATILDI',ONE_LEG_UNWIND_FAILED:'TEK BACAK / KAPATMA BAŞARISIZ',NONE_FILLED:'HİÇBİR BACAK DOLMADI'};
function trReason(r){if(!r)return'';if(r.startsWith('INSUFFICIENT_INDEPENDENT_WINDOWS:'))return'Yeterli bağımsız STRICT işlem yok: '+r.split(':')[1];if(r.startsWith('PAIR_COMPLETION_TOO_LOW:'))return'İki bacak dolum oranı düşük: '+r.split(':')[1];if(r.startsWith('PAIR_WILSON_LOWER_TOO_LOW:'))return'Wilson güven alt sınırı düşük: '+r.split(':')[1];if(r.startsWith('ONE_LEG_RATE_TOO_HIGH:'))return'Tek bacak oranı yüksek: '+r.split(':')[1];if(r.startsWith('CUMULATIVE_PNL_NOT_POSITIVE:'))return'Toplam K/Z pozitif değil';if(r.startsWith('AVERAGE_PNL_NOT_POSITIVE:'))return'Ortalama K/Z pozitif değil';return r}
async function tick(){try{const d=await(await fetch('/api/summary',{cache:'no-store'})).json(),x=d.dry_run||{},rd=x.readiness||{},lg=x.legacy_indicative||{},s=d.scanner||{},t=s.book_transport||{},lv=d.live||{};$('state').textContent='OK · '+new Date().toLocaleTimeString();$('modepill').textContent=lv.mode==='LIVE_ARMED'?'LIVE ARMED':lv.mode==='LIVE_HALTED'?'LIVE HALTED':'DRY / SHADOW';$('modepill').className='pill '+(lv.mode==='LIVE_ARMED'?'bad':lv.mode==='LIVE_HALTED'?'warn':'ok');
$('notice').innerHTML=lv.mode==='LIVE_ARMED'?'<b>CANLI MOD ARM EDİLDİ.</b> Gerçek emir yolu yalnız yerel 8094 kontrolü ve preflight kapıları üzerinden açıktır.':'<b>Şu an DRY/SHADOW modundayız.</b> DRY modunda gerçek emir gönderilmez ve cüzdan gerekmez. LIVE kontrolü yalnız '+(lv.control||'127.0.0.1:8094')+' yerel panelindedir.';
$('safety').innerHTML=m(lv.mode||'DRY','Çalışma modu',lv.mode==='LIVE_ARMED'?'bad':'ok')+m(lv.live_feature_enabled?'AÇIK':'KAPALI','LIVE özellik',lv.live_feature_enabled?'warn':'ok')+m(lv.auto_execute_enabled?'AÇIK':'KAPALI','Otomatik emir',lv.auto_execute_enabled?'warn':'ok')+m(lv.preflight_ok===true?'GEÇTİ':lv.preflight_ok===false?'KALDI':'YAPILMADI','LIVE ön kontrol',lv.preflight_ok===true?'ok':'warn')+m(d.order_submission_enabled?'AÇIK':'KAPALI','Emir gönderimi',d.order_submission_enabled?'bad':'ok')+m(d.signing_enabled?'AÇIK':'KAPALI','İmza',d.signing_enabled?'bad':'ok')+m(d.wallet_required?'GEREKLİ':'GEREKLİ DEĞİL','Cüzdan (DRY)',d.wallet_required?'warn':'ok')+m(d.db_integrity==='ok'?'SAĞLAM':d.db_integrity,'Veritabanı',d.db_integrity==='ok'?'ok':'bad');
$('dry').innerHTML=m('$'+n(x.cumulative_pnl_usdc),'STRICT toplam K/Z',cls(x.cumulative_pnl_usdc))+m('$'+n(x.bankroll_usdc),'STRICT sanal bakiye')+m(x.attempts_executed??0,'STRICT işlem')+m(pc(x.pair_completion_rate),'İki bacak dolum')+m(pc(x.one_leg_rate),'Tek bacak oranı')+m('$'+n(x.max_drawdown_usdc),'Maks. düşüş')+m(x.confirmed_windows??0,'Onaylanan fırsat')+m(x.skipped_edge??0,'Edge yetersiz')+m(rd.status==='DRY_VALIDATED'?'DRY DOĞRULANDI':'HENÜZ HAZIR DEĞİL','Hazırlık',rd.status==='DRY_VALIDATED'?'ok':'warn');$('reasons').textContent=(rd.reasons||[]).map(trReason).join(' | ');
$('livecycles').innerHTML=(d.live_cycles||[]).map(v=>`<tr><td>${v.id}</td><td>${v.combo_key}</td><td>${v.status}</td><td>${n(v.quantity_shares,3)}</td><td>$${n(v.capital_usdc)}</td><td>${v.up_fill_verified?'✓':'—'}</td><td>${v.down_fill_verified?'✓':'—'}</td><td class="mono">${v.merge_tx_hash?String(v.merge_tx_hash).slice(0,12)+'…':'—'}</td><td>${v.error_code||'—'}</td></tr>`).join('');
$('attempts').innerHTML=(x.recent_attempts||[]).map(v=>`<tr><td>${v.window_id}</td><td>${v.combo_key}</td><td>${durum[v.dry_status]||v.dry_status}</td><td>${v.theoretical_net_profit_usdc==null?'—':'$'+n(v.theoretical_net_profit_usdc)}</td><td>${replayDurum[v.replay_outcome]||v.replay_outcome||'—'}</td><td class="${cls(v.cycle_net_pnl_usdc)}">${v.cycle_net_pnl_usdc==null?'—':'$'+n(v.cycle_net_pnl_usdc)}</td><td>${v.bankroll_after_usdc==null?'—':'$'+n(v.bankroll_after_usdc)}</td></tr>`).join('');
$('legacy').innerHTML=m('$'+n(lg.cumulative_pnl_usdc),'Eski toplam K/Z',cls(lg.cumulative_pnl_usdc))+m(lg.attempts_executed??0,'Eski işlem')+m(pc(lg.pair_completion_rate),'Eski iki bacak dolum')+m(pc(lg.one_leg_rate),'Eski tek bacak');$('scanner').innerHTML=m(s.conditions??0,'Aktif market')+m(s.valid_pairs??0,'Geçerli book çifti')+m(s.missing_book??0,'Eksik book',s.missing_book?'bad':'ok')+m(s.transport_stale??0,'Transport stale',s.transport_stale?'bad':'ok')+m(s.missing_fee??0,'Eksik komisyon',s.missing_fee?'bad':'ok')+m(t.connected?'CANLI':'KAPALI','Book socket',t.connected?'ok':'bad');}catch(e){$('state').textContent='HATA · '+e}}
setInterval(tick,3000);tick();</script></body></html>"""
