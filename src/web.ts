import http from "node:http";
import https from "node:https";
import fs from "node:fs";
import { timingSafeEqual } from "node:crypto";
import { validateConfig, cfg } from "./config.js";
import { WebController } from "./webController.js";
import { log } from "./logger.js";

const PORT = Number(process.env.WEB_PORT || 3000);
const HOST = process.env.WEB_HOST || "127.0.0.1"; // guvenlik: varsayilan sadece localhost
const AUTH_USER = process.env.WEB_USER || "";
const AUTH_PASS = process.env.WEB_PASS || "";
const IS_LOCAL = HOST === "127.0.0.1" || HOST === "localhost";
const AUTH_ON = Boolean(AUTH_USER && AUTH_PASS);
const TLS_CERT = process.env.WEB_TLS_CERT || "";
const TLS_KEY = process.env.WEB_TLS_KEY || "";
const TLS_ON =
  Boolean(TLS_CERT && TLS_KEY) && fs.existsSync(TLS_CERT) && fs.existsSync(TLS_KEY);

const ctrl = new WebController();

// --- Brute-force korumasi: IP basina hatali deneme sayaci ---
const fails = new Map<string, { count: number; until: number }>();
const MAX_FAILS = 8;
const LOCK_MS = 5 * 60_000;
function ipOf(req: http.IncomingMessage): string {
  return String(req.socket.remoteAddress || "?");
}
function isLocked(ip: string): boolean {
  const e = fails.get(ip);
  return Boolean(e && e.until > Date.now());
}
function noteFail(ip: string) {
  const e = fails.get(ip) || { count: 0, until: 0 };
  e.count++;
  if (e.count >= MAX_FAILS) {
    e.until = Date.now() + LOCK_MS;
    e.count = 0;
    log.warn(`Brute-force kilidi: ${ip} ${LOCK_MS / 60000}dk bloklandi`);
  }
  fails.set(ip, e);
}
function noteOk(ip: string) {
  fails.delete(ip);
}

/** Sabit-zamanli string karsilastirma (timing attack'e karsi). */
function safeEq(a: string, b: string): boolean {
  const ba = Buffer.from(a);
  const bb = Buffer.from(b);
  if (ba.length !== bb.length) return false;
  return timingSafeEqual(ba, bb);
}

/** HTTP Basic Auth kontrolu. Auth kapaliysa (yerel) her zaman gecer. */
function checkAuth(req: http.IncomingMessage): boolean {
  if (!AUTH_ON) return true;
  const h = req.headers.authorization || "";
  if (!h.startsWith("Basic ")) return false;
  const [u, p] = Buffer.from(h.slice(6), "base64").toString().split(":");
  return safeEq(u || "", AUTH_USER) && safeEq(p || "", AUTH_PASS);
}

function json(res: http.ServerResponse, code: number, body: unknown) {
  const s = JSON.stringify(body);
  res.writeHead(code, { "Content-Type": "application/json" });
  res.end(s);
}

async function readBody(req: http.IncomingMessage): Promise<any> {
  return new Promise((resolve) => {
    let data = "";
    req.on("data", (c) => (data += c));
    req.on("end", () => {
      try {
        resolve(data ? JSON.parse(data) : {});
      } catch {
        resolve({});
      }
    });
  });
}

