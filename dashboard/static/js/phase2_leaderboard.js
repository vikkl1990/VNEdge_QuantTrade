/* Phase 2 Shadow-of-Shadow Leaderboard widget — 2026-04-27
 * Live A/B/C/D/E verdict for the 5 exit configs. Polls
 * /api/phase2/leaderboard every 60s. Renders a small table
 * sorted by net PnL with the winner badge.
 *
 * Mount: <section id="phase2-leaderboard"></section>
 *
 * Each config row shows:
 *   id | n_closed (n_open pending) | wins | WR | Net | Avg | PF | summary tooltip
 *
 * Winner = first row with n_closed >= 5 AND net > 0 (server logic).
 */
(function () {
  const PANEL_ID = 'phase2-leaderboard';
  const REFRESH_MS = 60000;
  let currentHours = 24;

  function escHtml(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, c => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[c]));
  }
  function fmt(v, decimals) {
    if (v == null || isNaN(v)) return '—';
    return Number(v).toFixed(decimals != null ? decimals : 2);
  }
  function fmtMoney(v) {
    if (v == null || isNaN(v)) return '—';
    const n = Number(v);
    const sign = n >= 0 ? '+' : '−';
    return sign + '$' + Math.abs(n).toFixed(2);
  }
  function netClass(v) {
    if (v == null) return '';
    if (v > 0.5) return 'p2-net--ok';
    if (v < -0.5) return 'p2-net--bad';
    return 'p2-net--neutral';
  }
  function pfClass(v) {
    if (v == null) return '';
    if (v >= 1.5) return 'p2-pf--ok';
    if (v >= 1.0) return 'p2-pf--neutral';
    return 'p2-pf--bad';
  }

  function render(d) {
    const el = document.getElementById(PANEL_ID);
    if (!el) return;
    const cfgs = d.configs || [];
    const winner = d.winner;
    const headerHtml = `
      <div class="p2-header">
        <span class="p2-title">Phase 2 — Shadow-of-Shadow A/B/C/D/E</span>
        <div class="p2-controls">
          <select id="p2-hours-sel" class="p2-sel">
            <option value="1"  ${currentHours==1?'selected':''}>1h</option>
            <option value="6"  ${currentHours==6?'selected':''}>6h</option>
            <option value="24" ${currentHours==24?'selected':''}>24h</option>
            <option value="72" ${currentHours==72?'selected':''}>72h</option>
          </select>
        </div>
      </div>
    `;
    if (!cfgs.length) {
      el.innerHTML = headerHtml + `
        <div class="p2-empty">
          No Phase 2 fan-out trades in the last ${currentHours}h.
          Verify <code>PHASE2_SOS_ENABLED=true</code> on cryptobot.service.
        </div>`;
      wireControls();
      return;
    }
    const rowsHtml = cfgs.map(c => {
      const isWinner = c.id === winner;
      const rowCls = isWinner ? 'p2-row p2-row--winner' : 'p2-row';
      const nText = c.n_closed + (c.n_open > 0 ? ` <span class="p2-pending">(+${c.n_open} open)</span>` : '');
      const wrText = c.wr_pct != null ? c.wr_pct.toFixed(0) + '%' : '—';
      const pfText = c.pf_inf ? '∞' : (c.pf != null ? c.pf.toFixed(2) : '—');
      return `<tr class="${rowCls}" title="${escHtml(c.summary || '')}">
        <td class="p2-id">${isWinner ? '🏆 ' : ''}<code>${escHtml(c.id)}</code></td>
        <td class="num">${nText}</td>
        <td class="num">${c.wins}</td>
        <td class="num">${wrText}</td>
        <td class="num ${netClass(c.net)}">${fmtMoney(c.net)}</td>
        <td class="num">${c.avg_pnl != null ? fmtMoney(c.avg_pnl) : '—'}</td>
        <td class="num ${pfClass(c.pf)}">${pfText}</td>
      </tr>`;
    }).join('');
    el.innerHTML = headerHtml + `
      <table class="p2-table">
        <thead><tr>
          <th>Config</th><th class="num">n closed</th>
          <th class="num">wins</th><th class="num">WR</th>
          <th class="num">Net</th><th class="num">Avg</th>
          <th class="num">PF</th>
        </tr></thead>
        <tbody>${rowsHtml}</tbody>
      </table>
      <div class="p2-footer">
        ${d.n_total_closed} closed · ${d.n_total_open} open across all configs · last ${currentHours}h
        ${winner ? `· 🏆 Leader: <strong>${escHtml(winner)}</strong>` : '· no winner yet (need n≥5 + positive net)'}
        <span class="p2-meta">Hover any row for the config summary.</span>
      </div>`;
    wireControls();
  }

  function wireControls() {
    const sel = document.getElementById('p2-hours-sel');
    if (sel) sel.addEventListener('change', e => {
      currentHours = parseInt(e.target.value, 10);
      refresh();
    });
  }

  async function refresh() {
    try {
      const r = await fetch(`/api/phase2/leaderboard?hours=${currentHours}`, { cache: 'no-store' });
      if (!r.ok) throw new Error('http ' + r.status);
      const d = await r.json();
      render(d);
    } catch (e) {
      console.warn('phase2-leaderboard fetch failed:', e);
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
