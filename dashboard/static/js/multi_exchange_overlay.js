/* Multi-Exchange Overlay — non-destructively augments existing panels:
 *   A. Active Trades         → 4-col grid (paper / delta_shadow / bybit_shadow / bybit_demo)
 *   B. Recent Closed Trades  → adds exchange dropdown filter
 *   C. Last Paper/Real strip → 4-cell (extended)
 *   D. cmd-panel hero        → adds Bybit row
 *   E. Analytics Trade Hist  → 4-tab unified component (paper/Δshadow/βshadow/βdemo)
 *                              fed by /api/multi-exchange/closed?bucket=...
 *   F. Overview Strip BOOK   → per-exchange breakdown (handled in overview_strip.js render)
 *
 * Strategy: hide legacy DOM via CSS (display:none on existing IDs), inject new
 * containers in same parent, render from /api/multi-exchange/{overview,closed}.
 * 2026-04-26
 */
(function () {
  const REFRESH_MS = 7_000;

  function escHtml(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, c => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[c]));
  }
  function fmtMoney(v) {
    if (v == null || isNaN(v)) return '—';
    const n = Number(v);
    const sign = n >= 0 ? '+' : '−';
    const abs = Math.abs(n);
    if (abs >= 1000) return sign + '$' + (abs / 1000).toFixed(2) + 'k';
    return sign + '$' + abs.toFixed(2);
  }
  function fmtAge(iso) {
    if (!iso) return '—';
    const t = new Date(iso).getTime();
    if (isNaN(t)) return '—';
    const ms = Date.now() - t;
    if (ms < 60_000) return Math.floor(ms / 1000) + 's';
    if (ms < 3_600_000) return Math.floor(ms / 60_000) + 'm';
    if (ms < 86_400_000) return Math.floor(ms / 3_600_000) + 'h';
    return Math.floor(ms / 86_400_000) + 'd';
  }
  function pnlClass(n) {
    if (n == null || isNaN(n)) return '';
    if (n > 0) return 'pnl-pos';
    if (n < 0) return 'pnl-neg';
    return '';
  }

  // ─────────────────────────────────────────────────────────
  // (A) Active Trades 4-column grid + (D) Overview BOOK detail
  // ─────────────────────────────────────────────────────────
  const BUCKET_LABELS = {
    paper:        'PAPER',
    delta_shadow: 'DELTA · SHADOW',
    bybit_shadow: 'BYBIT · SHADOW',
    bybit_demo:   'BYBIT · DEMO',
  };
  const BUCKET_KEYS = ['paper', 'delta_shadow', 'bybit_shadow', 'bybit_demo'];

  function ensureActiveContainer() {
    // Hide legacy 2-col paper/real grid + inject our 4-col version inside the same panel
    const legacy = document.getElementById('active-trades-paper');
    if (!legacy) return null;
    const panelInner = legacy.parentElement.parentElement;  // outer 2-col grid
    if (!panelInner) return null;

    let mex = document.getElementById('mex-active-grid');
    if (!mex) {
      // Hide legacy inner grid by display:none on its style
      panelInner.style.display = 'none';
      mex = document.createElement('div');
      mex.id = 'mex-active-grid';
      mex.className = 'mex-active-grid';
      panelInner.parentElement.insertBefore(mex, panelInner.nextSibling);
    }
    return mex;
  }

  function fmtPriceA(p) {
    if (p == null || isNaN(p)) return '—';
    const n = +p;
    return n < 1 ? n.toFixed(4) : n.toFixed(2);
  }

  function renderActive(byBucket) {
    const el = ensureActiveContainer();
    if (!el) return;
    el.innerHTML = BUCKET_KEYS.map(k => {
      const trades = byBucket[k] || [];
      const rows = trades.length === 0
        ? `<div class="mex-empty">no open</div>`
        : trades.slice(0, 6).map(t => {
            const sideCls = (t.side || '').toLowerCase() === 'long' ? 'side-tag--long' : 'side-tag--short';
            const entryFmt = fmtPriceA(t.entry_price);
            const slFmt    = fmtPriceA(t.stop_loss);
            const tpFmt    = fmtPriceA(t.take_profit);
            const upnl     = t.unrealized_pnl;
            const upnlHtml = upnl != null
              ? `<span class="mex-row-pnl ${pnlClass(upnl)}">${fmtMoney(upnl)}</span>`
              : `<span class="mex-row-pnl mex-row-pnl-pending">—</span>`;
            const lev = t.leverage ? `${(+t.leverage).toFixed(0)}x` : '';
            return `
              <div class="mex-row">
                <span class="mex-row-sym">${escHtml(t.symbol)}</span>
                <span class="side-tag ${sideCls}">${(t.side||'').toUpperCase()}</span>
                ${upnlHtml}
                <span class="mex-row-meta">
                  E ${entryFmt} · SL <span class="mex-sl">${slFmt}</span> · TP <span class="mex-tp">${tpFmt}</span>
                </span>
                <span class="mex-row-meta-sub">
                  qty ${t.quantity ?? '—'}${lev ? ' · ' + lev : ''} · ${fmtAge(t.opened_at)} ago${t.scanner ? ' · ' + escHtml(t.scanner) : ''}
                </span>
              </div>`;
          }).join('');
      return `
        <div class="mex-active-col mex-active-col--${k}">
          <div class="mex-col-title">${BUCKET_LABELS[k]} <span class="mex-col-count">${trades.length}</span></div>
          ${rows}
        </div>`;
    }).join('');
  }

  // ─────────────────────────────────────────────────────────
  // (C) Last Paper/Real strip → 4-cell extended
  // ─────────────────────────────────────────────────────────
  function ensureLastStrip() {
    const legacyPaper = document.getElementById('cmd-last-trade');
    const legacyReal  = document.getElementById('cmd-last-real-trade');
    if (!legacyPaper || !legacyReal) return null;

    // Hide their parent flex-row card
    const card = legacyPaper.closest('.bg-card-soft, .glass-card');
    if (!card) return null;
    let mex = document.getElementById('mex-last-strip');
    if (!mex) {
      card.style.display = 'none';
      mex = document.createElement('div');
      mex.id = 'mex-last-strip';
      mex.className = 'mex-last-strip';
      card.parentElement.insertBefore(mex, card.nextSibling);
    }
    return mex;
  }

  function renderLast(lastByBucket) {
    const el = ensureLastStrip();
    if (!el) return;
    el.innerHTML = BUCKET_KEYS.map(k => {
      const t = lastByBucket[k];
      if (!t) {
        return `
          <div class="mex-last-cell mex-last-cell--${k}">
            <span class="mex-last-label">${BUCKET_LABELS[k]}</span>
            <span class="mex-last-val">—</span>
            <span class="mex-last-meta">no closed yet</span>
          </div>`;
      }
      const pnl = t.pnl_usd;
      return `
        <div class="mex-last-cell mex-last-cell--${k}">
          <span class="mex-last-label">${BUCKET_LABELS[k]}</span>
          <span class="mex-last-val ${pnlClass(pnl)}">
            ${escHtml(t.symbol||'?')} ${(t.side||'').toUpperCase()} ${pnl != null ? fmtMoney(pnl) : ''}
          </span>
          <span class="mex-last-meta">${fmtAge(t.closed_at)} ago · ${escHtml(t.exit_reason || '')}</span>
        </div>`;
    }).join('');
  }

  // ─────────────────────────────────────────────────────────
  // (F) cmd-panel hero — append a Bybit row
  // ─────────────────────────────────────────────────────────
  function ensureHeroBybitRow() {
    const cmdPanel = document.getElementById('cmd-panel');
    if (!cmdPanel) return null;
    let row = document.getElementById('mex-hero-row');
    if (!row) {
      row = document.createElement('div');
      row.id = 'mex-hero-row';
      row.className = 'mex-hero-row';
      cmdPanel.appendChild(row);
    }
    return row;
  }

  function renderHero(today) {
    const el = ensureHeroBybitRow();
    if (!el) return;
    // 4-cell unified hero: paper / delta_shadow / bybit_shadow / bybit_demo
    const buckets = [
      { k: 'paper',        label: 'PAPER (24H)' },
      { k: 'delta_shadow', label: 'DELTA SHADOW (24H)' },
      { k: 'bybit_shadow', label: 'BYBIT SHADOW (24H)' },
      { k: 'bybit_demo',   label: 'BYBIT DEMO (24H)' },
    ];
    el.innerHTML = buckets.map(({ k, label }) => {
      const d = today[k] || { n: 0, wr_pct: null, pnl_total: 0, fees: 0 };
      const arrow = d.pnl_total > 0 ? '▲' : (d.pnl_total < 0 ? '▼' : '');
      return `
        <div class="mex-hero-cell mex-hero-cell--${k}">
          <div class="mex-hero-label">${label}</div>
          <div class="mex-hero-val ${pnlClass(d.pnl_total)}">${arrow ? arrow + ' ' : ''}${fmtMoney(d.pnl_total)}</div>
          <div class="mex-hero-meta">
            n=${d.n} · WR ${d.wr_pct != null ? d.wr_pct.toFixed(0) + '%' : '—'} · fees ${fmtMoney(-Math.abs(d.fees||0))}
          </div>
        </div>`;
    }).join('');
  }

  // ─────────────────────────────────────────────────────────
  // (B) Recent Closed Trades — add exchange dropdown to the
  //     Recent Closed Trades panel header (next to PAPER/DEMO/LIVE/SHADOW
  //     tab buttons), NOT to any hero stat card.
  // ─────────────────────────────────────────────────────────
  function ensureRcExchangeFilter() {
    // Anchor on the Recent Closed Trades panel by walking up from #rc-tab-paper
    // (or #recent-closed-body if tabs are hidden).
    const rcAnchor = document.getElementById('rc-tab-paper')
                  || document.getElementById('recent-closed-body');
    if (!rcAnchor) return;
    const card = rcAnchor.closest('.glass-card');
    if (!card) return;

    // Clean up any wrong-placement instances that landed on hero cards
    document.querySelectorAll('.mex-rc-filter').forEach(node => {
      if (!card.contains(node)) node.remove();
    });
    if (card.querySelector('#mex-rc-filter')) return;

    // The tab button group is `.flex.gap-1` containing #rc-tab-paper
    const tabGroup = rcAnchor.parentElement;  // div.flex.gap-1
    if (!tabGroup) return;

    const filter = document.createElement('span');
    filter.id = 'mex-rc-filter';
    filter.className = 'mex-rc-filter';
    filter.innerHTML = `
      <span class="mex-rc-filter-lbl">Exchange</span>
      <select id="mex-rc-ex">
        <option value="all">all</option>
        <option value="delta_india">delta</option>
        <option value="bybit">bybit</option>
      </select>`;
    tabGroup.appendChild(filter);

    document.getElementById('mex-rc-ex')?.addEventListener('change', e => {
      window._mexRcExchange = e.target.value;
      // Fire a custom event so any panel renderer can react
      document.dispatchEvent(new CustomEvent('mex-rc-exchange-changed', { detail: e.target.value }));
      // Best-effort: trigger a re-render by toggling currently-active tab
      const activeTab = document.querySelector('[id^="rc-tab-"][style*="cyan"]')
                     || document.getElementById('rc-tab-paper');
      if (activeTab) activeTab.click();
    });
  }

  // ─────────────────────────────────────────────────────────
  // (E) Analytics Trade History — 4-tab unified component
  // ─────────────────────────────────────────────────────────
  let _activeHistTab = 'paper';   // selected bucket
  let _histDays = 7;
  let _histLimit = 100;

  function ensureAnalyticsHistory() {
    // Legacy 2-tab Trade History panel anchors:
    //   #tab-paper-closed, #tab-real-closed, #paper-closed-wrap, #real-closed-wrap
    const legacyPaper = document.getElementById('paper-closed-wrap');
    const legacyReal  = document.getElementById('real-closed-wrap');
    const legacyTabPaper = document.getElementById('tab-paper-closed');
    const legacyTabReal  = document.getElementById('tab-real-closed');
    if (!legacyPaper || !legacyReal || !legacyTabPaper) return null;

    let mex = document.getElementById('mex-hist-root');
    if (mex) return mex;

    // Hide legacy tabs + wraps
    legacyTabPaper.style.display = 'none';
    if (legacyTabReal) legacyTabReal.style.display = 'none';
    legacyPaper.style.display = 'none';
    legacyReal.style.display = 'none';

    // Find the parent glass-card to inject into
    const card = legacyPaper.closest('.glass-card');
    if (!card) return null;

    mex = document.createElement('div');
    mex.id = 'mex-hist-root';
    mex.className = 'mex-hist-root';
    mex.innerHTML = `
      <div class="mex-hist-tabs">
        ${BUCKET_KEYS.map(k => `
          <button class="mex-hist-tab mex-hist-tab--${k}" data-bucket="${k}">
            <span class="mex-hist-tab-label">${BUCKET_LABELS[k]}</span>
            <span class="mex-hist-tab-count" id="mex-hist-count-${k}">—</span>
          </button>`).join('')}
        <div class="mex-hist-controls">
          <label>days
            <select id="mex-hist-days">
              <option value="1">1</option>
              <option value="3">3</option>
              <option value="7" selected>7</option>
              <option value="14">14</option>
              <option value="30">30</option>
            </select>
          </label>
          <label>rows
            <select id="mex-hist-limit">
              <option value="50">50</option>
              <option value="100" selected>100</option>
              <option value="250">250</option>
              <option value="500">500</option>
            </select>
          </label>
          <button id="mex-hist-refresh" class="mex-hist-btn">↻</button>
        </div>
      </div>
      <div class="mex-hist-summary" id="mex-hist-summary"></div>
      <div class="mex-hist-tbl-wrap">
        <table class="mex-hist-tbl">
          <thead>
            <tr>
              <th>Closed</th><th>Symbol</th><th>Side</th><th>Scanner</th>
              <th class="num">Entry</th><th class="num">Exit</th>
              <th class="num">Qty</th><th class="num">PnL $</th>
              <th class="num">PnL %</th><th>Reason</th><th class="num">Fees</th>
            </tr>
          </thead>
          <tbody id="mex-hist-body">
            <tr><td colspan="11" class="mex-hist-empty">loading…</td></tr>
          </tbody>
        </table>
      </div>
    `;
    card.appendChild(mex);

    // Wire tab buttons
    mex.querySelectorAll('.mex-hist-tab').forEach(btn => {
      btn.addEventListener('click', () => {
        _activeHistTab = btn.dataset.bucket;
        mex.querySelectorAll('.mex-hist-tab').forEach(b =>
          b.classList.toggle('mex-hist-tab--active', b.dataset.bucket === _activeHistTab));
        loadHistory();
      });
    });
    // Wire controls
    document.getElementById('mex-hist-days')?.addEventListener('change', e => {
      _histDays = +e.target.value || 7;
      loadHistory();
    });
    document.getElementById('mex-hist-limit')?.addEventListener('change', e => {
      _histLimit = +e.target.value || 100;
      loadHistory();
    });
    document.getElementById('mex-hist-refresh')?.addEventListener('click', loadHistory);

    // Default tab styling
    mex.querySelector(`.mex-hist-tab[data-bucket="${_activeHistTab}"]`)
      ?.classList.add('mex-hist-tab--active');
    return mex;
  }

  function fmtPctChange(entry, exit, side) {
    if (entry == null || exit == null || isNaN(entry) || isNaN(exit) || +entry === 0) return '—';
    const pct = (+exit - +entry) / +entry * 100 * ((side || '').toLowerCase() === 'short' ? -1 : 1);
    return (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%';
  }
  function fmtPrice(p) {
    if (p == null || isNaN(p)) return '—';
    const n = +p;
    return n < 1 ? n.toFixed(4) : n.toFixed(2);
  }
  function fmtDateShort(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '—';
    const mo = String(d.getMonth() + 1).padStart(2, '0');
    const da = String(d.getDate()).padStart(2, '0');
    const hr = String(d.getHours()).padStart(2, '0');
    const mn = String(d.getMinutes()).padStart(2, '0');
    return `${mo}-${da} ${hr}:${mn}`;
  }

  async function loadHistory() {
    const root = ensureAnalyticsHistory();
    if (!root) return;
    const body = document.getElementById('mex-hist-body');
    const summary = document.getElementById('mex-hist-summary');
    if (body) body.innerHTML = `<tr><td colspan="11" class="mex-hist-empty">loading ${BUCKET_LABELS[_activeHistTab]}…</td></tr>`;
    if (summary) summary.textContent = '';
    try {
      const url = `/api/multi-exchange/closed?bucket=${encodeURIComponent(_activeHistTab)}` +
                  `&days=${_histDays}&limit=${_histLimit}`;
      const r = await fetch(url, { cache: 'no-store' });
      if (!r.ok) {
        if (body) body.innerHTML = `<tr><td colspan="11" class="mex-hist-empty">HTTP ${r.status}</td></tr>`;
        return;
      }
      const d = await r.json();
      const trades = d.trades || [];
      // Update count badge
      const cb = document.getElementById(`mex-hist-count-${_activeHistTab}`);
      if (cb) cb.textContent = String(d.n || 0);
      // Render summary
      if (summary && d.agg) {
        const pnl = d.agg.pnl_total ?? d.agg.total_pnl ?? d.agg.net_pnl ?? 0;
        const wr  = d.agg.win_rate ?? d.agg.wr ?? null;
        const pf  = d.agg.profit_factor ?? null;
        summary.innerHTML = `
          <span>n=<b>${d.n}</b></span>
          <span>net=<b class="${pnlClass(pnl)}">${fmtMoney(pnl)}</b></span>
          ${wr != null ? `<span>WR=<b>${(+wr).toFixed(1)}%</b></span>` : ''}
          ${pf != null ? `<span>PF=<b>${(+pf).toFixed(2)}</b></span>` : ''}
          <span class="mex-hist-summary-meta">last ${_histDays}d · top ${_histLimit}</span>`;
      }
      // Render rows
      if (!body) return;
      if (trades.length === 0) {
        body.innerHTML = `<tr><td colspan="11" class="mex-hist-empty">no trades in window</td></tr>`;
        return;
      }
      body.innerHTML = trades.map(t => {
        const sideCls = (t.side || '').toLowerCase() === 'long' ? 'side-tag--long' : 'side-tag--short';
        const pnl = t.pnl_usd ?? t.pnl ?? null;
        const fees = t.fees_usd ?? t.fees ?? 0;
        const reason = (t.exit_reason || (t.metadata && t.metadata.exit_reason) || '').toString();
        const scanner = t.scanner || (t.metadata && t.metadata.scanner) || '';
        return `
          <tr>
            <td class="mex-hist-date">${fmtDateShort(t.closed_at)}</td>
            <td class="mex-hist-sym">${escHtml(t.symbol || '')}</td>
            <td><span class="side-tag ${sideCls}">${(t.side || '').toUpperCase()}</span></td>
            <td class="mex-hist-scanner">${escHtml(scanner)}</td>
            <td class="num">${fmtPrice(t.entry_price)}</td>
            <td class="num">${fmtPrice(t.exit_price)}</td>
            <td class="num">${t.quantity ?? '—'}</td>
            <td class="num ${pnlClass(pnl)}">${pnl != null ? fmtMoney(pnl) : '—'}</td>
            <td class="num ${pnlClass(pnl)}">${fmtPctChange(t.entry_price, t.exit_price, t.side)}</td>
            <td class="mex-hist-reason">${escHtml(reason)}</td>
            <td class="num">${fees ? fmtMoney(-Math.abs(+fees)) : '—'}</td>
          </tr>`;
      }).join('');
    } catch (e) {
      console.warn('mex history load failed:', e);
      if (body) body.innerHTML = `<tr><td colspan="11" class="mex-hist-empty">error: ${escHtml(e.message || e)}</td></tr>`;
    }
  }

  // Update tab counts (lightweight) from overview data so all 4 tabs always show counts
  function updateHistTabCounts(today) {
    BUCKET_KEYS.forEach(k => {
      const cb = document.getElementById(`mex-hist-count-${k}`);
      if (cb) cb.textContent = today[k]?.n != null ? `${today[k].n}` : '—';
    });
  }

  // Hook into Analytics tab activation: only load when user opens the tab
  function wireAnalyticsTabHook() {
    const analyticsTab = document.querySelector('[data-arg="analytics"]');
    if (!analyticsTab || analyticsTab.dataset.mexHooked) return;
    analyticsTab.dataset.mexHooked = '1';
    analyticsTab.addEventListener('click', () => {
      // Defer until tab DOM is visible
      setTimeout(() => { ensureAnalyticsHistory(); loadHistory(); }, 60);
    });
  }

  // ─────────────────────────────────────────────────────────
  // Main refresh loop
  // ─────────────────────────────────────────────────────────
  async function refresh() {
    try {
      const r = await fetch('/api/multi-exchange/overview', { cache: 'no-store' });
      if (!r.ok) return;
      const d = await r.json();
      renderActive(d.active || {});
      renderLast(d.last || {});
      renderHero(d.today || {});
      ensureRcExchangeFilter();
      // Surface E hooks — counts updated from overview, table loaded on tab click
      updateHistTabCounts(d.today || {});
      wireAnalyticsTabHook();
      // If Analytics tab is currently visible, also keep table fresh
      const analyticsPanel = document.getElementById('tab-analytics');
      if (analyticsPanel && analyticsPanel.classList.contains('active')) {
        ensureAnalyticsHistory();
        // Refresh table at most every 4 cycles (~28s) to avoid hammering
        if (!window._mexHistTick) window._mexHistTick = 0;
        window._mexHistTick = (window._mexHistTick + 1) % 4;
        if (window._mexHistTick === 0) loadHistory();
      }
    } catch (e) {
      console.warn('multi-exchange overlay fetch failed:', e);
    }
  }

  function boot() {
    refresh();
    setInterval(refresh, REFRESH_MS);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