async function handler(req: http.IncomingMessage, res: http.ServerResponse) {
  const url = req.url || "/";
  try {
    const ip = ipOf(req);
    if (AUTH_ON && isLocked(ip)) {
      res.writeHead(429);
      res.end("cok fazla hatali deneme, biraz bekle");
      return;
    }
    if (!checkAuth(req)) {
      if (AUTH_ON) noteFail(ip);
      res.writeHead(401, {
        "WWW-Authenticate": 'Basic realm="basit-arbitraj panel"',
      });
      res.end("yetkisiz");
      return;
    }
    if (AUTH_ON) noteOk(ip);
    if (url === "/" || url === "/index.html") {
      res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      res.end(HTML);
      return;
    }
    if (url === "/api/state") {
      json(res, 200, ctrl.snapshot());
      return;
    }
    if (url === "/api/place" && req.method === "POST") {
      const b = await readBody(req);
      const price = Number(b.price ?? cfg.targetLegPrice);
      const shares = Number(b.shares ?? cfg.pairShares);
      const r = await ctrl.placeBox(price, shares);
      json(res, r.ok ? 200 : 400, r);
      return;
    }
    if (url === "/api/auto" && req.method === "POST") {
      const b = await readBody(req);
      ctrl.setAuto(Boolean(b.on), {
        mode: b.mode === "adaptive" ? "adaptive" : b.mode === "fixed" ? "fixed" : undefined,
        price: b.price != null ? Number(b.price) : undefined,
        shares: b.shares != null ? Number(b.shares) : undefined,
        proxUsd: b.proxUsd != null ? Number(b.proxUsd) : undefined,
        minSec: b.minSec != null ? Number(b.minSec) : undefined,
        maxSec: b.maxSec != null ? Number(b.maxSec) : undefined,
        maxCombined: b.maxCombined != null ? Number(b.maxCombined) : undefined,
      });
      json(res, 200, { ok: true, msg: `Oto ${b.on ? "AÇIK" : "KAPALI"}` });
      return;
    }
    if (url === "/api/cancel" && req.method === "POST") {
      json(res, 200, await ctrl.cancelAll());
      return;
    }
    if (url === "/api/reset" && req.method === "POST") {
      json(res, 200, await ctrl.reset());
      return;
    }
    res.writeHead(404);
    res.end("not found");
  } catch (e) {
    json(res, 500, { ok: false, msg: (e as Error).message });
  }
}

const server = TLS_ON
  ? https.createServer(
      { cert: fs.readFileSync(TLS_CERT), key: fs.readFileSync(TLS_KEY) },
      handler
    )
  : http.createServer(handler);

async function main() {
  validateConfig();

  // GUVENLIK: halka acik bind (0.0.0.0 vb) sadece sifre (Basic Auth) ile.
  if (!IS_LOCAL && !AUTH_ON) {
    throw new Error(
      `Halka acik bind (WEB_HOST=${HOST}) icin sifre zorunlu. ` +
        `.env'e WEB_USER ve WEB_PASS ekle (uzun/guclu bir key).`
    );
  }
  // Halka acik + sifreli ama TLS yoksa uyar (sifre acik metin gider).
  if (!IS_LOCAL && !TLS_ON) {
    log.warn(
      "TLS KAPALI: sifre sifrelenmeden (HTTP) gidiyor. HTTPS icin WEB_TLS_CERT/WEB_TLS_KEY ayarla (README)."
    );
  }

  const scheme = TLS_ON ? "https" : "http";
  await ctrl.start();
  server.listen(PORT, HOST, () => {
    log.ok(
      `Web panel: ${scheme}://${HOST}:${PORT}  (DRY_RUN=${cfg.dryRun}, auth=${AUTH_ON ? "ACIK" : "KAPALI"}, tls=${TLS_ON ? "ACIK" : "KAPALI"})`
    );
    if (IS_LOCAL) {
      log.info(`SSH tuneli: ssh -L ${PORT}:localhost:${PORT} KULLANICI@VPS_IP`);
    } else {
      log.warn(`PANEL HALKA ACIK (${HOST}:${PORT}). DRY_RUN=false iken cok dikkatli ol.`);
    }
  });
}

main().catch((e) => {
  log.err("Fatal:", e);
  process.exit(1);
});

