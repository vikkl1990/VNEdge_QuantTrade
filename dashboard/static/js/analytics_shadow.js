/* Analytics SHADOW/PAPER toggle — 2026-04-27
 *
 * Adds a small pill toggle at the top of the Analytics tab. The full
 * analytics view (equity curve, Sharpe, MaxDD, daily P&L, key metric
 * cards) was previously paper-only because all data came from
 * /api/tracker/* endpoints. This module adds a parallel data path
 * for SHADOW execution data via /api/shadow/closed (clean=true) +
 * /api/quant-metrics?mode=shadow.
 *
 * Default mode: SHADOW. Click PAPER to see the legacy view.
 *
 * The shadow path:
 *   1. Hides the existing app.js paper-driven refreshAnalytics()
 *      from running on the 30s interval (we install our own ticker)
 *   2. Fetches /api/shadow/closed?days=30&limit=2000&clean=true
 *   3. Maps shadow trades into the shape app.js update fns expect
 *   4. Re-uses updateEquityChart, updateDailyPnlChart,
 *      updatePerScannerPerf, updateClosedTrades, updatePnlCalendar
 *      — they don't care about source as long as the trade objects
 *      have closed_at + pnl/pnl_pct/scanner/etc.
 *   5. Fetches /api/quant-metrics?mode=shadow for headline cards
 *      (sharpe, max_dd, expectancy, profit_factor, avg_win, avg_loss)
 */
