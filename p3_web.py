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
            "wallet_required": False,
            "wallet_loaded": False,
            "wallet_note": "DRY/SHADOW modunda cüzdan ve private key gerekmez.",
            "db_integrity": integrity_check(conn),
            "opportunities": int(conn.execute("SELECT COUNT(*) FROM p3_opportunities").fetchone()[0]),
            "windows": int(conn.execute("SELECT COUNT(*) FROM p3_windows").fetchone()[0]),
            "window_observations": int(conn.execute("SELECT COUNT(*) FROM p3_window_observations").fetchone()[0]),
            "replays": int(conn.execute("SELECT COUNT(*) FROM p3_replays").fetchone()[0]),
            "entry_replays": int(conn.execute("SELECT COUNT(*) FROM p3_entry_replays").fetchone()[0]),
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
            "ok": True,
            "mode": "SHADOW_PAPER_ONLY",
            "execution_enabled": False,
            "private_key_loaded": False,
            "signing_enabled": False,
            "order_submission_enabled": False,
            "wallet_required": False,
            "wallet_loaded": False,
        })

    async def summary(_request: web.Request) -> web.Response:
        return web.json_response(build_summary(settings))

    async def opportunities(request: web.Request) -> web.Response:
        limit = max(1, min(500, int(request.query.get("limit", "100"))))
        payload = build_summary(settings)
        return web.json_response({"paperOnly": True, "rows": payload["recent"][:limit]})

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


