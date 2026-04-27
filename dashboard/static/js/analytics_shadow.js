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
  let _ticker = null;
  const REFRESH_MS = 30000;

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
    } else {
      pBtn.style.background  = 'rgba(0,212,255,.10)';
      pBtn.style.color       = 'var(--cyan, #00d4ff)';
      pBtn.style.borderColor = 'rgba(0,212,255,.4)';
      sBtn.style.background  = 'transparent';
      sBtn.style.color       = 'var(--text-muted, #6b7794)';
      sBtn.style.borderColor = 'rgba(255,255,255,.08)';
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
  // update fns expect (pnl_pct + closed_at + scanner + grade + symbol +
  // metadata + fees_usd). $50 margin baseline for pnl_pct conversion —
  // close enough; charts only need a relative scale.
  function shadowTradesToCommon(trades) {
    const MARGIN_BASE = 50.0;  // typical shadow margin
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
        pnl_pct: (pnl / MARGIN_BASE) * 100,
        scanner: t.scanner || meta.scanner || '',
        grade: t.grade || meta.grade || '',
        regime: t.regime || meta.regime || '',
        fees_usd: Number(t.fees_usd || 0),
        exit_reason: t.exit_reason || meta.exit_reason || '',
        metadata: meta,
      };
    });
  }

  async function refreshShadow() {
    try {
      const closedResp = await fetch('/api/shadow/closed?exchange=delta_india&days=30&limit=2000&clean=true', { cache: 'no-store' }).then(r => r.ok ? r.json() : null);
      const trades = shadowTradesToCommon((closedResp && closedResp.trades) || []);

      // Re-use paper-side chart/table renderers — they accept any
      // trade list with closed_at + pnl_pct.
      try { if (typeof window.updateEquityChart === 'function')      window.updateEquityChart(trades); } catch (e) { console.warn('shadow updateEquityChart', e); }
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
        setText('rm-pf',         qm.pf         != null ? Number(qm.pf).toFixed(2)            : '--');
        setColor('rm-pf',        qm.pf,        { ok: 1.5, mid: 1.0 });
        setText('rm-avg-win',    qm.avg_win    != null ? '$' + Number(qm.avg_win).toFixed(2) : '--');
        setText('rm-avg-loss',   qm.avg_loss   != null ? '−$' + Math.abs(Number(qm.avg_loss)).toFixed(2) : '--');
        // Optional: payoff and WR
        if (qm.avg_win != null && qm.avg_loss != null && qm.avg_loss !== 0) {
          setText('rm-payoff',   (Number(qm.avg_win) / Math.abs(Number(qm.avg_loss))).toFixed(2));
        }
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