const HTML = /* html */ `<!doctype html>
<html lang="tr"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Basit-Arbitraj Panel</title>
<style>
  :root{--bg:#0d1117;--card:#161b22;--bd:#30363d;--fg:#e6edf3;--mut:#8b949e;
    --up:#2ea043;--down:#f85149;--acc:#58a6ff;--warn:#d29922}
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}
  .wrap{max-width:840px;margin:0 auto;padding:18px}
  h1{font-size:18px;margin:0 0 4px} .sub{color:var(--mut);font-size:12px;margin-bottom:16px}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  .card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px}
  .card h2{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);margin:0 0 8px}
  .big{font-size:28px;font-weight:700}
  .row{display:flex;justify-content:space-between;padding:3px 0}
  .row .k{color:var(--mut)} .mono{font-variant-numeric:tabular-nums}
  .up{color:var(--up)} .down{color:var(--down)} .acc{color:var(--acc)} .warn{color:var(--warn)}
  .badge{display:inline-block;padding:2px 8px;border-radius:6px;font-size:11px;font-weight:600}
  .badge.dry{background:#1f6feb33;color:var(--acc);border:1px solid #1f6feb66}
  .badge.live{background:#f8514933;color:var(--down);border:1px solid #f8514966}
  .controls{margin-top:12px;display:flex;gap:10px;flex-wrap:wrap;align-items:end}
  label{display:block;font-size:11px;color:var(--mut);margin-bottom:4px}
  input{background:#0d1117;border:1px solid var(--bd);color:var(--fg);border-radius:6px;padding:8px;width:90px;font-size:15px}
  button{border:0;border-radius:6px;padding:9px 16px;font-size:14px;font-weight:600;cursor:pointer;color:#fff}
  .b-go{background:var(--up)} .b-cancel{background:#6e7681} .b-reset{background:var(--down)}
  button:disabled{opacity:.5;cursor:not-allowed}
  table{width:100%;border-collapse:collapse;margin-top:6px} td,th{text-align:right;padding:4px 6px}
  th{color:var(--mut);font-weight:500;font-size:11px} td:first-child,th:first-child{text-align:left}
  .msg{margin-top:10px;font-size:13px;min-height:18px}
  .full{grid-column:1/3}
  .fill-bar{height:5px;background:#21262d;border-radius:3px;overflow:hidden;margin-top:3px}
  .fill-bar>div{height:100%;background:var(--acc)}
  small{color:var(--mut)}
</style></head>
<body><div class="wrap">
  <h1>Basit-Arbitraj — BTC 5dk BOX Panel <span id="mode" class="badge dry">…</span></h1>
  <div class="sub">UP + DOWN limit emir, toplam &lt; $1 → garanti kâr · <span id="status">…</span></div>

  <div class="grid">
    <div class="card">
      <h2>Net PnL (kilitli kutulardan, garanti)</h2>
      <div class="big mono" id="pnl">$0.00</div>
      <div class="row"><span class="k">Kilitlenen box</span><span class="mono" id="lockedCount">0</span></div>
      <div class="row" id="openRiskRow" style="display:none;border-top:1px solid var(--bd);margin-top:6px;padding-top:6px">
        <span class="k">⚠ Açık risk (naked)</span><span class="mono" id="openRisk">—</span>
      </div>
      <div class="row" id="totalRow" style="display:none">
        <span class="k">Toplam (gerçekleşen + açık)</span><span class="mono" id="totalPnl">—</span>
      </div>
    </div>
    <div class="card">
      <h2>Aktif Market</h2>
      <div class="row"><span class="k">Slug</span><span class="mono acc" id="slug">—</span></div>
      <div class="row"><span class="k">Strike (priceToBeat) <small id="strikeSrc"></small></span><span class="mono" id="strike">—</span></div>
      <div class="row"><span class="k">BTC fiyat <small id="src"></small></span><span class="mono" id="spot">—</span></div>
      <div class="row"><span class="k">Fark (spot − strike)</span><span class="mono" id="dist">—</span></div>
      <div class="row"><span class="k">Kalan süre</span><span class="mono" id="secLeft">—</span></div>
    </div>

    <div class="card full">
      <h2>Order Book & Emirlerin</h2>
      <table>
        <tr><th>Taraf</th><th>Best Bid</th><th>Best Ask</th><th>Senin Limit</th><th>Dolan / Toplam</th></tr>
        <tr><td class="up">UP</td><td class="mono" id="u_bid">—</td><td class="mono" id="u_ask">—</td>
            <td class="mono" id="u_lim">—</td><td class="mono" id="u_fill">—</td></tr>
        <tr><td class="down">DOWN</td><td class="mono" id="d_bid">—</td><td class="mono" id="d_ask">—</td>
            <td class="mono" id="d_lim">—</td><td class="mono" id="d_fill">—</td></tr>
      </table>
      <div class="row" style="margin-top:8px"><span class="k">Ask toplamı (ikisini ALırsan = taker)</span>
        <span class="mono" id="askSum">—</span></div>
      <div class="row"><span class="k">Bid toplamı (ikisini SATarsan)</span>
        <span class="mono" id="bidSum">—</span></div>
      <div class="row"><span class="k">Senin doldurduğun combined</span>
        <span class="mono" id="combined">—</span></div>

      <div class="controls">
        <div><label>Limit fiyat ($)</label><input id="price" type="number" step="0.01" value="0.40"></div>
        <div><label>Share (her bacak)</label><input id="shares" type="number" step="1" value="5"></div>
        <button class="b-go" id="go">Box Yerleştir (UP+DOWN)</button>
        <button class="b-cancel" id="cancel">İptal</button>
        <button class="b-reset" id="reset">Reset</button>
      </div>
      <div class="msg" id="msg"></div>

      <hr style="border:0;border-top:1px solid var(--bd);margin:14px 0">
      <h2>🤖 Otomatik mod <span id="autoBadge" class="badge" style="background:#6e768133">KAPALI</span></h2>
      <div class="controls" style="margin-bottom:8px">
        <div><label>Mod</label>
          <select id="mode" style="background:#0d1117;border:1px solid var(--bd);color:var(--fg);border-radius:6px;padding:8px;font-size:14px">
            <option value="fixed">fixed (0.40/0.40 · tombul)</option>
            <option value="adaptive">adaptive (best-bid · sık/ince)</option>
          </select>
        </div>
        <div><label>En erken (≤ sn kala)</label><input id="maxSec" type="number" step="5" value="45"></div>
        <div><label>En geç (≥ sn kala)</label><input id="minSec" type="number" step="5" value="20"></div>
      </div>
      <div style="color:var(--mut);font-size:12px;margin-bottom:8px">
        <b>Emir penceresi:</b> kalan süre <b id="winLbl">45–20</b>s arasındayken box açar (son saniyeler).<br>
        <b>fixed:</b> spot priceToBeat'e ≤ <b id="pxLbl">2</b>$ + iki bacak da 0.40 üstündeyken 0.40/0.40 koyar.<br>
        <b>adaptive:</b> her bacağı best bid'ine oturtur, combined ≤ <b id="mcLbl">0.97</b> tutar.
      </div>
      <div class="controls">
        <div><label>Yakınlık ≤ $ (fixed)</label><input id="proxUsd" type="number" step="0.5" value="2"></div>
        <div><label>Max combined (adaptive)</label><input id="maxCombined" type="number" step="0.01" value="0.97"></div>
        <button class="b-go" id="autoOn">Oto AÇ</button>
        <button class="b-cancel" id="autoOff">Oto KAPAT</button>
      </div>
      <div class="msg" id="autoMsg"></div>
    </div>

    <div class="card full">
      <h2>Son kilitlenen kutular</h2>
      <table id="lockedTbl"><tr><th>Slug</th><th>Share</th><th>Combined</th><th>Kâr ($)</th></tr></table>
    </div>
  </div>
  <p style="margin-top:14px"><small>Fiyatlar 0–1 arası olasılık = USD/share. DRY_RUN'da emirler simüledir.</small></p>
</div>
<script>
const $ = id => document.getElementById(id);
const f = (v,d=3) => v==null ? "—" : Number(v).toFixed(d);

async function poll(){
  try{
    const s = await (await fetch("/api/state")).json();
    $("mode").textContent = s.dryRun ? "DRY_RUN" : "CANLI";
    $("mode").className = "badge " + (s.dryRun ? "dry" : "live");
    $("status").textContent = s.status + (s.lastError ? " · hata: "+s.lastError : "");
    $("pnl").textContent = "$" + f(s.netPnl,2);
    $("pnl").className = "big mono " + (s.netPnl>0?"up":s.netPnl<0?"down":"");
    $("lockedCount").textContent = s.lockedCount;
    // Açık risk (naked bacak) göstergesi
    if(s.openRisk){
      const o=s.openRisk;
      $("openRiskRow").style.display="flex";
      $("totalRow").style.display="flex";
      $("openRisk").innerHTML = o.side+" "+o.shares+" @ "+f(o.avg)+
        " → şimdi: <b class='"+(o.unrealized>=0?"up":"down")+"'>"+(o.unrealized>=0?"+":"")+f(o.unrealized,2)+"$</b>"+
        " · ters:<span class='down'>"+f(o.worst,2)+"$</span> lehte:<span class='up'>+"+f(o.best,2)+"$</span>";
      $("totalPnl").textContent="$"+f(s.totalPnl,2);
      $("totalPnl").className="mono "+(s.totalPnl>0?"up":s.totalPnl<0?"down":"");
    } else {
      $("openRiskRow").style.display="none";
      $("totalRow").style.display="none";
    }
    if(s.market){
      $("slug").textContent = s.market.slug;
      $("strike").textContent = f(s.market.strike,2);
      const ss = s.market.strikeSrc;
      $("strikeSrc").textContent = ss==="polymarket-openPrice" ? "(polymarket ✓)"
        : ss==="chainlink-tick" ? "(chainlink~)" : "(fallback ⚠)";
      $("strikeSrc").style.color = ss==="polymarket-openPrice" ? "var(--up)"
        : ss==="chainlink-tick" ? "var(--acc)" : "var(--warn)";
      $("secLeft").textContent = s.market.secLeft + "s";
      $("secLeft").className = "mono " + (s.market.secLeft<20?"warn":"");
    }
    $("spot").textContent = f(s.spot,2);
    $("src").textContent = "("+s.priceSource+")";
    if(s.dist!=null){
      $("dist").textContent = (s.dist>=0?"+":"") + f(s.dist,2) + " $  (" +
        (s.dist>=0?"UP tarafı":"DOWN tarafı") + ")";
      $("dist").className = "mono " + (s.dist>=0?"up":"down");
    } else { $("dist").textContent="—"; $("dist").className="mono"; }
    $("u_bid").textContent=f(s.up.bestBid); $("u_ask").textContent=f(s.up.bestAsk);
    $("u_lim").textContent=f(s.up.limit); $("u_fill").textContent=s.up.filled+" / "+(s.up.shares??"—");
    $("d_bid").textContent=f(s.down.bestBid); $("d_ask").textContent=f(s.down.bestAsk);
    $("d_lim").textContent=f(s.down.limit); $("d_fill").textContent=s.down.filled+" / "+(s.down.shares??"—");
    const askSum = (s.up.bestAsk!=null&&s.down.bestAsk!=null)?s.up.bestAsk+s.down.bestAsk:null;
    const bidSum = (s.up.bestBid!=null&&s.down.bestBid!=null)?s.up.bestBid+s.down.bestBid:null;
    $("askSum").textContent = askSum!=null ? f(askSum)+(askSum<1?" ✓ AL=kâr":" ✗ AL=zarar"):"—";
    $("askSum").className = "mono " + (askSum!=null?(askSum<1?"up":"down"):"");
    $("bidSum").textContent = bidSum!=null ? f(bidSum)+" (SAT=zarar, owe $1)":"—";
    $("bidSum").className = "mono warn";
    $("combined").textContent = s.combined!=null ? f(s.combined)+(s.combined<1?" ✓ kâr":" ✗"):"—";
    $("combined").className = "mono " + (s.combined!=null ? (s.combined<1?"up":"down"):"");
    $("go").disabled = s.pinned;
    // Otomatik mod göstergesi
    const ab = $("autoBadge");
    ab.textContent = s.auto ? "AÇIK" : "KAPALI";
    ab.style.background = s.auto ? "#2ea04333" : "#6e768133";
    ab.style.color = s.auto ? "var(--up)" : "var(--mut)";
    $("pxLbl").textContent = s.autoProxUsd;
    $("mcLbl").textContent = s.autoMaxCombined;
    $("winLbl").textContent = s.autoMaxSec + "–" + s.autoMinSec;
    const tbl = $("lockedTbl");
    tbl.innerHTML = "<tr><th>Slug</th><th>Share</th><th>Combined</th><th>Kâr ($)</th></tr>" +
      (s.locked||[]).map(l=>"<tr><td>"+l.slug+"</td><td class=mono>"+l.shares+
      "</td><td class=mono>"+f(l.combined)+"</td><td class='mono up'>"+f(l.profit,3)+"</td></tr>").join("");
  }catch(e){ $("status").textContent = "panel bağlantı hatası"; }
}
async function post(path, body){
  const r = await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify(body||{})});
  const j = await r.json();
  $("msg").textContent = j.msg || "";
  $("msg").className = "msg " + (j.ok?"up":"down");
  poll();
}
$("go").onclick = ()=>post("/api/place",{price:Number($("price").value),shares:Number($("shares").value)});
$("cancel").onclick = ()=>post("/api/cancel");
$("reset").onclick = ()=>post("/api/reset");
async function postAuto(on){
  const r = await fetch("/api/auto",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({on,mode:$("mode").value,price:Number($("price").value),shares:Number($("shares").value),
      proxUsd:Number($("proxUsd").value),minSec:Number($("minSec").value),maxSec:Number($("maxSec").value),
      maxCombined:Number($("maxCombined").value)})});
  const j = await r.json(); $("autoMsg").textContent=j.msg||""; $("autoMsg").className="msg up"; poll();
}
$("autoOn").onclick = ()=>postAuto(true);
$("autoOff").onclick = ()=>postAuto(false);
poll(); setInterval(poll, 1000);
</script>
</body></html>`;