_HTML = r"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>P3 Arbitraj Laboratuvarı</title>
<style>
:root{--bg:#07101b;--panel:#101c2c;--line:#233650;--text:#eef5ff;--mut:#8ea5c3;--green:#20d095;--red:#f06b72;--blue:#65a9ff;--amber:#f1bd58}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:13px Inter,Arial,sans-serif}header{padding:14px 18px;border-bottom:1px solid var(--line);background:#0a1523;display:flex;gap:10px;align-items:center;flex-wrap:wrap}h1{font-size:18px;margin:0;color:var(--blue)}.pill{padding:5px 8px;border-radius:6px;background:#17375d;font-weight:800}.pill.ok{background:#0d5b43}.pill.warn{background:#6a4b0c}.wrap{max-width:1700px;margin:auto;padding:14px}.notice{border:1px solid #5f4a19;background:#251d09;padding:10px;border-radius:8px;color:#ffe1a0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:8px;margin-top:12px}.metric,.box{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:10px}.metric b{display:block;font-size:20px}.metric span{color:var(--mut)}.boxes{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}.box h2{font-size:14px;margin:0 0 8px}table{width:100%;border-collapse:collapse}th,td{padding:7px;border-bottom:1px solid #1e3048;text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}th{color:var(--mut)}.pos{color:var(--green)}.neg{color:var(--red)}.warn{color:var(--amber)}.mut{color:var(--mut)}.mono{font-family:ui-monospace,Consolas,monospace;font-size:11px}@media(max-width:900px){.boxes{grid-template-columns:1fr}}
</style></head><body>
<header><h1>P3 Arbitraj Laboratuvarı</h1><span class="pill">YAPISAL ARBİTRAJ</span><span class="pill ok">DRY / SHADOW</span><span class="pill warn">GERÇEK EMİR YOK</span><span id="state" class="mut">yükleniyor…</span></header>
<div class="wrap">
<div class="notice"><b>Şu an DRY/SHADOW modundayız.</b> Sistem gerçek para ile emir göndermez; cüzdan, private key ve imza gerekmez. STRICT testte fırsat önce doğrulanır, sonra confirmation zamanından itibaren execution replay yapılır. Eski veriler yalnız karşılaştırma amacıyla ayrı gösterilir.</div>
<div class="box" style="margin-top:12px"><h2>Güvenlik ve Çalışma Modu</h2><div class="grid" id="safety"></div></div>
<div class="grid" id="metrics"></div>
<div class="box" style="margin-top:12px"><h2>STRICT DRY — Ana Sonuçlar</h2><div class="grid" id="dry"></div><div id="reasons" class="mono warn" style="margin-top:10px"></div></div>
<div class="box" style="margin-top:12px"><h2>Eski / Gösterge Niteliğindeki Sonuçlar — STRICT Kararına Dahil Değil</h2><div class="grid" id="legacy"></div><div class="mono mut" style="margin-top:8px">Bu bölüm eski/coarse replay sonucudur. Canlıya geçiş kararı yalnız STRICT DRY verisiyle verilir.</div></div>
<div class="box" style="margin-top:12px"><h2>Onay Süresi Karşılaştırması</h2><div style="overflow:auto"><table><thead><tr><th>Onay Süresi</th><th>STRICT Window</th><th>Onaylanan</th><th>Zincir Kopması</th><th>Eski Veri Hariç</th><th>İşlem</th><th>İki Bacak Dolum</th><th>Tek Bacak</th><th>Net Kâr/Zarar</th><th>0 ms Farkı</th><th>Maks. Düşüş</th></tr></thead><tbody id="survival"></tbody></table></div></div>
<div class="box" style="margin-top:12px"><h2>Tarayıcı ve Emir Defteri Sağlığı</h2><div class="grid" id="scanner"></div></div>
<div class="boxes"><div class="box"><h2>Stratejiler</h2><table><thead><tr><th>Strateji</th><th>Gözlem</th><th>En Yüksek Kâr</th><th>Ort. ROI</th></tr></thead><tbody id="strategies"></tbody></table></div><div class="box"><h2>İki Bacak Replay — Gözlem Seviyesi</h2><table><thead><tr><th>Gecikme</th><th>Adet</th><th>İki Bacak Dolum</th><th>Tek Bacak</th><th>Ort. Kâr/Zarar</th></tr></thead><tbody id="replays"></tbody></table></div></div>
<div class="box" style="margin-top:12px"><h2>Son Bağımsız STRICT DRY İşlemler</h2><div style="overflow:auto;max-height:380px"><table><thead><tr><th>Window</th><th>Market</th><th>Durum</th><th>Giriş Yaşı</th><th>Maks. Boşluk</th><th>Sermaye</th><th>Teorik Kâr</th><th>Replay Sonucu</th><th>İşlem K/Z</th><th>Bakiye</th></tr></thead><tbody id="attempts"></tbody></table></div></div>
<div class="box" style="margin-top:12px"><h2>Son Arbitraj Fırsatı Gözlemleri</h2><div style="overflow:auto;max-height:420px"><table><thead><tr><th>Market</th><th>Miktar</th><th>Sermaye</th><th>Teorik Net Kâr</th><th>ROI</th><th>Kaynak Zaman Farkı</th></tr></thead><tbody id="recent"></tbody></table></div></div>
</div><script>
const $=id=>document.getElementById(id),n=(v,d=4)=>v==null?'—':Number(v).toFixed(d),pc=v=>v==null?'—':(Number(v)*100).toFixed(2)+'%',m=(v,l,k='')=>`<div class="metric"><b class="${k}">${v??0}</b><span>${l}</span></div>`,cls=v=>Number(v||0)>=0?'pos':'neg';
const durum={DRY_EXECUTED:'DRY İŞLEM YAPILDI',SKIPPED_CONFIRMATION:'ONAY SÜRESİNİ GEÇEMEDİ',CONFIRMATION_GAP:'ONAY ZİNCİRİ KOPTU',LEGACY_CONFIRMATION_UNPROVEN:'ESKİ VERİ / STRICT KANITSIZ',SKIPPED_EDGE_GATE:'KÂR EŞİĞİ ALTINDA',SKIPPED_CAPITAL_LIMIT:'SERMAYE LİMİTİ',PENDING_ENTRY_REPLAY:'REPLAY BEKLENİYOR',PENDING_CONFIRMATION:'ONAY BEKLENİYOR'};
const replayDurum={BOTH_FILLED:'İKİ BACAK DOLDU',ONE_LEG_FILLED_UNWIND:'TEK BACAK DOLDU / KAPATILDI',ONE_LEG_UNWIND_FAILED:'TEK BACAK / KAPATMA BAŞARISIZ',NONE_FILLED:'HİÇBİR BACAK DOLMADI',NO_SYNCHRONOUS_BOOK:'EŞZAMANLI BOOK YOK',FEE_SCHEDULE_UNAVAILABLE:'KOMİSYON VERİSİ YOK'};
function trReason(r){if(!r)return'';if(r.startsWith('INSUFFICIENT_INDEPENDENT_WINDOWS:'))return'Yeterli bağımsız STRICT işlem yok: '+r.split(':')[1];if(r.startsWith('PAIR_COMPLETION_TOO_LOW:'))return'İki bacak dolum oranı düşük: '+r.split(':')[1];if(r.startsWith('PAIR_WILSON_LOWER_TOO_LOW:'))return'İstatistiksel güven alt sınırı düşük: '+r.split(':')[1];if(r.startsWith('ONE_LEG_RATE_TOO_HIGH:'))return'Tek bacakta kalma oranı yüksek: '+r.split(':')[1];if(r.startsWith('CUMULATIVE_PNL_NOT_POSITIVE:'))return'Toplam kâr henüz pozitif değil: '+r.split(':')[1];if(r.startsWith('AVERAGE_PNL_NOT_POSITIVE:'))return'İşlem başı ortalama kâr henüz pozitif değil: '+r.split(':')[1];if(r.startsWith('MAX_DRAWDOWN_EXCEEDED:'))return'Maksimum düşüş limiti aşıldı: '+r.split(':')[1];const map={NO_STRICT_TIMELINE_EVIDENCE:'Henüz STRICT zaman çizelgesi verisi yok',NO_ACTIVE_CONDITIONS:'Aktif market bulunamadı',NOT_ALL_BOOK_PAIRS_VALID:'Tüm UP/DOWN book çiftleri geçerli değil',FEE_LINEAGE_INCOMPLETE:'Komisyon verisi eksik',BOOK_TRANSPORT_NOT_LIVE:'Order book bağlantısı canlı değil'};return map[r]||r}
async function tick(){try{const d=await(await fetch('/api/summary',{cache:'no-store'})).json(),s=d.scanner||{},t=s.book_transport||{},x=d.dry_run||{},rd=x.readiness||{},lg=x.legacy_indicative||{};$('state').textContent='CANLI · '+new Date().toLocaleTimeString();
$('safety').innerHTML=m('DRY / SHADOW','Çalışma modu','pos')+m(d.execution_enabled?'AÇIK':'KAPALI','Gerçek emir',d.execution_enabled?'neg':'pos')+m(d.order_submission_enabled?'AÇIK':'KAPALI','Emir gönderimi',d.order_submission_enabled?'neg':'pos')+m(d.signing_enabled?'AÇIK':'KAPALI','İmza',d.signing_enabled?'neg':'pos')+m(d.private_key_loaded?'YÜKLÜ':'YÜKLÜ DEĞİL','Private key',d.private_key_loaded?'neg':'pos')+m(d.wallet_required?'GEREKLİ':'GEREKLİ DEĞİL','Cüzdan (DRY)',d.wallet_required?'warn':'pos')+m(d.db_integrity==='ok'?'SAĞLAM':d.db_integrity,'Veritabanı','pos');
$('metrics').innerHTML=m(d.opportunities,'Fırsat gözlemi')+m(d.windows,'Bağımsız window')+m(d.window_observations??0,'STRICT zaman gözlemi')+m(d.entry_replays??0,'STRICT giriş replay')+m(d.replays,'Genel replay')+m(n(d.lifetime?.median_ms,0)+' ms','Ortanca fırsat ömrü');
$('dry').innerHTML=m('$'+n(x.cumulative_pnl_usdc),'STRICT toplam K/Z',cls(x.cumulative_pnl_usdc))+m('$'+n(x.bankroll_usdc),'STRICT sanal bakiye')+m(x.attempts_executed??0,'Gerçekçi DRY işlem')+m(x.strict_timeline_windows??0,'STRICT window')+m(x.legacy_unproven_windows??0,'Eski veri hariç','warn')+m(x.confirmation_gaps??0,'Onay zinciri kopması',x.confirmation_gaps?'warn':'')+m(pc(x.confirmation_survival_rate),'Onaydan sağ çıkma')+m(pc(x.pair_completion_rate),'İki bacak dolum')+m(pc(x.pair_completion_wilson_lower_95),'Wilson alt sınır %95')+m(pc(x.one_leg_rate),'Tek bacak oranı',Number(x.one_leg_rate||0)>0.03?'warn':'')+m('$'+n(x.max_drawdown_usdc),'Maksimum düşüş')+m(rd.status==='DRY_VALIDATED'?'DRY DOĞRULANDI':'HENÜZ HAZIR DEĞİL','Hazırlık',rd.status==='DRY_VALIDATED'?'pos':'warn')+m((x.entry_confirm_ms??0)+' ms','Giriş onayı')+m((x.confirm_max_gap_ms??0)+' ms','Maks. onay boşluğu')+m((x.latency_ms??0)+' ms','Simüle execution gecikmesi');
$('reasons').textContent=(rd.reasons||[]).map(trReason).join(' | ')||'Tüm DRY doğrulama şartları geçti.';
$('legacy').innerHTML=m('$'+n(lg.cumulative_pnl_usdc),'Eski toplam K/Z',cls(lg.cumulative_pnl_usdc))+m('$'+n(lg.bankroll_usdc),'Eski sanal bakiye')+m(lg.attempts_executed??0,'Eski işlem adedi')+m(pc(lg.pair_completion_rate),'Eski iki bacak dolum')+m(pc(lg.one_leg_rate),'Eski tek bacak')+m('$'+n(lg.max_drawdown_usdc),'Eski maks. düşüş');
const grid=x.survival_by_confirm_ms||{};$('survival').innerHTML=Object.entries(grid).sort((a,b)=>Number(a[0])-Number(b[0])).map(([k,v])=>`<tr><td class="${Number(k)===Number(x.entry_confirm_ms)?'pos':''}">${k} ms${Number(k)===Number(x.entry_confirm_ms)?' · AKTİF':''}</td><td>${v.strict_timeline_windows}</td><td>${v.confirmed_windows} (${pc(v.confirmation_survival_rate)})</td><td class="${v.confirmation_gaps?'warn':''}">${v.confirmation_gaps}</td><td class="${v.legacy_unproven_windows?'warn':''}">${v.legacy_unproven_windows}</td><td>${v.attempts_executed}</td><td>${pc(v.pair_completion_rate)}</td><td class="${Number(v.one_leg_rate||0)>0.03?'warn':''}">${v.one_leg} (${pc(v.one_leg_rate)})</td><td class="${cls(v.cumulative_pnl_usdc)}">$${n(v.cumulative_pnl_usdc)}</td><td class="${cls(v.pnl_delta_vs_0_usdc)}">$${n(v.pnl_delta_vs_0_usdc)}</td><td>$${n(v.max_drawdown_usdc)}</td></tr>`).join('');
$('scanner').innerHTML=m(s.conditions??0,'Aktif market')+m(s.valid_pairs??0,'Geçerli UP/DOWN book çifti',s.valid_pairs?'pos':'warn')+m(s.missing_book??0,'Eksik book',s.missing_book?'neg':'')+m(s.transport_stale??0,'Eski/stale bağlantı',s.transport_stale?'neg':'')+m(s.session_incomplete??0,'Eksik oturum',s.session_incomplete?'warn':'')+m(s.missing_fee??0,'Eksik komisyon',s.missing_fee?'neg':'')+m(t.connected?'CANLI':'KAPALI','Order book soketi',t.connected?'pos':'neg')+m(t.subscribed_tokens??0,'Abone token');
$('strategies').innerHTML=Object.entries(d.strategies||{}).map(([k,v])=>`<tr><td class="mono">${k}</td><td>${v.opportunities}</td><td class="pos">$${n(v.peak_profit_usdc)}</td><td>${pc(v.avg_roi)}</td></tr>`).join('');
$('replays').innerHTML=Object.entries(d.replay_by_delay||{}).map(([k,v])=>`<tr><td>${k} ms</td><td>${v.n}</td><td>${pc(v.pair_completion_rate)}</td><td>${v.one_leg}</td><td class="${cls(v.avg_cycle_pnl_usdc)}">$${n(v.avg_cycle_pnl_usdc)}</td></tr>`).join('');
$('attempts').innerHTML=(x.recent_attempts||[]).map(v=>`<tr><td>${v.window_id}</td><td>${v.combo_key}</td><td>${durum[v.dry_status]||v.dry_status}</td><td>${v.entry_age_ms==null?'—':v.entry_age_ms+' ms'}</td><td>${v.max_gap_seen_ms==null?'—':v.max_gap_seen_ms+' ms'}</td><td>${v.capital_usdc==null?'—':'$'+n(v.capital_usdc)}</td><td>${v.theoretical_net_profit_usdc==null?'—':'$'+n(v.theoretical_net_profit_usdc)}</td><td>${replayDurum[v.replay_outcome]||v.replay_outcome||'—'}</td><td class="${cls(v.cycle_net_pnl_usdc)}">${v.cycle_net_pnl_usdc==null?'—':'$'+n(v.cycle_net_pnl_usdc)}</td><td>${v.bankroll_after_usdc==null?'—':'$'+n(v.bankroll_after_usdc)}</td></tr>`).join('');
$('recent').innerHTML=(d.recent||[]).map(v=>`<tr><td>${v.combo_key}</td><td>${n(v.quantity_shares,2)}</td><td>$${n(v.capital_usdc)}</td><td class="pos">$${n(v.net_profit_usdc)}</td><td>${pc(v.net_roi)}</td><td>${v.source_skew_ms} ms</td></tr>`).join('');}catch(e){$('state').textContent='HATA · '+e}}
setInterval(tick,3000);tick();</script></body></html>"""
