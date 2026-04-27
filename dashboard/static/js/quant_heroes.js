/* Quant Heroes Panel — Phase 3 (replaces fitness-app dollar cards)
 * Renders Sharpe / Sortino / PF / MaxDD / Expectancy as a single 5-cell strip.
 * Reads /api/quant-metrics?days=30&mode=all
 * Spec: docs/UX_DESIGN_SYSTEM_v2_PRODUCTION.md (Phase 3)
 * Shipped: 2026-04-26
 */
(function () {
  const PANEL_ID = 'quant-heroes';
  const REFRESH_MS = 30000;
  // 2026-04-27 — default to SHADOW mode (was 'all'). Architect directive:
  // shadow is now the canonical edge tracker now that the max_age fix
  // ships, the legacy auto_responder_stuck_60m noise is filtered server-
  // side, and Phase 2 fan-out trades are excluded from the aggregate.
  let currentMode = 'shadow';
  let currentDays = 7;

  function fmt(v, decimals) {
    if (v == null || isNaN(v)) return '—';
    return Number(v).toFixed(decimals != null ? decimals : 2);
  }
  function fmtMoney(v) {
    if (v == null || isNaN(v)) return '—';
    const n = Number(v);
    if (Math.abs(n) >= 1000) return (n >= 0 ? '+' : '') + '$' + (n / 1000).toFixed(1) + 'k';
    return (n >= 0 ? '+' : '') + '$' + n.toFixed(2);
  }
  function escapeHtml(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, c => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[c]));
  }
  function sharpeClass(s) {
    if (s == null) return '';
    if (s >= 1.0) return 'qh-val--ok';
    if (s >= 0)   return 'qh-val--neutral';
    return 'qh-val--danger';
  }
  function pfClass(s) {
    if (s == null) return '';
    if (s >= 1.5) return 'qh-val--ok';
    if (s >= 1)   return 'qh-val--neutral';
    return 'qh-val--danger';
  }

  function render(d) {
    const el = document.getElementById(PANEL_ID);
    if (!el) return;
    if (!d || d.n === 0) {
      el.innerHTML = `
        <div class="qh-header">
          <span class="qh-title">Quant Edge — last ${currentDays}d / ${escapeHtml(currentMode)}</span>
          <div class="qh-controls">
            <select id="qh-mode-sel" class="qh-sel">
              <option value="all">all</option>
              <option value="paper">paper</option>
              <option value="real">real</option>
              <option value="shadow">shadow</option>
            </select>
            <select id="qh-days-sel" class="qh-sel">
              <option value="1">1d</option>
              <option value="7">7d</option>
              <option value="30" selected>30d</option>
              <option value="90">90d</option>
            </select>
          </div>
        </div>
        <div class="qh-empty">No closed trades in window. Switch mode/window or wait for activity.</div>`;
      wireControls();
      return;
    }
    el.innerHTML = `
      <div class="qh-header">
        <span class="qh-title">Quant Edge — last ${currentDays}d / ${escapeHtml(currentMode)}</span>
        <div class="qh-controls">
          <select id="qh-mode-sel" class="qh-sel">
            <option value="shadow"${currentMode==='shadow'?'selected':''}>shadow</option>
            <option value="real"  ${currentMode==='real'?'selected':''}>real</option>
            <option value="paper" ${currentMode==='paper'?'selected':''}>paper</option>
            <option value="all"   ${currentMode==='all'?'selected':''}>all</option>
          </select>
          <select id="qh-days-sel" class="qh-sel">
            <option value="1"  ${currentDays==1?'selected':''}>1d</option>
            <option value="7"  ${currentDays==7?'selected':''}>7d</option>
            <option value="30" ${currentDays==30?'selected':''}>30d</option>
            <option value="90" ${currentDays==90?'selected':''}>90d</option>
          </select>
        </div>
      </div>
      <div class="qh-grid">
        <div class="qh-cell">
          <span class="qh-label">Sharpe</span>
          <span class="qh-val ${sharpeClass(d.sharpe)}">${fmt(d.sharpe, 2)}</span>
          <span class="qh-meta">per-trade · n=${d.n}</span>
        </div>
        <div class="qh-cell">
          <span class="qh-label">Sortino</span>
          <span class="qh-val ${sharpeClass(d.sortino)}">${fmt(d.sortino, 2)}</span>
          <span class="qh-meta">downside risk only</span>
        </div>
        <div class="qh-cell">
          <span class="qh-label">Profit Factor</span>
          <span class="qh-val ${pfClass(d.pf)}">${fmt(d.pf, 2)}</span>
          <span class="qh-meta">gross_w / gross_l</span>
        </div>
        <div class="qh-cell">
          <span class="qh-label">Win Rate</span>
          <span class="qh-val">${d.win_rate != null ? d.win_rate.toFixed(0) + '%' : '—'}</span>
          <span class="qh-meta">avg win ${fmtMoney(d.avg_win)} · loss ${fmtMoney(d.avg_loss)}</span>
        </div>
        <div class="qh-cell">
          <span class="qh-label">Expectancy</span>
          <span class="qh-val ${d.expectancy != null && d.expectancy > 0 ? 'qh-val--ok' : 'qh-val--danger'}">${fmtMoney(d.expectancy)}</span>
          <span class="qh-meta">per trade</span>
        </div>
        <div class="qh-cell">
          <span class="qh-label">Max Drawdown</span>
          <span class="qh-val qh-val--danger">${d.max_dd_usd != null ? '−$' + d.max_dd_usd.toFixed(2) : '—'}</span>
          <span class="qh-meta">${d.max_dd_pct != null ? d.max_dd_pct.toFixed(1) + '% peak' : ''}</span>
        </div>
      </div>
      <div class="qh-footer">
        Total P&L: <strong>${fmtMoney(d.total_pnl)}</strong>
        · Best: <strong>${fmtMoney(d.best_trade)}</strong>
        · Worst: <strong>${fmtMoney(d.worst_trade)}</strong>
        ${d.clean && d.excluded_n > 0 ? `<span class="qh-excluded" title="Legacy auto_responder_stuck_60m closes (zero-PnL admin cleanup) and Phase 2 fan-out trades excluded. Toggle ?clean=false on the API for raw view.">· ${d.excluded_n} legacy rows hidden</span>` : ''}
      </div>`;
    wireControls();
  }

  function wireControls() {
    const m = document.getElementById('qh-mode-sel');
    const dd = document.getElementById('qh-days-sel');
    if (m) m.addEventListener('change', e => { currentMode = e.target.value; refresh(); });
    if (dd) dd.addEventListener('change', e => { currentDays = parseInt(e.target.value, 10); refresh(); });
  }

  async function refresh() {
    try {
      const url = `/api/quant-metrics?days=${currentDays}&mode=${currentMode}`;
      const r = await fetch(url, { cache: 'no-store' });
      if (!r.ok) throw new Error('http ' + r.status);
      const d = await r.json();
      render(d);
    } catch (e) {
      console.warn('quant-heroes fetch failed:', e);
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
