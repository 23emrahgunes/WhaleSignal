(() => {
  "use strict";

  const REFRESH_MS = Number(
    document.documentElement.dataset.refreshMs || "3000"
  );
  const csrfMeta = document.querySelector('meta[name="p3-csrf"]');
  const CSRF = csrfMeta ? csrfMeta.content : "";
  const byId = (id) => document.getElementById(id);
  const stateNode = byId("state");
  let refreshHandle = null;
  let requestInFlight = false;

  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

  const number = (value, digits = 3) => {
    if (value === null || value === undefined || value === "") return "—";
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed.toFixed(digits) : "—";
  };

  const percent = (value) => {
    if (value === null || value === undefined || value === "") return "—";
    const parsed = Number(value);
    return Number.isFinite(parsed) ? `${(parsed * 100).toFixed(1)}%` : "—";
  };

  const pnlClass = (value) => Number(value || 0) >= 0 ? "ok" : "bad";

  const metric = (value, label, cssClass = "") => (
    `<div class="metric"><b class="${escapeHtml(cssClass)}">` +
    `${escapeHtml(value ?? "—")}</b><span>${escapeHtml(label)}</span></div>`
  );

  const showState = (text, cssClass = "mut") => {
    if (!stateNode) return;
    stateNode.textContent = text;
    stateNode.className = cssClass;
  };

  const fetchJson = async (url, options = {}) => {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(url, {
        credentials: "same-origin",
        cache: "no-store",
        ...options,
        headers: {
          Accept: "application/json",
          ...(options.headers || {}),
        },
        signal: controller.signal,
      });

      if (response.status === 401) {
        window.location.assign("/login");
        throw new Error("AUTH_REQUIRED");
      }

      const text = await response.text();
      let data = {};
      try {
        data = text ? JSON.parse(text) : {};
      } catch (_error) {
        throw new Error(`INVALID_JSON_HTTP_${response.status}`);
      }

      if (!response.ok) {
        const reason = data.error || data.reason || `HTTP_${response.status}`;
        const error = new Error(String(reason));
        error.payload = data;
        throw error;
      }
      return data;
    } finally {
      window.clearTimeout(timeout);
    }
  };

  const renderStatus = (data) => {
    const dual = data.dual40 || {};
    const live = data.live || {};
    const states = dual.state || {};
    const paperState = states.PAPER || {};
    const liveState = states.LIVE || {};
    const policy = dual.policy || {};
    const ladder = policy.ladder || dual.ladder || [];

    const mode = live.mode || data.mode || "DRY";
    const modePill = byId("modepill");
    if (modePill) {
      modePill.textContent = mode;
      modePill.className = `pill ${
        mode === "LIVE_ARMED" ? "bad" : mode === "LIVE_HALTED" ? "warn" : "ok"
      }`;
    }

    const notice = byId("notice");
    if (notice) {
      notice.innerHTML = mode === "LIVE_ARMED"
        ? "<b>CANLI MOD ARM EDİLDİ.</b> İlk uygun stabil markette iki gerçek 40¢ POST-ONLY GTC emir gönderilebilir."
        : "<b>DRY / PAPER.</b> 5 → 10 → 30 global recovery; 30 sonrası HARD STOP. 41¢ yalnız near-touch tanısıdır, fill kanıtı değildir.";
    }

    const paperLevel = paperState.level_index == null
      ? "—"
      : `${ladder[paperState.level_index] ?? "—"} share`;
    const liveLevel = liveState.level_index == null
      ? "—"
      : `${ladder[liveState.level_index] ?? "—"} share`;

    const status = byId("status");
    if (status) {
      status.innerHTML =
        metric(data.strategy_mode, "Strateji") +
        metric(mode, "Çalışma modu", mode === "LIVE_ARMED" ? "bad" : "ok") +
        metric(policy.price == null ? "—" : `${Math.round(Number(policy.price) * 100)}¢`, "İki taraf fiyatı") +
        metric(ladder.join(" → "), "Merdiven") +
        metric(paperLevel, "Paper seviye") +
        metric(`$${number(paperState.loss_pool_usdc)}`, "Paper zarar havuzu", Number(paperState.loss_pool_usdc || 0) > 0 ? "warn" : "ok") +
        metric(paperState.hard_stopped ? "HARD STOP" : "AÇIK", "Paper kilidi", paperState.hard_stopped ? "bad" : "ok") +
        metric(liveLevel, "LIVE seviye") +
        metric(`$${number(liveState.loss_pool_usdc)}`, "LIVE zarar havuzu", Number(liveState.loss_pool_usdc || 0) > 0 ? "warn" : "ok") +
        metric(liveState.hard_stopped ? "HARD STOP" : "AÇIK", "LIVE kilidi", liveState.hard_stopped ? "bad" : "ok") +
        metric(`$${number(policy.full_ladder_capital_usdc)}`, "Tam merdiven minimumu") +
        metric(`$${number(policy.minimum_live_collateral_usdc)}`, "LIVE arm minimumu") +
        metric(data.db_integrity === "ok" ? "SAĞLAM" : data.db_integrity, "Veritabanı", data.db_integrity === "ok" ? "ok" : "bad");
    }
  };

  const renderPerformance = (data) => {
    const dual = data.dual40 || {};
    const performance = dual.performance || {};
    const paper = performance.PAPER || {};
    const live = performance.LIVE || {};
    const node = byId("performance");
    if (!node) return;

    node.innerHTML =
      metric(paper.cycles ?? 0, "Paper cycle") +
      metric(paper.settled ?? 0, "Paper settled") +
      metric(`${paper.wins ?? 0}/${paper.losses ?? 0}`, "Paper W/L") +
      metric(`$${number(paper.realized_pnl_usdc)}`, "Paper PnL", pnlClass(paper.realized_pnl_usdc)) +
      metric(percent(paper.pair_completion_rate), "Paper çift dolum") +
      metric(percent(paper.single_leg_rate), "Paper tek bacak") +
      metric(`$${number(paper.max_drawdown_usdc)}`, "Paper max DD", Number(paper.max_drawdown_usdc || 0) > 0 ? "warn" : "ok") +
      metric(live.cycles ?? 0, "LIVE cycle") +
      metric(`$${number(live.realized_pnl_usdc)}`, "LIVE PnL", pnlClass(live.realized_pnl_usdc)) +
      metric(percent(live.pair_completion_rate), "LIVE çift dolum");
  };

  const renderActiveCycle = (data) => {
    const active = (data.dual40 || {}).active_cycle;
    const node = byId("active");
    if (node) node.textContent = active ? JSON.stringify(active, null, 2) : "Aktif cycle yok.";
  };

  const renderScan = (data) => {
    const scan = (data.dual40 || {}).scan || {};
    const transport = scan.transport || {};
    const metrics = byId("scanmetrics");
    if (metrics) {
      metrics.innerHTML =
        metric(transport.ok ? "CANLI" : "YOK", "Book transport", transport.ok ? "ok" : "bad") +
        metric(scan.active_markets ?? 0, "Aktif 5m market") +
        metric(scan.eligible_markets ?? 0, "Uygun market") +
        metric(scan.scope || "—", "Tarama scope") +
        metric(JSON.stringify(scan.reason_counts || {}), "Red nedenleri");
    }

    const candidates = byId("candidates");
    if (candidates) {
      candidates.innerHTML = (scan.candidates || []).map((candidate) => (
        `<tr>` +
        `<td>${escapeHtml(candidate.combo_key)}</td>` +
        `<td class="${candidate.eligible ? "ok" : "bad"}">${candidate.eligible ? "EVET" : "HAYIR"}</td>` +
        `<td>${escapeHtml(candidate.reason || "—")}</td>` +
        `<td>${number(candidate.score)}</td>` +
        `<td>${number(candidate.stable_for_sec, 1)}s</td>` +
        `<td>${number(candidate.tte_sec, 1)}s</td>` +
        `<td>${number(candidate.up_mid)}</td>` +
        `<td>${number(candidate.down_mid)}</td>` +
        `<td>${number(candidate.mid_range)}</td>` +
        `<td>${number(candidate.net_drift)}</td>` +
        `<td>${number(candidate.slope_per_sec, 4)}</td>` +
        `<td>${number(candidate.one_way_ratio)}</td>` +
        `<td>${number(candidate.queue_ahead_up_at_40, 1)} / ${number(candidate.queue_ahead_down_at_40, 1)}</td>` +
        `</tr>`
      )).join("");
    }
  };

  const renderCycles = (data) => {
    const node = byId("cycles");
    if (!node) return;
    const cycles = (data.dual40 || {}).cycles || [];
    node.innerHTML = cycles.map((cycle) => (
      `<tr>` +
      `<td>${escapeHtml(cycle.id)}</td>` +
      `<td>${escapeHtml(cycle.scope)}</td>` +
      `<td>${escapeHtml(cycle.combo_key)}</td>` +
      `<td>${escapeHtml(cycle.status)}</td>` +
      `<td>${escapeHtml(cycle.level_index)}</td>` +
      `<td>${number(cycle.target_shares, 1)}</td>` +
      `<td>${number(cycle.up_filled_shares)}</td>` +
      `<td>${number(cycle.down_filled_shares)}</td>` +
      `<td>${number(cycle.matched_shares)}</td>` +
      `<td>${escapeHtml(cycle.residual_side || "—")} ${number(cycle.residual_shares)}</td>` +
      `<td>${escapeHtml(cycle.official_result || "—")}</td>` +
      `<td class="${pnlClass(cycle.realized_pnl_usdc)}">${cycle.realized_pnl_usdc == null ? "—" : `$${number(cycle.realized_pnl_usdc)}`}</td>` +
      `<td>${cycle.loss_pool_after_usdc == null ? "—" : `$${number(cycle.loss_pool_after_usdc)}`}</td>` +
      `<td>${cycle.near_touch_up_41 ? "UP " : ""}${cycle.near_touch_down_41 ? "DN" : ""}</td>` +
      `<td>${escapeHtml(cycle.error_code || "—")}</td>` +
      `</tr>`
    )).join("");
  };

  const render = (data) => {
    renderStatus(data);
    renderPerformance(data);
    renderActiveCycle(data);
    renderScan(data);
    renderCycles(data);
    showState(`OK · ${new Date().toLocaleTimeString()}`, "mut ok");
  };

  const tick = async () => {
    if (requestInFlight) return;
    requestInFlight = true;
    try {
      const data = await fetchJson("/api/summary");
      render(data);
    } catch (error) {
      if (String(error && error.message) !== "AUTH_REQUIRED") {
        console.error("DUAL40 dashboard refresh failed", error);
        showState(`PANEL HATASI · ${error.message || error}`, "bad");
      }
    } finally {
      requestInFlight = false;
    }
  };

  const liveAction = async (path) => {
    const output = byId("liveout");
    try {
      const result = await fetchJson(path, {
        method: "POST",
        headers: { "X-P3-CSRF": CSRF },
      });
      if (output) output.textContent = JSON.stringify(result, null, 2);
    } catch (error) {
      const payload = error.payload || { ok: false, error: error.message || String(error) };
      if (output) output.textContent = JSON.stringify(payload, null, 2);
    }
    await tick();
  };

  const bind = () => {
    byId("probe-btn")?.addEventListener("click", () => liveAction("/api/live/probe"));
    byId("disarm-btn")?.addEventListener("click", () => liveAction("/api/live/disarm"));
    byId("arm-btn")?.addEventListener("click", () => {
      const accepted = window.confirm(
        "DUAL40 CANLI moda geçsin mi? Uygun ilk stabil markette iki gerçek 40¢ POST-ONLY GTC emir açılır."
      );
      if (accepted) liveAction("/api/live/arm");
    });
    byId("logout-btn")?.addEventListener("click", async () => {
      try {
        await fetchJson("/logout", {
          method: "POST",
          headers: { "X-P3-CSRF": CSRF },
        });
      } finally {
        window.location.assign("/login");
      }
    });

    tick();
    refreshHandle = window.setInterval(tick, Math.max(1000, REFRESH_MS));
  };

  window.addEventListener("error", (event) => {
    showState(`JAVASCRIPT HATASI · ${event.message || "unknown"}`, "bad");
  });
  window.addEventListener("unhandledrejection", (event) => {
    const reason = event.reason && event.reason.message
      ? event.reason.message
      : String(event.reason || "unknown");
    showState(`PROMISE HATASI · ${reason}`, "bad");
  });
  window.addEventListener("beforeunload", () => {
    if (refreshHandle !== null) window.clearInterval(refreshHandle);
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind, { once: true });
  } else {
    bind();
  }
})();
