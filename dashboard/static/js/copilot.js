/* Co-Pilot Panel — Decisions Queued For You (Sprint 1 MVP)
 * Reads from /api/copilot/queue, posts to /api/copilot/action.
 * Renders into #copilot-panel if present.
 * Spec: docs/COPILOT_PIVOT_v1.md
 * Shipped: 2026-04-26
 */
(function () {
  const PANEL_ID = 'copilot-panel';
  const REFRESH_MS = 8000;

  function escapeHtml(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, c => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[c]));
  }

  function fmtAge(iso) {
    if (!iso) return '';
    const t = new Date(iso).getTime();
    if (isNaN(t)) return '';
    const ms = Date.now() - t;
    if (ms < 60_000) return Math.floor(ms / 1000) + 's ago';
    if (ms < 3_600_000) return Math.floor(ms / 60_000) + 'm ago';
    if (ms < 86_400_000) return Math.floor(ms / 3_600_000) + 'h ago';
    return Math.floor(ms / 86_400_000) + 'd ago';
  }

  function iconFor(severity) {
    if (severity === 'critical' || severity === 'error') return '\u26A0';   // ⚠
    if (severity === 'warn') return '\u26A0';
    if (severity === 'ok') return '\u2713';                                  // ✓
    return '\u2139';                                                          // ℹ
  }

  function actionsFor(item) {
    const t = item.event_type || '';
    if (t === 'auto_revert') {
      return [
        { label: 'Investigate', cls: 'copilot-btn--primary', action: 'investigate' },
        { label: 'Approve',     cls: '',                     action: 'acknowledge' },
        { label: 'Override',    cls: 'copilot-btn--danger',  action: 'override'    },
      ];
    }
    if (t === 'cohort_drift') {
      return [
        { label: 'View',     cls: 'copilot-btn--primary', action: 'investigate' },
        { label: 'Snooze',   cls: 'copilot-btn--snooze',  action: 'snooze'      },
        { label: 'Ack',      cls: '',                     action: 'acknowledge' },
      ];
    }
    if (t === 'silent_failure') {
      return [
        { label: 'View',     cls: 'copilot-btn--primary', action: 'investigate' },
        { label: 'Ack',      cls: '',                     action: 'acknowledge' },
      ];
    }
    // default
    return [
      { label: 'Ack',    cls: '', action: 'acknowledge' },
      { label: 'Snooze', cls: 'copilot-btn--snooze', action: 'snooze' },
    ];
  }

  async function postAction(eventId, action) {
    try {
      const r = await fetch('/api/copilot/action', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ event_id: eventId, action }),
      });
      if (r.ok) {
        if (window._notifyOk) _notifyOk('Acknowledged: ' + action);
        refreshCopilot();
      } else {
        if (window._notifyError) _notifyError('Action failed: HTTP ' + r.status);
      }
    } catch (e) {
      if (window._notifyError) _notifyError('Action error: ' + e.message);
    }
  }

  function render(items) {
    const el = document.getElementById(PANEL_ID);
    if (!el) return;
    if (!items || items.length === 0) {
      el.innerHTML = `
        <div class="copilot-header">
          <span class="copilot-title">Decisions Queued For You</span>
          <span class="copilot-count">0 items</span>
        </div>
        <div class="copilot-empty">No decisions queued. The bot is operating cleanly.</div>`;
      return;
    }
    const cards = items.map((it, i) => {
      const sev = (it.severity || 'info').toLowerCase();
      const icon = iconFor(sev);
      const actions = actionsFor(it).map(a =>
        `<button class="copilot-btn ${a.cls}" data-evt="${it.id}" data-action="${a.action}">${escapeHtml(a.label)}</button>`
      ).join('');
      return `
        <div class="copilot-card">
          <div class="copilot-icon copilot-icon--${escapeHtml(sev)}">${icon}</div>
          <div class="copilot-body">
            <div class="copilot-body-title">${escapeHtml(it.title || '(no title)')}</div>
            <div class="copilot-body-meta">${escapeHtml(fmtAge(it.event_at))}${it.cohort ? ' · ' + escapeHtml(it.cohort) : ''}${it.detail ? ' — ' + escapeHtml(it.detail.slice(0, 140)) : ''}</div>
          </div>
          <div class="copilot-actions">${actions}</div>
        </div>`;
    }).join('');
    el.innerHTML = `
      <div class="copilot-header">
        <span class="copilot-title">Decisions Queued For You</span>
        <span class="copilot-count">${items.length} item${items.length === 1 ? '' : 's'}</span>
      </div>
      ${cards}`;
    // Wire button handlers
    el.querySelectorAll('button[data-evt]').forEach(btn => {
      btn.addEventListener('click', () => {
        postAction(parseInt(btn.dataset.evt, 10), btn.dataset.action);
      });
    });
  }

  async function refreshCopilot() {
    try {
      const r = await fetch('/api/copilot/queue', { cache: 'no-store' });
      if (!r.ok) throw new Error('http ' + r.status);
      const d = await r.json();
      render(d.items || []);
    } catch (e) {
      console.warn('copilot fetch failed:', e);
    }
  }

  function boot() {
    refreshCopilot();
    setInterval(refreshCopilot, REFRESH_MS);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
