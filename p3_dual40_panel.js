(() => {
  "use strict";

  const REFRESH_MS = Number(
    document.documentElement.dataset.refreshMs || "3000"
  );
  const csrfMeta = document.querySelector('meta[name="p3-csrf"]');
  const CSRF = csrfMeta ? csrfMeta.content : "";
  const byId = (id) => document.getElementById(id);
  const stateNode = byId("state");
  const REQUEST_TIMEOUT_MS = 15000;
  let refreshHandle = null;
  let requestInFlight = false;
  let lastSuccessfulAt = null;
  let consecutiveFailures = 0;

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

  const timestampMs = (value) => {
    if (value === null || value === undefined || value === "") return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
  };

  const localTime = (value, includeSeconds = true) => {
    const parsed = timestampMs(value);
    if (parsed === null) return "—";
    return new Intl.DateTimeFormat("tr-TR", {
      hour: "2-digit",
      minute: "2-digit",
      ...(includeSeconds ? { second: "2-digit" } : {}),
      hour12: false,
    }).format(new Date(parsed));
  };

  const fullLocalDateTime = (value) => {
    const parsed = timestampMs(value);
    if (parsed === null) return "—";
    return new Intl.DateTimeFormat("tr-TR", {
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(new Date(parsed));
  };

  const marketIntervalMinutes = (comboKey) => {
    const match = String(comboKey || "").match(/:(\d+)m$/i);
    return match ? Number(match[1]) : 5;
  };

  const marketWindowLabel = (cycle) => {
    const asset = String(cycle.asset || cycle.combo_key || "—").split(":", 1)[0];
    const minutes = marketIntervalMinutes(cycle.combo_key);
    const endMs = timestampMs(cycle.market_end_ts_ms);
    if (endMs === null) return `${asset} ${minutes} dk`;
    const startMs = endMs - (minutes * 60 * 1000);
    return `${asset} ${minutes} dk · ${localTime(startMs, false)}–${localTime(endMs, false)}`;
  };

  const shortIdentifier = (value) => {
    const text = String(value || "");
    return text.length > 18 ? `${text.slice(0, 9)}…${text.slice(-6)}` : text || "—";
  };

  const pnlClass = (value) => Number(value || 0) >= 0 ? "ok" : "bad";

  const cycleStatusLabel = (value) => ({
    PAPER_RESTING: "Sanal emirler bekliyor",
    LIVE_RESTING: "Canlı emirler bekliyor",
    WAIT_RESOLUTION: "Market sonucu bekleniyor",
  })[String(value || "")] || String(value || "Aktif cycle");

  const REASON_LABELS = {
    PASS: "Kontroller geçti",
    MODEL_ARTIFACT_NOT_READY: "Model bekleniyor",
    ALPHA_ARTIFACT_NOT_READY: "Alpha profili bekleniyor",
    BOOK_PAIR_MISSING: "Emir defteri çifti eksik",
    BOOK_TRANSPORT_NOT_LIVE: "Book bağlantısı hazır değil",
    FORECAST_CARD_MISSING: "Tahmin kartı bulunamadı",
    FORECAST_MARKET_MISMATCH: "Tahmin marketle eşleşmedi",
    FORECAST_MISSING: "Tahmin verisi yok",
    FORECAST_NOT_READY: "Tahmin henüz hazır değil",
    FORECAST_STALE: "Tahmin verisi eski",
    FORECAST_NEUTRAL: "Tahmin nötr",
    REJECTED_STRONG_DIRECTIONAL_ALPHA: "Güçlü yönlü tahmin nedeniyle reddedildi",
    NOT_EVALUATED: "Henüz değerlendirilmedi",
  };

  const reasonLabel = (value) => {
    const code = String(value || "");
    return REASON_LABELS[code] || code.replaceAll("_", " ").toLocaleLowerCase("tr-TR") || "—";
  };

  const topCount = (counts) => Object.entries(counts || {})
    .sort((left, right) => Number(right[1] || 0) - Number(left[1] || 0))[0] || ["—", 0];

  const chip = (label, value, cssClass = "") => (
    `<span><b class="${escapeHtml(cssClass)}">${escapeHtml(value ?? "—")}</b> ` +
    `${escapeHtml(label)}</span>`
  );

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
    let timedOut = false;
    const timeout = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, REQUEST_TIMEOUT_MS);
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
    } catch (error) {
      if (timedOut || (error && error.name === "AbortError")) {
        const timeoutError = new Error("REQUEST_TIMEOUT");
        timeoutError.code = "REQUEST_TIMEOUT";
        throw timeoutError;
      }
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
  };

  const renderStatus = (data) => {
    const dual = data.dual40 || {};
    const live = data.live || {};
    const states = dual.state || {};
    const paperStates = states.PAPER || {};
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
        ? "<b>CANLI MOD ARM EDİLDİ.</b> Uygun ilk stabil lane gerçek 40¢ POST-ONLY GTC emir gönderebilir; LIVE paralellik 1'dir."
        : "<b>DRY / PAPER.</b> Açılan cycle'da UP ve DOWN için ayrı 40¢ sanal limit emir izlenir. 41¢ near-touch yalnız tanıdır; fill kanıtı değildir.";
      if (dual.migration_review) {
        notice.innerHTML += " <b>Legacy havuz incelemesi gerekli:</b> Eski global zarar hiçbir asset'e dağıtılmadı.";
      }
    }

    const status = byId("status");
    if (status) {
      const price = policy.price == null ? "—" : `${Math.round(Number(policy.price) * 100)}¢`;
      const gateModes = `${policy.opening_gate_mode || "—"} / ${policy.forecast_gate_mode || "—"}`;
      const dbChecking = data.db_integrity === "checking";
      const dbValue = data.db_integrity === "ok"
        ? "SAĞLAM"
        : dbChecking ? "KONTROL EDİLİYOR" : data.db_integrity;
      status.innerHTML =
        metric(mode, "Çalışma modu", mode === "LIVE_ARMED" ? "bad" : "ok") +
        metric(`${price} + ${price}`, "Emir çifti") +
        metric(ladder.join(" → "), "Recovery merdiveni") +
        metric(gateModes, "Opening / forecast") +
        metric(`${policy.paper_max_concurrent_assets ?? 4} / ${policy.live_max_concurrent_assets ?? 1}`, "PAPER / LIVE paralellik") +
        metric(dbValue, "Veritabanı", data.db_integrity === "ok" ? "ok" : dbChecking ? "warn" : "bad");
    }

    const lanes = byId("lanes");
    if (lanes) {
      lanes.innerHTML = assets.map((asset) => {
        const assetSummary = byAsset[asset] || {};
        const paper = (assetSummary.state || {}).PAPER || paperStates[asset] || {};
        const level = paper.level_index == null ? 0 : Number(paper.level_index);
        const target = ladder[level] ?? "—";
        const debt = Number(paper.recovery_debt_usdc ?? paper.loss_pool_usdc ?? 0);
        const active = paper.active_cycle;
        const hardStopped = Boolean(paper.hard_stopped);
        const laneClass = hardStopped ? "bad-lane" : debt > 0 ? "warn-lane" : "";
        const badge = hardStopped ? "HARD STOP" : debt > 0 ? "RECOVERY" : "HAZIR";
        const activeText = active
          ? `#${active.id} · ${marketWindowLabel(active)} · ` +
            `${Math.round(Number(active.maker_price ?? policy.price ?? 0.40) * 100)}¢ + ` +
            `${Math.round(Number(active.maker_price ?? policy.price ?? 0.40) * 100)}¢ · ` +
            `UP ${number(active.up_filled_shares, 1)}/${number(active.target_shares, 1)} · ` +
            `DN ${number(active.down_filled_shares, 1)}/${number(active.target_shares, 1)}`
          : "Aktif cycle yok";
        return (
          `<article class="lane ${laneClass}">` +
          `<div class="lane-head"><span class="lane-asset">${asset}</span><span class="lane-badge">${badge}</span></div>` +
          `<div class="lane-values">` +
          `<div class="lane-value"><b>${escapeHtml(target)}</b><span>Hedef share</span></div>` +
          `<div class="lane-value"><b class="${debt > 0 ? "warn" : "ok"}">$${number(debt)}</b><span>Recovery borcu</span></div>` +
          `<div class="lane-value"><b>${escapeHtml(paper.markets_skipped ?? 0)}</b><span>Atlanan market</span></div>` +
          `</div><div class="lane-foot" title="${escapeHtml(activeText)}">${escapeHtml(activeText)}</div>` +
          `</article>`
        );
      }).join("");
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
      metric(`$${number(paper.realized_pnl_usdc)}`, "Gerçekleşen PnL", pnlClass(paper.realized_pnl_usdc)) +
      metric(`${paper.wins ?? 0} / ${paper.losses ?? 0}`, `Kazanç / kayıp · ${paper.settled ?? 0} settled`) +
      metric(percent(paper.pair_completion_rate), "Çift bacak dolum") +
      metric(percent(paper.single_leg_rate), "Tek bacak riski", Number(paper.single_leg_rate || 0) > 0 ? "warn" : "ok") +
      metric(`$${number(paper.max_drawdown_usdc)}`, "Maksimum düşüş", Number(paper.max_drawdown_usdc || 0) > 0 ? "warn" : "ok") +
      metric(`${number(paper.average_recovery_duration_sec, 1)} sn`, `${paper.recovery_cycles ?? 0} recovery cycle`) +
      metric(`$${number(live.realized_pnl_usdc)}`, `LIVE PnL · ${live.cycles ?? 0} cycle`, pnlClass(live.realized_pnl_usdc));
  };

  const renderActiveCycle = (data) => {
    const active = (data.dual40 || {}).active_cycles || [];
    const node = byId("active");
    if (!node) return;
    if (!active.length) {
      node.innerHTML = '<div class="empty">Aktif cycle yok.</div>';
      return;
    }
    node.innerHTML = active.map((cycle) => {
      const target = number(cycle.target_shares, 1);
      const limit = `${Math.round(Number(cycle.maker_price ?? 0.40) * 100)}¢`;
      const rawStatus = String(cycle.status || "AKTİF");
      const orderKind = String(cycle.scope || "PAPER") === "LIVE" ? "Canlı" : "Sanal";
      const openedAtMs = timestampMs(cycle.orders_posted_at_ms) ?? timestampMs(cycle.created_at_ms);
      const conditionId = String(cycle.condition_id || "");
      return (
        `<div class="active-row">` +
        `<b>${escapeHtml(cycle.asset || cycle.combo_key || "—")}</b>` +
        `<div class="active-order">` +
        `<strong title="${escapeHtml(rawStatus)}">${escapeHtml(cycleStatusLabel(rawStatus))}</strong>` +
        `<span class="active-market" title="Condition: ${escapeHtml(conditionId)}">${escapeHtml(marketWindowLabel(cycle))}</span>` +
        `<span>${orderKind} emir çifti · UP ${target} @ ${limit} · DOWN ${target} @ ${limit}</span>` +
        `<span class="active-time" title="${escapeHtml(fullLocalDateTime(openedAtMs))}">` +
        `Emir açılışı ${escapeHtml(localTime(openedAtMs))} · Market bitişi ${escapeHtml(localTime(cycle.market_end_ts_ms))} · ` +
        `ID ${escapeHtml(shortIdentifier(conditionId))}</span>` +
        `</div>` +
        `<div class="active-fill">` +
        `<span><b>UP dolum</b> ${number(cycle.up_filled_shares, 1)} / ${target}</span>` +
        `<span><b>DOWN dolum</b> ${number(cycle.down_filled_shares, 1)} / ${target}</span>` +
        `</div>` +
        `<strong>#${escapeHtml(cycle.id || "—")}</strong>` +
        `</div>`
      );
    }).join("");
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
        `${row.cycles ?? 0} · EV $${number(row.ev_per_settled_usdc)}`,
        `${label} · DD $${number(row.max_drawdown_usdc)}`,
        pnlClass(row.ev_per_settled_usdc)
      );
    }).join("");
  };

  const renderP26 = (data) => {
    const audit = (data.dual40 || {}).p26_paper || {};
    const metrics = byId("p26metrics");
    if (metrics) {
      const [topReason, topReasonCount] = topCount(audit.reason_counts);
      metrics.innerHTML =
        metric(audit.status === "NOT_READY" ? "HAZIR DEĞİL" : audit.status || "—", "Model durumu", audit.status === "NOT_READY" ? "warn" : "ok") +
        metric(audit.candidates ?? 0, "İncelenen aday") +
        metric(audit.would_open ?? 0, "Would open") +
        metric(audit.opened ?? 0, "Açılan") +
        metric(reasonLabel(topReason), `${topReasonCount} ret`, topReason === "—" ? "" : "warn");
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
      const [topReason, topReasonCount] = topCount(scan.reason_counts);
      const modes = scan.gate_modes || {};
      metrics.innerHTML =
        chip("Book", transport.ok ? "CANLI" : "YOK", transport.ok ? "ok" : "bad") +
        chip("Aktif market", scan.active_markets ?? 0) +
        chip("Uygun", scan.eligible_markets ?? 0, Number(scan.eligible_markets || 0) > 0 ? "ok" : "") +
        chip("Base", scan.would_open_base ?? 0) +
        chip("Opening", scan.would_open_opening ?? 0) +
        chip("Forecast", scan.would_open_opening_forecast ?? 0) +
        chip("Gate", `${modes.opening || "—"}/${modes.forecast || "—"}`) +
        chip("En sık ret", `${reasonLabel(topReason)} (${topReasonCount})`, topReason === "—" ? "" : "warn");
    }

    const candidates = byId("candidates");
    if (candidates) {
      candidates.innerHTML = (scan.candidates || []).map((candidate) => {
        const opening = candidate.opening_gate || {};
        const forecast = candidate.forecast_gate || {};
        const stable = `${number(candidate.stable_for_sec, 1)} sn`;
        const openingText = opening.reason === "PASS"
          ? `Geçti · r ${number(opening.mid_range)}`
          : reasonLabel(opening.reason);
        const forecastText = forecast.p_up_external == null
          ? reasonLabel(forecast.reason)
          : `${number(forecast.p_up_external, 3)} · ${reasonLabel(forecast.reason)}`;
        return (
        `<tr>` +
        `<td title="Condition: ${escapeHtml(candidate.condition_id || "")}">` +
        `<span class="cell-main">${escapeHtml(marketWindowLabel(candidate))}</span>` +
        `<span class="cell-code">ID ${escapeHtml(shortIdentifier(candidate.condition_id))}</span></td>` +
        `<td class="${candidate.eligible ? "ok" : "bad"}">${candidate.eligible ? "UYGUN" : "BEKLE"}</td>` +
        `<td><span class="cell-main">${escapeHtml(reasonLabel(candidate.reason))}</span><span class="cell-code" title="${escapeHtml(candidate.reason || "")}">${escapeHtml(candidate.reason || "—")}</span></td>` +
        `<td class="mobile-optional">${escapeHtml(candidate.target_shares ?? "—")}</td>` +
        `<td class="mobile-optional">${number(candidate.tte_sec, 1)} sn</td>` +
        `<td class="price-pair">${number(candidate.up_mid)} / ${number(candidate.down_mid)}</td>` +
        `<td class="mobile-optional">${stable}</td>` +
        `<td class="desktop-optional" title="${escapeHtml(opening.reason || "")}">${escapeHtml(openingText)}</td>` +
        `<td class="desktop-optional" title="${escapeHtml(forecast.reason || "")}">${escapeHtml(forecastText)}</td>` +
        `<td class="mobile-optional">${escapeHtml(candidate.lane_status || "—")}</td>` +
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
    node.innerHTML = cycles.map((cycle) => {
      const openedAtMs = timestampMs(cycle.orders_posted_at_ms) ?? timestampMs(cycle.created_at_ms);
      const conditionId = String(cycle.condition_id || "");
      return (
      `<tr>` +
      `<td>${escapeHtml(cycle.id)}</td>` +
      `<td>${escapeHtml(cycle.scope)}</td>` +
      `<td>${escapeHtml(cycle.asset || "—")}</td>` +
      `<td title="Condition: ${escapeHtml(conditionId)}">` +
      `<span class="cell-main">${escapeHtml(marketWindowLabel(cycle))}</span>` +
      `<span class="cell-code">ID ${escapeHtml(shortIdentifier(conditionId))}</span></td>` +
      `<td title="${escapeHtml(fullLocalDateTime(openedAtMs))}">${escapeHtml(localTime(openedAtMs))}</td>` +
      `<td>${escapeHtml(cycle.status)}</td>` +
      `<td>${escapeHtml(cycle.level_index)}</td>` +
      `<td>${number(cycle.target_shares, 1)}</td>` +
      `<td>${Math.round(Number(cycle.maker_price ?? 0.40) * 100)}¢</td>` +
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
      );
    }).join("");
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
    lastSuccessfulAt = Date.now();
    consecutiveFailures = 0;
    showState(`Güncel · ${new Date().toLocaleTimeString()}`, "mut ok");
  };

  const tick = async () => {
    if (requestInFlight) return;
    requestInFlight = true;
    try {
      const data = await fetchJson("/api/summary");
      render(data);
    } catch (error) {
      if (String(error && error.message) !== "AUTH_REQUIRED") {
        consecutiveFailures += 1;
        if (error && error.code === "REQUEST_TIMEOUT") {
          const lastSuccess = lastSuccessfulAt === null
            ? "yeniden deneniyor"
            : `son başarılı ${new Date(lastSuccessfulAt).toLocaleTimeString()}`;
          console.warn("DUAL40 dashboard refresh delayed", {
            consecutiveFailures,
            lastSuccessfulAt,
          });
          showState(`VERİ GECİKİYOR · ${lastSuccess}`, "warn");
        } else {
          console.error("DUAL40 dashboard refresh failed", error);
          showState(`PANEL HATASI · ${error.message || error}`, "bad");
        }
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
