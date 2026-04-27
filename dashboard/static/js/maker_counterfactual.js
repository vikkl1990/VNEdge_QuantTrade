/* Maker counterfactual widget — 2026-04-27 Path A
 *
 * Surfaces the question "what would shadow PnL look like if patient
 * maker mode were filling at X%?" Reads /api/maker/counterfactual
 * which aggregates cf_maker_savings_* fields stamped onto every
 * shadow close by _close_shadow.
 *
 * Mount: <section id="maker-counterfactual-panel"></section>
 */
(function () {
  const PANEL_ID = 'maker-counterfactual-panel';
  const REFRESH_MS = 60000;
  let _windowDays = 1;

  function fmtMoney(v) {
    if (v == null || isNaN(v)) return '$--';
    const n = Number(v);
    return (n >= 0 ? '+$' : '-$') + Math.abs(n).toFixed(2);
  }
  function fmtPct(v) {
    if (v == null || isNaN(v)) return '—';
    return (v >= 0 ? '+' : '') + Number(v).toFixed(1) + '%';
  }
  function netClass(v) {
    if (v == null) return '';
    if (v > 0) return 'mc-net--ok';
    if (v < 0) return 'mc-net--bad';
    return '';
  }

  function render(d) {
    const el = document.getElementById(PANEL_ID);
    if (!el) return;
    if (!d || d.n === 0) {
      el.innerHTML = `
        <div class="mc-header">
          <span class="mc-title">🎯 Maker Counterfactual</span>
          ${selector()}
        </div>
        <div class="mc-empty">No shadow trades in last ${_windowDays}d window.</div>`;
      wire();
      return;
    }
    el.innerHTML = `
      <div class="mc-header">
        <span class="mc-title">🎯 Maker Counterfactual <span class="mc-sub">— "what if patient maker filled?"</span></span>
        ${selector()}
      </div>
      <div class="mc-grid">
        <div class="mc-cell">
          <div class="mc-label">Actual (100% taker)</div>
          <div class="mc-val ${netClass(d.actual_net)}">${fmtMoney(d.actual_net)}</div>
          <div class="mc-meta">today's reality · n=${d.n}</div>
        </div>
        <div class="mc-cell mc-cell--cf">
          <div class="mc-label">CF @ 50% maker</div>
          <div class="mc-val ${netClass(d.cf_50pct_net)}">${fmtMoney(d.cf_50pct_net)}</div>
          <div class="mc-meta">save ${fmtMoney(d.cf_50pct_savings)} · uplift ${fmtPct(d.uplift_50pct_pct)}</div>
        </div>
        <div class="mc-cell mc-cell--cf">
          <div class="mc-label">CF @ 100% maker</div>
          <div class="mc-val ${netClass(d.cf_100pct_net)}">${fmtMoney(d.cf_100pct_net)}</div>
          <div class="mc-meta">save ${fmtMoney(d.cf_100pct_savings)} · uplift ${fmtPct(d.uplift_100pct_pct)} · theoretical max</div>
        </div>
      </div>
      <div class="mc-footer">
        Maker rate (Delta India): 0.024% per side · Taker: 0.059% per side · Δ = 0.035% saved per filled side.
        <br>Calibrate against Path B real-pilot maker fill rate when that data lands.
      </div>`;
    wire();
  }

  function selector() {
    return `
      <select id="mc-window-sel" class="mc-sel">
        <option value="1"  ${_windowDays==1?'selected':''}>Last 1d</option>
        <option value="7"  ${_windowDays==7?'selected':''}>Last 7d</option>
        <option value="30" ${_windowDays==30?'selected':''}>Last 30d</option>
      </select>`;
  }

  function wire() {
    const sel = document.getElementById('mc-window-sel');
    if (sel && !sel.dataset.wired) {
      sel.dataset.wired = '1';
      sel.addEventListener('change', e => {
        _windowDays = parseInt(e.target.value, 10) || 1;
        refresh();
      });
    }
  }

  async function refresh() {
    try {
      const r = await fetch(`/api/maker/counterfactual?days=${_windowDays}&clean=true`, { cache: 'no-store' });
      if (!r.ok) throw new Error('http ' + r.status);
      const d = await r.json();
      render(d);
    } catch (e) {
      console.warn('maker-cf fetch failed:', e);
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
