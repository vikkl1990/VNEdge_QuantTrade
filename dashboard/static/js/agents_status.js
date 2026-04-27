/* Agents Status widget — 2026-04-27
 *
 * Live fire-time + last-output table for the 14-agent operational team
 * + Tier A/B workers + specialty crons. Polls /api/agents/status every
 * 60s. Renders into #agents-status-panel.
 *
 * Status legend:
 *   🟢 green   — last fire <= 1.5 × cadence (healthy)
 *   🟡 yellow  — last fire 1.5-3 × cadence  (overdue)
 *   🔴 red     — last fire >  3 × cadence    (FAILED)
 *   ⚪ neutral — event-driven, no schedule
 */
(function () {
  const PANEL_ID = 'agents-status-panel';
  const REFRESH_MS = 60000;

  function escHtml(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, c => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[c]));
  }
  function fmtAge(min) {
    if (min == null) return 'never';
    if (min < 1)    return Math.round(min * 60) + 's ago';
    if (min < 60)   return Math.round(min) + 'm ago';
    if (min < 1440) return (min / 60).toFixed(1) + 'h ago';
    return (min / 1440).toFixed(1) + 'd ago';
  }
  function fmtCadence(min) {
    if (!min || min <= 0) return 'event-driven';
    if (min < 60)   return `every ${min}m`;
    if (min < 1440) return `every ${min / 60}h`;
    if (min === 1440)  return 'daily';
    if (min === 10080) return 'weekly';
    if (min === 43200) return 'monthly';
    return `every ${(min / 1440).toFixed(0)}d`;
  }
  function statusEmoji(s) {
    return ({green: '🟢', yellow: '🟡', red: '🔴', neutral: '⚪'}[s] || '?');
  }
  function statusClass(s) {
    return 'agt-row--' + s;
  }

  function render(d) {
    const el = document.getElementById(PANEL_ID);
    if (!el) return;
    if (!d || !Array.isArray(d.agents) || d.agents.length === 0) {
      el.innerHTML = '<div class="agt-empty">No agent data available.</div>';
      return;
    }
    const sum = d.summary || {};
    const summaryHtml = `
      <div class="agt-summary">
        <span class="agt-pill agt-pill--green">${sum.green || 0} healthy</span>
        <span class="agt-pill agt-pill--yellow">${sum.yellow || 0} overdue</span>
        <span class="agt-pill agt-pill--red">${sum.red || 0} failed</span>
        <span class="agt-pill agt-pill--neutral">${sum.neutral || 0} event-driven</span>
      </div>`;

    const rows = d.agents.map(a => {
      const ageDisplay = a.last_mtime_s ? fmtAge(a.age_min) : 'never';
      const fileDisplay = a.last_file ? `<code>${escHtml(a.last_file)}</code>` : '<span class="agt-na">—</span>';
      return `<tr class="${statusClass(a.status)}">
        <td class="agt-status">${statusEmoji(a.status)}</td>
        <td class="agt-name">${escHtml(a.name)}</td>
        <td class="agt-tier">${escHtml(a.tier)}</td>
        <td class="agt-cadence">${escHtml(fmtCadence(a.cadence_min))}</td>
        <td class="agt-age">${ageDisplay}</td>
        <td class="agt-file">${fileDisplay}</td>
      </tr>`;
    }).join('');

    el.innerHTML = `
      <div class="agt-header">
        <span class="agt-title">🛰 Agents Status</span>
        ${summaryHtml}
      </div>
      <table class="agt-table">
        <thead><tr>
          <th></th><th>Agent</th><th>Tier</th><th>Cadence</th><th>Last fire</th><th>Last output</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <div class="agt-footer">
        ${d.agents.length} capabilities tracked · refreshed every 60s
        · status: 🟢 ≤1.5×cadence · 🟡 ≤3× · 🔴 >3× or missing · ⚪ event-driven
      </div>`;
  }

  async function refresh() {
    try {
      const r = await fetch('/api/agents/status', { cache: 'no-store' });
      if (!r.ok) throw new Error('http ' + r.status);
      const d = await r.json();
      render(d);
    } catch (e) {
      console.warn('agents-status fetch failed:', e);
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
