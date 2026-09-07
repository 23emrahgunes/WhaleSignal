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
    const paperStates = states.PAPER || {};
    const liveStates = states.LIVE || {};
    const byAsset = dual.by_asset || {};
    const policy = dual.policy || {};
    const ladder = policy.ladder || dual.ladder || [];
    const assets = ["BTC", "ETH", "SOL", "XRP"];

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
        ? "<b>CANLI MOD ARM EDİLDİ.</b> LIVE paralellik varsayılan 1; uygun ilk stabil asset lane'inde iki gerçek 40¢ POST-ONLY GTC emir gönderilebilir."
        : "<b>DRY / PAPER.</b> BTC/ETH/SOL/XRP bağımsız 5 → 10 → 30 recovery lane'leri çalışır. 41¢ yalnız near-touch tanısıdır, fill kanıtı değildir.";
      if (dual.migration_review) {
        notice.innerHTML += " <b>LEGACY_GLOBAL_STATE_REVIEW_REQUIRED:</b> Eski global zarar havuzu hiçbir asset'e dağıtılmadı.";
      }
    }

    const status = byId("status");
    if (status) {
      const laneCards = assets.map((asset) => {
        const paper = ((byAsset[asset] || {}).state || {}).PAPER || paperStates[asset] || {};
        const liveAsset = liveStates[asset] || {};
        const paperLevel = paper.level_index == null
          ? "—"
          : `${ladder[paper.level_index] ?? "—"} share`;
        const active = paper.active_cycle || liveAsset.active_cycle;
        return metric(
          `${paperLevel} · borç $${number(paper.recovery_debt_usdc ?? paper.loss_pool_usdc)} · atlanan ${paper.markets_skipped ?? 0} · ${paper.hard_stopped ? "HARD" : "AÇIK"} · ${active ? `#${active.id}` : "boş"}`,
          `${asset} lane`,
          paper.hard_stopped ? "bad" : Number(paper.loss_pool_usdc || 0) > 0 ? "warn" : "ok"
        );
      }).join("");
      status.innerHTML =
        metric(data.strategy_mode, "Strateji") +
        metric(mode, "Çalışma modu", mode === "LIVE_ARMED" ? "bad" : "ok") +
        metric(policy.price == null ? "—" : `${Math.round(Number(policy.price) * 100)}¢`, "İki taraf fiyatı") +
        metric(ladder.join(" → "), "Merdiven") +
        metric(policy.paper_max_concurrent_assets ?? "4", "PAPER paralellik") +
        metric(policy.live_max_concurrent_assets ?? "1", "LIVE paralellik") +
        metric(policy.opening_gate_mode || "—", "Opening gate") +
        metric(policy.forecast_gate_mode || "—", "Forecast gate") +
        metric(policy.global_risk_mode || "—", "Global risk") +
        laneCards +
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
      metric(`$${number(paper.ev_per_settled_usdc)}`, "Paper EV / settled", pnlClass(paper.ev_per_settled_usdc)) +
      metric(percent(paper.pair_completion_rate), "Paper çift dolum") +
      metric(percent(paper.single_leg_rate), "Paper tek bacak") +
      metric(paper.recovery_cycles ?? 0, "Recovery cycle") +
      metric(`${number(paper.average_recovery_duration_sec, 1)}s`, "Recovery ort. süre") +
      metric(`$${number(paper.max_drawdown_usdc)}`, "Paper max DD", Number(paper.max_drawdown_usdc || 0) > 0 ? "warn" : "ok") +
      metric(live.cycles ?? 0, "LIVE cycle") +
      metric(`$${number(live.realized_pnl_usdc)}`, "LIVE PnL", pnlClass(live.realized_pnl_usdc)) +
      metric(percent(live.pair_completion_rate), "LIVE çift dolum");
  };

  const renderActiveCycle = (data) => {
    const active = (data.dual40 || {}).active_cycles || [];
    const node = byId("active");
    if (node) node.textContent = active.length ? JSON.stringify(active, null, 2) : "Aktif cycle yok.";
  };

  const renderCohorts = (data) => {
    const node = byId("cohorts");
    if (!node) return;
    const cohorts = (data.dual40 || {}).observed_cohorts || {};
    const labels = {
      base: "Base",
      opening: "Opening",
      opening_forecast: "Opening + forecast",
      strict_recovery: "Strict recovery",
    };
    node.innerHTML = Object.entries(labels).map(([key, label]) => {
      const row = cohorts[key] || {};
      return metric(
        `${row.cycles ?? 0} cycle · EV $${number(row.ev_per_settled_usdc)} · DD $${number(row.max_drawdown_usdc)}`,
        `${label} gözlenen kohort`,
        pnlClass(row.ev_per_settled_usdc)
      );
    }).join("");
  };

  const renderP26 = (data) => {
    const audit = (data.dual40 || {}).p26_paper || {};
    const metrics = byId("p26metrics");
    if (metrics) {
      metrics.innerHTML =
        metric(audit.status || "—", "Readiness", audit.status === "NOT_READY" ? "warn" : "ok") +
        metric(audit.candidates ?? 0, "Aday") +
        metric(audit.would_open ?? 0, "Would open") +
        metric(audit.opened ?? 0, "Opened") +
        metric(JSON.stringify(audit.reason_counts || {}), "Red nedenleri");
    }
    const rows = byId("p26decisions");
    if (rows) {
      rows.innerHTML = (audit.recent || []).map((item) => (
        `<tr>` +
        `<td>${escapeHtml(item.combo_key || "—")}</td>` +
        `<td>${escapeHtml(item.stage || "—")}</td>` +
        `<td>${escapeHtml(item.decision || "—")}</td>` +
        `<td>${escapeHtml(item.reason || "—")}</td>` +
        `<td>${item.would_open ? "EVET" : "HAYIR"}</td>` +
        `<td>${item.observed_at_ms ? new Date(Number(item.observed_at_ms)).toLocaleTimeString() : "—"}</td>` +
        `</tr>`
      )).join("");
    }
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
        metric(scan.would_open_base ?? 0, "Base would-open") +
        metric(scan.would_open_opening ?? 0, "Opening would-open") +
        metric(scan.would_open_opening_forecast ?? 0, "Forecast would-open") +
        metric(JSON.stringify(scan.gate_modes || {}), "Gate modları") +
        metric(scan.scope || "—", "Tarama scope") +
        metric(JSON.stringify(scan.reason_counts || {}), "Red nedenleri");
    }

    const candidates = byId("candidates");
    if (candidates) {
      candidates.innerHTML = (scan.candidates || []).map((candidate) => {
        const opening = candidate.opening_gate || {};
        const forecast = candidate.forecast_gate || {};
        const modes = candidate.gate_modes || {};
        return (
        `<tr>` +
        `<td>${escapeHtml(candidate.combo_key)}</td>` +
        `<td class="${candidate.eligible ? "ok" : "bad"}">${candidate.eligible ? "EVET" : "HAYIR"}</td>` +
        `<td>${escapeHtml(candidate.reason || "—")}</td>` +
        `<td>${escapeHtml(candidate.target_shares ?? "—")}</td>` +
        `<td>${number(candidate.score)}</td>` +
        `<td>${number(candidate.stable_for_sec, 1)}s</td>` +
        `<td>${number(candidate.tte_sec, 1)}s</td>` +
        `<td>${number(candidate.up_mid)}</td>` +
        `<td>${number(candidate.down_mid)}</td>` +
        `<td>${number(candidate.mid_range)}</td>` +
        `<td>${number(candidate.net_drift)}</td>` +
        `<td>${escapeHtml(opening.reason || "—")}</td>` +
        `<td>${number(opening.mid_range)}</td>` +
        `<td>${number(opening.net_drift)}</td>` +
        `<td>${number(opening.one_way_ratio)}</td>` +
        `<td>${number(opening.queue_imbalance, 2)}</td>` +
        `<td>${number(opening.depth_balance_ratio, 2)}</td>` +
        `<td>${number(forecast.p_up_external, 3)} · ${escapeHtml(forecast.reason || "—")}</td>` +
        `<td>${escapeHtml(`${modes.opening || "—"}/${modes.forecast || "—"}/${modes.global_risk || "—"}`)}</td>` +
        `<td>${escapeHtml(candidate.lane_status || "—")}</td>` +
        `<td>${escapeHtml(candidate.decision || "—")}</td>` +
        `<td>${escapeHtml(candidate.active_cycle_id || "—")}</td>` +
        `</tr>`
        );
      }).join("");
    }
  };

  const renderDecisions = (data) => {
    const node = byId("decisions");
    if (!node) return;
    const decisions = (data.dual40 || {}).market_decisions || [];
    node.innerHTML = decisions.map((decision) => (
      (() => {
        const gate = decision.final_gate || {};
        const opening = gate.opening_gate || {};
        const forecast = gate.forecast_gate || {};
        return (
      `<tr>` +
      `<td>${escapeHtml(decision.asset)}</td>` +
      `<td>${escapeHtml(decision.condition_id)}</td>` +
      `<td>${escapeHtml(decision.decision)}</td>` +
      `<td>${escapeHtml(decision.reason || "—")}</td>` +
      `<td>${escapeHtml(opening.reason || "—")}</td>` +
      `<td>${number(forecast.p_up_external, 3)} · ${escapeHtml(forecast.reason || "—")}</td>` +
      `<td>${decision.updated_at_ms ? new Date(Number(decision.updated_at_ms)).toLocaleTimeString() : "—"}</td>` +
      `<td>${escapeHtml(decision.opened_cycle_id || "—")}</td>` +
      `</tr>`
        );
      })()
    )).join("");
  };

  const renderCycles = (data) => {
    const node = byId("cycles");
    if (!node) return;
    const cycles = (data.dual40 || {}).cycles || [];
    node.innerHTML = cycles.map((cycle) => (
      `<tr>` +
      `<td>${escapeHtml(cycle.id)}</td>` +
      `<td>${escapeHtml(cycle.scope)}</td>` +
      `<td>${escapeHtml(cycle.asset || "—")}</td>` +
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
    renderCohorts(data);
    renderP26(data);
    renderActiveCycle(data);
    renderScan(data);
    renderDecisions(data);
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
