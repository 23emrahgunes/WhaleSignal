(() => {
  const inverseURL = tf => `/api/paper/inverse/trades?limit=50&tf=${tf}`;
  const inverseStatsURL = tf => `/api/paper/inverse/stats?tf=${tf}`;

  function upgradeInverseABUI() {
    const body = document.getElementById('paperTradeBody');
    if (!body) return;
    const table = body.closest('table');
    const card = body.closest('.card');
    if (card) {
      const title = card.querySelector('h2');
      if (title) title.textContent = 'Kağıt İşlemler — Model A vs Ters Sinyal B';
      if (!card.querySelector('.inverse-ab-note')) {
        const note = document.createElement('div');
        note.className = 'banner inverse-ab-note';
        note.style.cssText = 'margin:-4px 0 12px;border-radius:7px;border:1px solid #80500a';
        note.innerHTML = '<b>TERS A/B SHADOW</b> — Model işlemi açıldığı anda aynı $ bütçe ile tam karşı yön, gerçek Polymarket CLOB VWAP + ücret/latency maliyetiyle ayrıca simüle edilir. Eski işlemlerde tarihsel karşı emir defteri bilinmediği için ters sütunlar — kalır.';
        const scroll = card.querySelector('.scroll');
        if (scroll) card.insertBefore(note, scroll);
      }
    }
    if (table) {
      const head = table.querySelector('thead tr');
      if (head) head.innerHTML = '<th>Giriş Saati</th><th>Piyasa</th><th>Model Yönü</th><th>Model Giriş</th><th>Maliyet</th><th>Güven</th><th>Durum</th><th>Sonuç</th><th>Model K/Z</th><th>Ters Yön</th><th>Ters Giriş</th><th>Ters K/Z</th><th>Ters − Model</th>';
    }

    const p = document.getElementById('paperPnl');
    const portfolio = p && p.closest('.card');
    if (portfolio) {
      const title = portfolio.querySelector('h2');
      if (title) title.textContent = 'Kağıt İşlem A/B — Model Sinyali vs Ters Sinyal';
      if (!document.getElementById('inversePaperPnl')) {
        const row = document.createElement('div');
        row.className = 'grid3';
        row.style.marginTop = '12px';
        row.innerHTML = `
          <div class="mini paperstat"><span>Ters Strateji Gerçekleşen K/Z</span><strong id="inversePaperPnl">$0.00</strong></div>
          <div class="mini paperstat"><span>Ters Strateji Kazanma Oranı</span><strong id="inversePaperWinRate">0.0%</strong></div>
          <div class="mini paperstat"><span>Ters − Model K/Z Farkı</span><strong id="inversePaperDelta">$0.00</strong></div>`;
        portfolio.appendChild(row);
      }
    }
  }

  function inverseCell(inv, field, formatter, fallback = '—') {
    if (!inv) return fallback;
    const v = inv[field];
    if (v === undefined || v === null || v === '') return fallback;
    return formatter ? formatter(v) : String(v);
  }

  const baseUpdatePaper = typeof updatePaper === 'function' ? updatePaper : null;
  if (!baseUpdatePaper) return;

  updatePaper = async function updatePaperWithInverseAB() {
    await baseUpdatePaper();
    upgradeInverseABUI();

    const tf = activeTf;
    const [originalStats, trades, inverseStats, inverses] = await Promise.all([
      getJSON('/api/paper/stats?tf=' + tf),
      getJSON('/api/paper/trades?limit=50&tf=' + tf),
      getJSON(inverseStatsURL(tf)),
      getJSON(inverseURL(tf))
    ]);

    const invPnl = Number(inverseStats.realizedPnl || 0);
    const modelPnl = Number(originalStats.realizedPnl || 0);
    const deltaTotal = invPnl - modelPnl;
    const invPnlEl = document.getElementById('inversePaperPnl');
    const invWinEl = document.getElementById('inversePaperWinRate');
    const invDeltaEl = document.getElementById('inversePaperDelta');
    if (invPnlEl) {
      invPnlEl.textContent = usd(invPnl);
      invPnlEl.className = signClass(invPnl);
    }
    if (invWinEl) invWinEl.textContent = pctDirect(inverseStats.winRate || 0, 1);
    if (invDeltaEl) {
      invDeltaEl.textContent = usd(deltaTotal);
      invDeltaEl.className = signClass(deltaTotal);
    }

    const body = document.getElementById('paperTradeBody');
    if (!body) return;
    if (!trades || !trades.length) {
      body.innerHTML = '<tr><td colspan="13">Henüz eşik koşullarını geçen kağıt işlem yok.</td></tr>';
      return;
    }

    const invByTrade = new Map((inverses || []).map(x => [Number(x.paperTradeId), x]));
    body.innerHTML = trades.map(t => {
      const inv = invByTrade.get(Number(t.id));
      const status = t.status === 'OPEN' ? chip('AÇIK', 'open') : t.won ? chip('KAZANDI', 'win') : chip('KAYBETTİ', 'loss');
      const modelSettled = t.status !== 'OPEN';
      const inverseSettled = !!inv && inv.status !== 'OPEN';
      const invSide = inv ? decisionChip(inv.side) : '—';
      const invEntry = inverseCell(inv, 'entryPrice', v => Number(v).toFixed(3));
      const invP = inverseSettled ? Number(inv.pnl || 0) : null;
      const modelP = modelSettled ? Number(t.pnl || 0) : null;
      const delta = invP !== null && modelP !== null ? invP - modelP : null;
      const invPnlHTML = invP === null ? '—' : `<span class="${signClass(invP)}">${usd(invP)}</span>`;
      const deltaHTML = delta === null ? '—' : `<span class="${signClass(delta)}">${usd(delta)}</span>`;
      return `<tr>
        <td>${timeOnly(t.entryTime)}</td>
        <td>${marketLabel(t.marketSlug)}</td>
        <td>${decisionChip(t.side)}</td>
        <td>${Number(t.entryPrice).toFixed(3)}</td>
        <td>${usd(t.stake)}</td>
        <td>${pctDirect(t.entryConfidence, 1)}</td>
        <td>${status}</td>
        <td>${t.outcome ? directionText(t.outcome) : '—'}</td>
        <td class="${modelP === null ? '' : signClass(modelP)}">${modelP === null ? '—' : usd(modelP)}</td>
        <td>${invSide}</td>
        <td>${invEntry}</td>
        <td>${invPnlHTML}</td>
        <td>${deltaHTML}</td>
      </tr>`;
    }).join('');
  };

  upgradeInverseABUI();
  updatePaper().catch(console.error);
})();
