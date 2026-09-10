/* Trade Stream — paper trades on production Delta India data
 * Renders into #trade-stream-panel.
 * Uses /api/paper/closed.
 * 2026-04-26
 */
(function () {
  const PANEL_ID = 'trade-stream-panel';
  const REFRESH_MS = 15_000;
  // 2026-04-27 — defaults shifted to clean shadow tracking. The legacy
  // auto_responder_stuck_60m rows (zero-PnL admin cleanup pre-max_age fix)
  // and Phase 2 fan-out virtual trades are filtered server-side via
  // ?clean=true (default in /api/shadow/closed). User can flip type=all
  // or open the API directly with clean=false for raw audit data.
  let exchangeFilter = 'paper';
  let typeFilter = 'paper';
  let limitVal = 50;
  let daysVal = 1;

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

  function badgeFor(t) {
    const tt = (t.trade_type || 'paper').toLowerCase();
    const ex = (t.exchange || 'paper').toLowerCase();
    return `<span class="tt-badge tt-badge--${tt}">${tt}</span>`
         + `<span class="ex-badge ex-badge--${ex}">${ex.replace('delta_india','delta')}</span>`;
  }

  function pnlCell(v) {
    if (v == null || isNaN(v)) return '<td class="num">—</td>';
    const n = Number(v);
    const cls = n > 0 ? 'pnl-pos' : (n < 0 ? 'pnl-neg' : '');
    return `<td class="num ${cls}">${fmtMoney(n)}</td>`;
  }

  function renderRow(t) {
    const sym = escHtml(t.symbol || '?');
    const side = (t.side || '').toLowerCase();
    const sideHtml = side ? `<span class="side-tag side-tag--${side}">${side.toUpperCase()}</span>` : '—';
    const entry = t.entry_price != null ? Number(t.entry_price).toFixed(t.entry_price < 1 ? 5 : 2) : '—';
    const exit = t.exit_price != null ? Number(t.exit_price).toFixed(t.exit_price < 1 ? 5 : 2) : '—';
    return `<tr>
      <td>${badgeFor(t)}</td>
      <td>${sym}</td>
      <td>${sideHtml}</td>
      <td class="num">${entry}</td>
      <td class="num">${exit}</td>
      ${pnlCell(t.pnl_usd)}
      <td class="num" style="opacity:.7">${t.fees_usd != null ? '−$' + Math.abs(t.fees_usd).toFixed(3) : '—'}</td>
      <td>${escHtml(t.scanner || '—')}</td>
      <td>${escHtml(t.exit_reason || '—')}</td>
      <td style="opacity:.7">${escHtml(fmtAge(t.closed_at))}</td>
    </tr>`;
  }

  let lastExcludedN = 0;  // surfaced in the meta footer

  async function fetchTrades() {
    // Single mode: paper trades on production Delta data.
    const fetches = [
      fetch(`/api/paper/closed?days=${daysVal}&limit=${limitVal}`).then(r => r.ok ? r.json() : {trades: []}),
    ];
    const results = await Promise.all(fetches);
    let all = [];
    lastExcludedN = 0;
    for (const res of results) {
      all = all.concat(res.trades || []);
      lastExcludedN += Number(res.excluded_n || 0);
    }
    // Filter by type if needed
    if (typeFilter !== 'all') {
      all = all.filter(t => (t.trade_type || 'paper') === typeFilter);
    }
    // Sort by closed_at desc
    all.sort((a, b) => {
      const ta = new Date(a.closed_at || 0).getTime();
      const tb = new Date(b.closed_at || 0).getTime();
      return tb - ta;
    });
    return all.slice(0, limitVal);
  }

  function controls() {
    return `
      <div class="tx-stream-controls">
        Window
        <select id="tx-days" class="tx-stream-sel">
          <option value="1"  ${daysVal==1?'selected':''}>1d</option>
          <option value="7"  ${daysVal==7?'selected':''}>7d</option>
          <option value="30" ${daysVal==30?'selected':''}>30d</option>
        </select>
        Exchange
        <select id="tx-ex" class="tx-stream-sel">
          <option value="paper"        ${exchangeFilter==='paper'?'selected':''}>paper only</option>
        </select>
        Type
        <select id="tx-tt" class="tx-stream-sel">
          <option value="paper"  ${typeFilter==='paper'?'selected':''}>paper</option>
        </select>
        Limit
        <select id="tx-lim" class="tx-stream-sel">
          <option value="25"  ${limitVal==25?'selected':''}>25</option>
          <option value="50"  ${limitVal==50?'selected':''}>50</option>
          <option value="100" ${limitVal==100?'selected':''}>100</option>
          <option value="250" ${limitVal==250?'selected':''}>250</option>
        </select>
      </div>`;
  }

  async function render() {
    const el = document.getElementById(PANEL_ID);
    if (!el) return;
    el.innerHTML = `
      <div class="tx-stream-header">
        <span class="tx-stream-title">Trade Stream — paper trades (production data)</span>
        ${controls()}
      </div>
      <div id="tx-body"><div class="tx-stream-empty">Loading…</div></div>
      <div class="tx-stream-meta" id="tx-meta"></div>`;
    wireControls();
    try {
      const trades = await fetchTrades();
      const body = document.getElementById('tx-body');
      const meta = document.getElementById('tx-meta');
      if (!trades.length) {
        body.innerHTML = `<div class="tx-stream-empty">No closed trades match the filters (last ${daysVal}d).</div>`;
        meta.textContent = '';
        return;
      }
      const totalPnl = trades.reduce((a, t) => a + (Number(t.pnl_usd) || 0), 0);
      const wins = trades.filter(t => Number(t.pnl_usd) > 0).length;
      body.innerHTML = `
        <table class="tx-stream-table">
          <thead><tr>
            <th>Source</th><th>Symbol</th><th>Side</th>
            <th class="num">Entry</th><th class="num">Exit</th>
            <th class="num">PnL</th><th class="num">Fees</th>
            <th>Scanner</th><th>Exit reason</th><th>Age</th>
          </tr></thead>
          <tbody>${trades.map(renderRow).join('')}</tbody>
        </table>`;
      const excludedTxt = (typeFilter === 'shadow' && lastExcludedN > 0)
        ? ` · ${lastExcludedN} legacy stuck/virtual rows hidden`
        : '';
      meta.textContent = `${trades.length} trades · WR ${wins}/${trades.length} (${(wins/trades.length*100).toFixed(0)}%) · Total ${fmtMoney(totalPnl)}${excludedTxt}`;
    } catch (e) {
      document.getElementById('tx-body').innerHTML =
        `<div class="tx-stream-empty">Fetch error: ${escHtml(String(e))}</div>`;
    }
  }

  function wireControls() {
    const d = document.getElementById('tx-days');
    const e = document.getElementById('tx-ex');
    const t = document.getElementById('tx-tt');
    const l = document.getElementById('tx-lim');
    if (d) d.addEventListener('change', ev => { daysVal = parseInt(ev.target.value, 10); render(); });
    if (e) e.addEventListener('change', ev => { exchangeFilter = ev.target.value; render(); });
    if (t) t.addEventListener('change', ev => { typeFilter = ev.target.value; render(); });
    if (l) l.addEventListener('change', ev => { limitVal = parseInt(ev.target.value, 10); render(); });
  }

  function boot() {
    render();
    setInterval(render, REFRESH_MS);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
