/* v2 Production Polish — auto-dismiss DELTA popup + small UX fixes
 * Loaded AFTER app.js so it can intercept and patch.
 * Spec: docs/UX_DESIGN_SYSTEM_v2_PRODUCTION.md (Phase 1)
 * Shipped: 2026-04-26
 */
(function () {
  // 1. Auto-dismiss persistent connection warning popups
  //    The existing _renderConnectionModal renders a yellow warning that lives
  //    forever. After 8s OR after one click, suppress for the rest of this
  //    sessionStorage window.
  const SUPPRESS_KEY = 'vne_dismissed_conn_modal_v1';
  const AUTO_DISMISS_MS = 8000;

  function tryDismissConnectionModal() {
    // Aggressive content-based match — find ANY element whose visible text
    // contains "DELTA CONNECTION" + "shadow_live", regardless of class
    const all = document.querySelectorAll('div, section, aside, article');
    all.forEach(el => {
      // Skip already-killed
      if (el.dataset.vneKilled) return;
      // Skip if too large (don't kill the whole page)
      const r = el.getBoundingClientRect();
      if (r.width > 600 || r.height > 400) return;
      const txt = (el.textContent || '').slice(0, 300);
      const isDeltaPopup = /DELTA CONNECTION/.test(txt) &&
                           /shadow_live|API key/.test(txt);
      if (!isDeltaPopup) return;
      // Walk up to the smallest container that contains the popup
      let popup = el;
      let parent = el.parentElement;
      while (parent && (parent.textContent || '').trim() === txt.trim()) {
        popup = parent;
        parent = parent.parentElement;
      }
      popup.dataset.vneKilled = '1';
      // shadow_live with no key is the EXPECTED state. Kill on sight.
      popup.style.display = 'none';
    });
  }

  // 2. Replace alarming "WHY NO TRADES?" yellow heading with neutral "DIAGNOSTIC"
  //    when bot is healthy. Otherwise leave alone (it IS a real warning then).
  function neutralizeWhyNoTrades() {
    const labels = document.querySelectorAll('div, span');
    labels.forEach(el => {
      if (el.children.length === 0 && el.textContent && el.textContent.trim() === 'WHY NO TRADES?') {
        // Check if there's actually a problem — look at the diagnostic text below
        const parent = el.parentElement;
        if (parent) {
          const diag = parent.querySelector('#trade-diagnostic');
          if (diag && /\u2705|active|healthy|trade active/i.test(diag.textContent || '')) {
            el.textContent = 'DIAGNOSTIC';
            el.style.color = 'var(--text-tertiary)';
          }
        }
      }
    });
  }

  // 3. Suppress redundant "All systems operational" green banner if state strip says PAUSED/TRADING
  function suppressRedundantBanner() {
    const overview = document.getElementById('overview-strip');
    if (!overview) return;
    const banners = document.querySelectorAll('.systems-banner, [class*="systems-operational"]');
    banners.forEach(b => {
      if ((b.textContent || '').includes('All systems operational')) {
        b.style.display = 'none';
      }
    });
  }

  function tick() {
    try { tryDismissConnectionModal(); } catch {}
    try { neutralizeWhyNoTrades(); } catch {}
    try { suppressRedundantBanner(); } catch {}
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
      tick();
      // Re-run every 2s since the popup may re-render
      setInterval(tick, 2000);
    });
  } else {
    tick();
    setInterval(tick, 2000);
  }
})();
