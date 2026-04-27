/* Architect's Overview Strip — Agent 14 UX P0 #1 (B3 variant)
 * Calls /api/overview every 5s. Renders 6 cells:
 *   STATE | LAST | NEXT | BOOK | EDGE | MAKER (24h fill rate)
 * No-op if #overview-strip element is not in DOM.
 * Design: docs/UX_OVERVIEW_STRIP_v1.md
 * Shipped: 2026-04-26
 */
(function () {
  const STRIP_ID = 'overview-strip';
  const REFRESH_MS = 5000;
  const STALE_THRESHOLD_MS = 30000;
  let lastFetchOk = 0;

  function fmtMoneyShort(n) {
    if (n == null || isNaN(n)) return '—';
    const v = Number(n);
    if (Math.abs(v) >= 1000) return (v >= 0 ? '+' : '') + '$' + (v / 1000).toFixed(1) + 'k';
    return (v >= 0 ? '+' : '') + '$' + v.toFixed(2);
  }

  function fmtTimeAgo(iso) {
    if (!iso) return '—';
    const t = new Date(iso).getTime();
    if (isNaN(t)) return '—';            // defensive: bad ISO → don't show NaN
    const ms = Date.now() - t;
    if (ms < 0) return 'just now';
    if (ms < 60_000) return Math.floor(ms / 1000) + 's ago';
    if (ms < 3_600_000) return Math.floor(ms / 60_000) + 'm ago';
    if (ms < 86_400_000) return Math.floor(ms / 3_600_000) + 'h ago';
    return Math.floor(ms / 86_400_000) + 'd ago';
  }

  function escapeHtml(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, c => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[c]));
  }

  function makerClass(pct) {
    if (pct == null) return '';
    if (pct >= 30) return 'overview-maker--ok';
    if (pct >= 5)  return 'overview-maker--partial';
    return 'overview-maker--fail';
  }

  // Multi-exchange BOOK cache (refreshed every 8s, used by main render)
  let _bookCacheTs = 0;
  function _bookCacheRefresh() {
    if (Date.now() - _bookCacheTs < 6000) return;
    _bookCacheTs = Date.now();
    fetch('/api/multi-exchange/overview', { cache: 'no-store' })
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        if (!d || !d.active) return;
        window._mexBookCounts = {};
        for (const [bucket, arr] of Object.entries(d.active)) {
          window._mexBookCounts[bucket] = (arr || []).length;
        }
      }).catch(() => {});
  }

  // Phase 2 secondary status row — restores info from killed legacy strips.
  // Single line: KILL SWITCH | ACTIVITY 24h | ALERTS
  function renderSecondary(s) {
    if (!s || (!s.kill_switch && !s.activity && s.alerts_unacked_24h == null)) return '';
    const ks = s.kill_switch || {};
    const act = s.activity || {};
    const ksClass = ks.engaged ? 'sec-val--danger' : 'sec-val--ok';
    const ksText = ks.engaged ? 'ENGAGED' : 'CLEAR';
    const ksMeta = ks.engaged && ks.reason
      ? ` (${escapeHtml(String(ks.reason).slice(0, 40))}${ks.engaged_at ? ' · ' + fmtTimeAgo(ks.engaged_at) : ''})`
      : '';
    const opens24 = act.opens_24h ?? 0;
    const closes24 = act.closes_24h ?? 0;
    const opens1h = act.opens_1h ?? 0;
    const opensClass = opens24 === 0 ? 'sec-val--warn' : 'sec-val';
    const alertN = s.alerts_unacked_24h ?? 0;
    const alertClass = alertN > 0 ? 'sec-val--warn' : 'sec-val--ok';

    return `<div class="overview-secondary">
        <span class="sec-item">
          <span class="sec-label">KILL SWITCH</span>
          <span class="${ksClass}">${ksText}</span>${ksMeta ? `<span class="sec-divider">${ksMeta}</span>` : ''}
        </span>
        <span class="sec-divider">|</span>
        <span class="sec-item">
          <span class="sec-label">ACTIVITY 24h</span>
          <span class="${opensClass}">${opens24} opens</span>
          <span class="sec-divider">/</span>
          <span class="sec-val">${closes24} closes</span>
          <span class="sec-divider">·</span>
          <span class="sec-val">${opens1h} in last 1h</span>
        </span>
        <span class="sec-divider">|</span>
        <span class="sec-item">
          <span class="sec-label">ALERTS 24h</span>
          <span class="${alertClass}">${alertN} unacked</span>
        </span>
      </div>`;
  }

  function render(data) {
    const el = document.getElementById(STRIP_ID);
    if (!el) return;
    const state = (data.state || 'unknown').toLowerCase();
    // 2026-04-27 — added 'shadow' state class so the strip can differentiate
    // shadow_live (active sim trading) from paused/halted (no activity).
    const stateClass = ['trading', 'shadow', 'paused', 'halted'].includes(state) ? state : 'paused';
    const last  = data.last  || {};
    const next  = data.next  || {};
    const book  = data.book  || {};
    const edge  = data.edge  || {};
    const maker = data.maker || {};
    const alerts = data.alerts || [];

    const stateLabel = (data.state || 'UNKNOWN').toUpperCase();
    const lastPnl   = last.pnl != null ? fmtMoneyShort(last.pnl) : '';
    const nextConf  = next.conf != null ? Math.round(next.conf * 100) + '%' : '';
    const wrText    = edge.wr_pct != null ? Number(edge.wr_pct).toFixed(0) + '% WR' : '—';
    const pfText    = edge.pf != null ? 'PF ' + Number(edge.pf).toFixed(2) : '';
    const pnl24Text = edge.pnl_24h != null ? fmtMoneyShort(edge.pnl_24h) : '';
    const makerPct  = maker.fill_rate_pct;
    const makerText = makerPct != null ? Number(makerPct).toFixed(0) + '% maker' : '—';
    const makerN    = maker.n != null && maker.n > 0 ? 'n=' + maker.n : '';

    // Empty-state markers — suppress meta lines that are just "0p / 0r / 0s · +$0.00"
    // 2026-04-26: per-exchange breakdown via the multi-exchange snapshot endpoint.
    // Async-fetched lazily; first render falls back to legacy paper/real/shadow keys.
    const bookOpen = (book.open || 0);
    let bookMeta = '';
    if (bookOpen > 0) {
      // Fetch the multi-exchange snapshot once per render cycle (cached in module)
      _bookCacheRefresh();
      if (window._mexBookCounts) {
        const c = window._mexBookCounts;
        const parts = [];
        if (c.paper)        parts.push(`${c.paper}p`);
        if (c.delta_shadow) parts.push(`${c.delta_shadow}Δs`);
        if (c.bybit_shadow) parts.push(`${c.bybit_shadow}βs`);
        if (c.bybit_demo)   parts.push(`${c.bybit_demo}βd`);
        if (c.delta_real || c.bybit_real) parts.push(`${(c.delta_real||0)+(c.bybit_real||0)}r`);
        bookMeta = parts.join(' / ') + ' · ' + escapeHtml(fmtMoneyShort(book.cap_deployed));
      } else {
        bookMeta = `${book.paper || 0}p / ${book.real || 0}r / ${book.shadow || 0}s · ${escapeHtml(fmtMoneyShort(book.cap_deployed))}`;
      }
    }
    const edgeMeta = (edge.pnl_24h != null || edge.pf != null)
      ? `${pfText}${pfText && pnl24Text ? ' · ' : ''}${pnl24Text}`
      : '';
    const lastMeta = last.symbol
      ? `${escapeHtml(fmtTimeAgo(last.closed_at))}${last.scanner ? ' · ' + escapeHtml(last.scanner) : ''}`
      : '';
    const nextLabel = next.symbol || '—';
    const nextMeta = next.eta_min != null ? 'ETA ~' + next.eta_min + 'm' : (next.symbol ? 'forming' : '');
    const makerMeta = (makerN || maker.last_at)
      ? `${makerN}${makerN && maker.last_at ? ' · ' : ''}${maker.last_at ? fmtTimeAgo(maker.last_at) : ''}`
      : '';

    el.innerHTML = `
      <div class="overview-cell">
        <span class="overview-label">State</span>
        <span class="overview-value overview-value--state-${stateClass}">
          <span class="overview-state-dot overview-state-dot--${stateClass}"></span>${escapeHtml(stateLabel)}
        </span>
        <span class="overview-meta" data-empty="${data.mode ? '0' : '1'}">${escapeHtml(data.mode || '')}</span>
      </div>
      <div class="overview-cell">
        <span class="overview-label">Last</span>
        <span class="overview-value">${escapeHtml(last.symbol || '—')}${last.side ? ' ' + escapeHtml(last.side) : ''}${lastPnl ? ' ' + escapeHtml(lastPnl) : ''}</span>
        <span class="overview-meta" data-empty="${lastMeta ? '0' : '1'}">${lastMeta}</span>
      </div>
      <div class="overview-cell">
        <span class="overview-label">Next</span>
        <span class="overview-value">${escapeHtml(nextLabel)}${nextConf ? ' ' + escapeHtml(nextConf) : ''}</span>
        <span class="overview-meta" data-empty="${nextMeta ? '0' : '1'}">${escapeHtml(nextMeta)}</span>
      </div>
      <div class="overview-cell">
        <span class="overview-label">Book</span>
        <span class="overview-value">${bookOpen} open</span>
        <span class="overview-meta" data-empty="${bookMeta ? '0' : '1'}">${bookMeta}</span>
      </div>
      <div class="overview-cell">
        <span class="overview-label">Edge (24h)</span>
        <span class="overview-value">${escapeHtml(wrText)}</span>
        <span class="overview-meta" data-empty="${edgeMeta ? '0' : '1'}">${edgeMeta}</span>
      </div>
      <div class="overview-cell overview-cell--clickable" id="overview-cell-maker" title="Click for per-symbol/per-user breakdown">
        <span class="overview-label">Maker (24h) <span style="opacity:.4">▾</span></span>
        <span class="overview-value ${makerClass(makerPct)}">${escapeHtml(makerText)}</span>
        <span class="overview-meta" data-empty="${makerMeta ? '0' : '1'}">${makerMeta}</span>
      </div>
      ${renderSecondary(data.secondary || {})}
      ${alerts.length ? '<div class="overview-alert-row">⚠ ' + alerts.map(a => escapeHtml(a.msg || a)).join(' · ') + '</div>' : ''}
    `;
    lastFetchOk = Date.now();
  }

  async function refreshOverview() {
    try {
      const r = await fetch('/api/overview', { cache: 'no-store' });
      if (!r.ok) throw new Error('http ' + r.status);
      const d = await r.json();
      render(d);
    } catch (e) {
      const el = document.getElementById(STRIP_ID);
      if (el && Date.now() - lastFetchOk > STALE_THRESHOLD_MS) {
        const stale = document.createElement('span');
        stale.className = 'overview-stale-badge';
        stale.textContent = 'STALE ' + Math.floor((Date.now() - lastFetchOk) / 1000) + 's';
        const stateCell = el.querySelector('.overview-cell .overview-value');
        if (stateCell && !el.querySelector('.overview-stale-badge')) {
          stateCell.appendChild(stale);
        }
      }
      console.warn('overview fetch failed:', e);
    }
  }

  // Batch B #7: maker drill-down modal
  function escAttr(s) { return escapeHtml(s); }

  async function openMakerDrillDown() {
    const existing = document.getElementById('maker-drill-modal');
    if (existing) existing.remove();
    const overlay = document.createElement('div');
    overlay.id = 'maker-drill-modal';
    overlay.innerHTML = `
      <style>
        #maker-drill-modal {
          position: fixed; inset: 0; z-index: 9998;
          background: rgba(5,12,28,0.78); backdrop-filter: blur(4px);
          display: flex; align-items: center; justify-content: center;
          font-family: var(--font-mono, monospace);
        }
        #maker-drill-modal .mdm-card {
          background: var(--bg-card, #0f1f3d); border: 1px solid var(--border, rgba(255,255,255,0.1));
          border-radius: 8px; padding: 18px 20px; min-width: 580px; max-width: 800px;
          max-height: 80vh; overflow-y: auto; color: var(--text-primary, #e6edf7);
          box-shadow: 0 8px 28px rgba(0,0,0,0.6);
        }
        #maker-drill-modal h3 { margin: 0 0 6px; font-size: 0.95rem; color: var(--text-primary); }
        #maker-drill-modal .mdm-sub { font-size: 0.65rem; color: var(--text-muted); margin-bottom: 12px; }
        #maker-drill-modal table { width: 100%; border-collapse: collapse; margin-bottom: 14px; font-size: 0.74rem; }
        #maker-drill-modal th { text-align: left; color: var(--text-muted); font-weight: 600; padding: 4px 6px; border-bottom: 1px solid var(--border); text-transform: uppercase; font-size: 0.6rem; letter-spacing: 1px; }
        #maker-drill-modal td { padding: 6px 6px; border-bottom: 1px solid rgba(255,255,255,0.04); }
        #maker-drill-modal td.num { text-align: right; font-variant-numeric: tabular-nums; }
        #maker-drill-modal .mdm-pct-ok { color: var(--green); }
        #maker-drill-modal .mdm-pct-partial { color: var(--yellow); }
        #maker-drill-modal .mdm-pct-fail { color: var(--red); }
        #maker-drill-modal .mdm-empty { color: var(--text-muted); font-style: italic; padding: 12px 0; }
        #maker-drill-modal .mdm-close { float: right; cursor: pointer; opacity: 0.6; font-size: 1.2rem; line-height: 1; }
        #maker-drill-modal .mdm-close:hover { opacity: 1; }
        #maker-drill-modal .mdm-controls { margin-bottom: 10px; font-size: 0.7rem; }
        #maker-drill-modal .mdm-controls select { background: var(--bg-card-soft, #1a2a44); color: var(--text-primary); border: 1px solid var(--border); padding: 2px 6px; border-radius: 3px; font-family: var(--font-mono); }
      </style>
      <div class="mdm-card" role="dialog" aria-modal="true" aria-labelledby="mdm-title">
        <span class="mdm-close" id="mdm-close" title="Close (Esc)">×</span>
        <h3 id="mdm-title">Maker Fill Rate — Drill Down</h3>
        <div class="mdm-sub">Real-mode entries only. NaNh ago = no real trades in window.</div>
        <div class="mdm-controls">
          Window: <select id="mdm-days">
            <option value="1">last 1 day</option>
            <option value="7">last 7 days</option>
            <option value="30">last 30 days</option>
          </select>
        </div>
        <div id="mdm-body"><span class="mdm-empty">Loading…</span></div>
      </div>
    `;
    document.body.appendChild(overlay);

    function pctClass(p) {
      if (p == null) return '';
      if (p >= 30) return 'mdm-pct-ok';
      if (p >= 5) return 'mdm-pct-partial';
      return 'mdm-pct-fail';
    }

    async function loadAndRender(days) {
      const body = document.getElementById('mdm-body');
      body.innerHTML = '<span class="mdm-empty">Loading…</span>';
      try {
        const r = await fetch('/api/maker-stats?days=' + encodeURIComponent(days), { cache: 'no-store' });
        if (!r.ok) throw new Error('http ' + r.status);
        const d = await r.json();
        const t = d.totals || {};
        const totalLine = (t.n != null && t.n > 0)
          ? `<div style="margin-bottom:10px;font-size:.78rem"><b>Total:</b> ${t.n} trades · ${t.makers}/${t.n} maker (<span class="${pctClass(t.maker_pct)}">${t.maker_pct == null ? '—' : t.maker_pct.toFixed(1) + '%'}</span>)</div>`
          : '<div style="margin-bottom:10px;font-size:.78rem"><b>Total:</b> 0 real trades in window</div>';
        const sym = (d.by_symbol || []);
        const usr = (d.by_user || []);
        const symTbl = sym.length
          ? `<table><thead><tr><th>Symbol</th><th class="num">N</th><th class="num">Maker</th><th class="num">Taker</th><th class="num">%</th><th>Last</th></tr></thead><tbody>${sym.map(r => `<tr><td>${escAttr(r.symbol)}</td><td class="num">${r.n}</td><td class="num">${r.makers}</td><td class="num">${r.takers}</td><td class="num ${pctClass(r.maker_pct)}">${r.maker_pct == null ? '—' : r.maker_pct.toFixed(1) + '%'}</td><td>${escAttr(fmtTimeAgo(r.last_at))}</td></tr>`).join('')}</tbody></table>`
          : '<div class="mdm-empty">No per-symbol data</div>';
        const usrTbl = usr.length
          ? `<table><thead><tr><th>User</th><th>Mode</th><th class="num">N</th><th class="num">Maker</th><th class="num">%</th><th>Last</th></tr></thead><tbody>${usr.map(r => `<tr><td>${escAttr(r.email)}</td><td>${escAttr(r.mode)}</td><td class="num">${r.n}</td><td class="num">${r.makers}</td><td class="num ${pctClass(r.maker_pct)}">${r.maker_pct == null ? '—' : r.maker_pct.toFixed(1) + '%'}</td><td>${escAttr(fmtTimeAgo(r.last_at))}</td></tr>`).join('')}</tbody></table>`
          : '<div class="mdm-empty">No per-user data</div>';
        body.innerHTML = totalLine + '<h4 style="margin:6px 0 4px;font-size:.7rem;text-transform:uppercase;letter-spacing:1.5px;color:var(--text-muted)">By Symbol</h4>' + symTbl + '<h4 style="margin:6px 0 4px;font-size:.7rem;text-transform:uppercase;letter-spacing:1.5px;color:var(--text-muted)">By User</h4>' + usrTbl;
      } catch (e) {
        body.innerHTML = '<div class="mdm-empty">Failed to load: ' + escapeHtml(String(e)) + '</div>';
      }
    }

    document.getElementById('mdm-days').addEventListener('change', e => loadAndRender(e.target.value));
    document.getElementById('mdm-close').addEventListener('click', () => overlay.remove());
    overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
    document.addEventListener('keydown', function escClose(ev) {
      if (ev.key === 'Escape') { overlay.remove(); document.removeEventListener('keydown', escClose); }
    });
    loadAndRender(1);
  }

  function wireDrillDown() {
    const cell = document.getElementById('overview-cell-maker');
    if (cell && !cell.dataset.wired) {
      cell.dataset.wired = '1';
      cell.style.cursor = 'pointer';
      cell.addEventListener('click', openMakerDrillDown);
    }
  }

  function boot() {
    refreshOverview();
    setInterval(refreshOverview, REFRESH_MS);
    // Wire drill-down on each render (cell is recreated each refresh)
    setInterval(wireDrillDown, 1000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