(function () {
  let _mode = 'shadow';   // default — shadow is the canonical edge tracker
  let _windowDays = 30;   // 30d default — matches 'overall' the architect requested
  let _ticker = null;
  let _stripTicker = null;
  const REFRESH_MS = 30000;
  const STRIP_REFRESH_MS = 30000;

  function fmtMoney(n) {
    if (n == null || isNaN(n)) return '$--';
    const v = Number(n);
    return (v >= 0 ? '+$' : '-$') + Math.abs(v).toFixed(2);
  }

  // Strip: drive an-shadow-trades / pnl / wr / avg via /api/quant-metrics
  // (window-aware) instead of /api/real/status (24h hardcoded).
  async function refreshStrip() {
    try {
      const url = `/api/quant-metrics?mode=shadow&days=${_windowDays}&clean=true`;
      const r = await fetch(url, { cache: 'no-store' });
      if (!r.ok) return;
      const d = await r.json();
      const lbl = document.getElementById('an-shadow-window-lbl');
      if (lbl) lbl.textContent = _windowDays === 1 ? '24h' : (_windowDays + 'd');
      const t = document.getElementById('an-shadow-trades');
      const p = document.getElementById('an-shadow-pnl');
      const w = document.getElementById('an-shadow-wr');
      const a = document.getElementById('an-shadow-avg');
      if (t) t.textContent = String(d.n || 0);
      if (p) {
        p.textContent = fmtMoney(d.total_pnl);
        p.style.color = (d.total_pnl != null && d.total_pnl >= 0) ? '#22c55e' : '#ef4444';
      }
      if (w) w.textContent = (d.win_rate != null) ? Number(d.win_rate).toFixed(1) + '%' : '--%';
      if (a) {
        a.textContent = fmtMoney(d.expectancy);
        a.style.color = (d.expectancy != null && d.expectancy >= 0) ? '#22c55e' : '#ef4444';
      }
    } catch (e) { /* silent */ }
  }

  function wireStripSelector() {
    const sel = document.getElementById('an-shadow-window-sel');
    if (!sel || sel.dataset.wired === '1') return;
    sel.dataset.wired = '1';
    sel.addEventListener('change', e => {
      _windowDays = parseInt(e.target.value, 10) || 30;
      refreshStrip();
    });
  }

  function setActive(mode) {
    const sBtn = document.getElementById('an-mode-shadow');
    const pBtn = document.getElementById('an-mode-paper');
    if (!sBtn || !pBtn) return;
    if (mode === 'shadow') {
      sBtn.style.background  = 'rgba(167,139,250,.18)';
      sBtn.style.color       = '#a78bfa';
      sBtn.style.borderColor = 'rgba(167,139,250,.4)';
      pBtn.style.background  = 'transparent';
      pBtn.style.color       = 'var(--text-muted, #6b7794)';
      pBtn.style.borderColor = 'rgba(255,255,255,.08)';
      applyLabels(SHADOW_LABELS);  // swap labels to $ units
    } else {
      pBtn.style.background  = 'rgba(0,212,255,.10)';
      pBtn.style.color       = 'var(--cyan, #00d4ff)';
      pBtn.style.borderColor = 'rgba(0,212,255,.4)';
      sBtn.style.background  = 'transparent';
      sBtn.style.color       = 'var(--text-muted, #6b7794)';
      sBtn.style.borderColor = 'rgba(255,255,255,.08)';
      applyLabels(PAPER_LABELS);   // restore R-multiple labels for paper
    }
    const lbl = document.getElementById('an-mode-label');
    if (lbl) {
      if (mode === 'shadow') {
        lbl.style.background   = 'rgba(167,139,250,.05)';
        lbl.style.borderColor  = 'rgba(167,139,250,.15)';
        lbl.innerHTML = '<span style="color:#a78bfa;font-weight:600">🌓 SHADOW ANALYTICS</span> — Equity curve, metrics &amp; charts from real shadow execution (last 30d, clean filter applied)';
      } else {
        lbl.style.background   = 'rgba(0,212,255,.04)';
        lbl.style.borderColor  = 'rgba(0,212,255,.1)';
        lbl.innerHTML = '<span style="color:var(--cyan);font-weight:600">PAPER ANALYTICS</span> — Equity curve, metrics &amp; charts based on paper trades (1000 trades, $1K start) &nbsp;|&nbsp; Real performance shown in overview above';
      }
    }
  }

  function setText(id, val) {
    const el = document.getElementById(id);
    if (el) el.textContent = val;
  }
  function setColor(id, val, thresholds) {
    const el = document.getElementById(id);
    if (!el || val == null || isNaN(val)) return;
    const v = Number(val);
    let cls = '';
    if (thresholds) {
      if (v >= thresholds.ok)      cls = 'green';
      else if (v >= thresholds.mid) cls = 'yellow';
      else                          cls = 'red';
    }
    el.className = 'stat-value ' + cls;
  }

  // Map shadow trade (from /api/shadow/closed) to the shape paper-side
  // update fns expect. Shadow has dollar P&L not R-multiples or %, so
  // we plug pnl_usd into BOTH pnl AND pnl_pct (not /50 normalize, which
  // produces the broken sub-zero equity curve). The equity chart treats
  // pnl_pct additively, so cumulative pnl_usd works as the visualization.
  function shadowTradesToCommon(trades) {
    return (trades || []).map(t => {
      const pnl = Number(t.pnl_usd || 0);
      const meta = t.metadata || {};
      return {
        closed_at: t.closed_at,
        opened_at: t.opened_at,
        symbol: t.symbol,
        side: t.side,
        entry_price: t.entry_price,
        exit_price: t.exit_price,
        pnl: pnl,
        pnl_usd: pnl,
        // Shadow uses $ not %. Equity chart sums pnl_pct; passing $
        // here gives "Cumulative $ P&L" semantics. Drawdown will also
        // be in $ — clamp_drawdown logic below patches the chart.
        pnl_pct: pnl,
        scanner: t.scanner || meta.scanner || '',
        grade: t.grade || meta.grade || '',
        regime: t.regime || meta.regime || '',
        fees_usd: Number(t.fees_usd || 0),
        exit_reason: t.exit_reason || meta.exit_reason || '',
        metadata: meta,
      };
    });
  }

  // Re-render the equity chart in DOLLAR units (override paper-side
  // updateEquityChart for shadow mode). app.js's equityChart starts at
  // 100 + sums pnl_pct, normalized like paper. For shadow we want
  // CUMULATIVE $ P&L starting at 0 with a true $ drawdown.
  function updateShadowEquityChart(trades) {
    if (!Array.isArray(trades) || trades.length === 0) return;
    const sorted = [...trades].sort(
      (a, b) => new Date(a.closed_at || a.exit_time || 0) - new Date(b.closed_at || b.exit_time || 0)
    );
    let cum = 0, peak = 0;
    const labels = [], eqData = [], ddData = [];
    sorted.forEach(t => {
      cum += Number(t.pnl_usd || t.pnl || 0);
      if (cum > peak) peak = cum;
      const dd = cum - peak;  // dollars below running peak (≤ 0)
      const ts = t.closed_at || t.exit_time;
      let lbl = '';
      try { lbl = new Date(ts).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Kolkata' }); } catch (e) { lbl = String(ts || ''); }
      labels.push(lbl);
      eqData.push(Number(cum.toFixed(2)));
      ddData.push(Number(dd.toFixed(2)));
    });
    const canvas = document.getElementById('equity-chart');
    if (!canvas || typeof Chart === 'undefined') return;
    const ctx = canvas.getContext('2d');
    if (window.equityChart && typeof window.equityChart.destroy === 'function') {
      try { window.equityChart.destroy(); } catch (e) { /* nm */ }
    }
    window.equityChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [
          { label: 'Cumulative $ P&L', data: eqData,
            borderColor: '#a78bfa', backgroundColor: 'rgba(167,139,250,.08)',
            fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y' },
          { label: 'Drawdown $', data: ddData,
            borderColor: 'rgba(239,68,68,.55)', backgroundColor: 'rgba(239,68,68,.05)',
            fill: true, tension: 0.3, pointRadius: 0, borderWidth: 1, yAxisID: 'y1' },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { labels: { color: '#9ba3b5', font: { size: 11 } } },
          tooltip: {
            backgroundColor: 'rgba(22,26,37,.95)', borderColor: 'rgba(167,139,250,.2)', borderWidth: 1,
            titleColor: '#e8ecf4', bodyColor: '#e8ecf4',
            callbacks: { label: c => c.dataset.label + ': ' + (c.parsed.y >= 0 ? '+$' : '-$') + Math.abs(c.parsed.y).toFixed(2) },
          },
        },
        scales: {
          x: { display: false },
          y:  { position: 'left',  grid: { color: 'rgba(255,255,255,.03)' },
                ticks: { color: '#5f6a7d', font: { size: 10 }, callback: v => '$' + v.toFixed(0) } },
          y1: { position: 'right', grid: { display: false },
                ticks: { color: 'rgba(239,68,68,.4)', font: { size: 10 }, callback: v => '$' + v.toFixed(0) }, max: 0 },
        },
      },
    });
  }

  // Swap label text on the metric cards so they reflect $ vs R units.
  // Paper uses R-multiples; shadow uses $. We rewrite the .stat-label
  // text under each metric card. setActive() restores paper labels on
  // toggle back.
  const PAPER_LABELS = {
    'rm-expectancy': 'Expectancy (R)',
    'rm-avg-r':      'Avg R/Trade',
    'rm-avg-win':    'Avg Win (R)',
    'rm-avg-loss':   'Avg Loss (R)',
    'rk-sharpe':     'Sharpe Ratio',
    'rk-maxdd':      'Max Drawdown',
    'rm-payoff':     'Payoff Ratio',
    'rm-pf':         'Edge Ratio',
  };
  const SHADOW_LABELS = {
    'rm-expectancy': 'Expectancy ($)',
    'rm-avg-r':      'Avg $ / Trade',
    'rm-avg-win':    'Avg Win ($)',
    'rm-avg-loss':   'Avg Loss ($)',
    'rk-sharpe':     'Sharpe Ratio',
    'rk-maxdd':      'Max Drawdown ($)',
    'rm-payoff':     'Payoff Ratio',
    'rm-pf':         'Edge Ratio (PF)',
  };
  function applyLabels(map) {
    Object.keys(map).forEach(id => {
      const v = document.getElementById(id);
      if (!v) return;
      const lbl = v.parentElement && v.parentElement.querySelector('.stat-label');
      if (lbl) lbl.textContent = map[id];
    });
  }

  async function refreshShadow() {
    try {
      // Apply $-aware labels on every shadow refresh so a re-render
      // doesn't leave stale "(R)" text after toggling between modes.
      applyLabels(SHADOW_LABELS);

      const closedResp = await fetch('/api/shadow/closed?exchange=delta_india&days=30&limit=2000&clean=true', { cache: 'no-store' }).then(r => r.ok ? r.json() : null);
      const trades = shadowTradesToCommon((closedResp && closedResp.trades) || []);

      // Equity chart: use $-native renderer (overrides paper's % fn).
      try { updateShadowEquityChart(trades); } catch (e) { console.warn('shadow equity chart', e); }
      // Other panels still use paper-side renderers — they treat
      // pnl_pct/pnl interchangeably for daily-bar / scanner-perf.
      try { if (typeof window.updateDailyPnlChart === 'function')    window.updateDailyPnlChart(trades); } catch (e) { console.warn('shadow updateDailyPnlChart', e); }
      try { if (typeof window.updatePerScannerPerf === 'function')   window.updatePerScannerPerf(trades); } catch (e) { console.warn('shadow updatePerScannerPerf', e); }
      try { if (typeof window.updateScannerDiversity === 'function') window.updateScannerDiversity(trades); } catch (e) { console.warn('shadow updateScannerDiversity', e); }
      try { if (typeof window.updateClosedTrades === 'function')     window.updateClosedTrades(trades); } catch (e) { console.warn('shadow updateClosedTrades', e); }
      try { if (typeof window.updateFeeImpact === 'function')        window.updateFeeImpact(trades); } catch (e) { console.warn('shadow updateFeeImpact', e); }
      try { if (typeof window.updateMLAccuracy === 'function')       window.updateMLAccuracy(trades); } catch (e) { console.warn('shadow updateMLAccuracy', e); }

      // Headline metric cards from /api/quant-metrics?mode=shadow
      const qm = await fetch('/api/quant-metrics?mode=shadow&days=30&clean=true', { cache: 'no-store' }).then(r => r.ok ? r.json() : null);
      if (qm) {
        setText('rk-sharpe',     qm.sharpe     != null ? Number(qm.sharpe).toFixed(2)        : '--');
        setColor('rk-sharpe',    qm.sharpe,    { ok: 1.0, mid: 0.0 });
        setText('rk-maxdd',      qm.max_dd_usd != null ? '−$' + Number(qm.max_dd_usd).toFixed(2) : '--');
        setText('rm-expectancy', qm.expectancy != null ? '$' + Number(qm.expectancy).toFixed(3) : '--');
        setColor('rm-expectancy',qm.expectancy,{ ok: 0.0, mid: -0.5 });
        // rm-avg-r in shadow mode shows AVG $/TRADE (same source as
        // expectancy — both are mean(pnl_usd) — but kept on its own
        // card so the layout doesn't shift between paper and shadow).
        setText('rm-avg-r',      qm.expectancy != null ? '$' + Number(qm.expectancy).toFixed(3) : '--');
        setText('rm-pf',         qm.pf         != null ? Number(qm.pf).toFixed(2)            : '--');
        setColor('rm-pf',        qm.pf,        { ok: 1.5, mid: 1.0 });
        setText('rm-avg-win',    qm.avg_win    != null ? '$' + Number(qm.avg_win).toFixed(2) : '--');
        setText('rm-avg-loss',   qm.avg_loss   != null ? '−$' + Math.abs(Number(qm.avg_loss)).toFixed(2) : '--');
        if (qm.avg_win != null && qm.avg_loss != null && qm.avg_loss !== 0) {
          setText('rm-payoff',   (Number(qm.avg_win) / Math.abs(Number(qm.avg_loss))).toFixed(2));
        }
        // rm-total-r doesn't make sense for shadow — stamp the
        // total $ P&L into it instead so the card isn't blank/stale.
        setText('rm-total-r',    qm.total_pnl  != null ? '$' + Number(qm.total_pnl).toFixed(2) : '--');
        // Won't override an-balance / an-return / an-wr / an-pf —
        // those are paper-account-specific (1000-trade $1K simulator).
      }
    } catch (e) {
      console.warn('analytics_shadow refresh failed:', e);
    }
  }

  function startTicker() {
    if (_ticker) clearInterval(_ticker);
    if (_mode === 'shadow') {
      refreshShadow();
      _ticker = setInterval(refreshShadow, REFRESH_MS);
    } else {
      // Hand back to app.js paper-side refreshAnalytics
      _ticker = setInterval(() => {
        if (typeof window.refreshAnalytics === 'function') window.refreshAnalytics();
      }, REFRESH_MS);
      if (typeof window.refreshAnalytics === 'function') window.refreshAnalytics();
    }
  }

  function wire() {
    const sBtn = document.getElementById('an-mode-shadow');
    const pBtn = document.getElementById('an-mode-paper');
    if (!sBtn || !pBtn) return false;
    sBtn.addEventListener('click', () => {
      if (_mode === 'shadow') return;
      _mode = 'shadow'; setActive('shadow'); startTicker();
    });
    pBtn.addEventListener('click', () => {
      if (_mode === 'paper') return;
      _mode = 'paper'; setActive('paper'); startTicker();
    });
    return true;
  }

  function boot() {
    // Wait briefly for app.js to initialize its globals (updateEquityChart etc).
    let attempts = 0;
    const tryWire = () => {
      attempts++;
      if (wire()) {
        setActive('shadow');
        startTicker();
        wireStripSelector();
        refreshStrip();
        if (_stripTicker) clearInterval(_stripTicker);
        _stripTicker = setInterval(refreshStrip, STRIP_REFRESH_MS);
      } else if (attempts < 20) {
        setTimeout(tryWire, 250);
      }
    };
    tryWire();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
