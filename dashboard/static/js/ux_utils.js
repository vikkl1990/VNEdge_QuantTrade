/* UX utilities — Agent 14 Batch A #19: replace blocking alert() with non-blocking toast.
 * Loaded BEFORE app.js so window._notify is available globally.
 * Shipped: 2026-04-26
 */
(function () {
  // Inject CSS once
  if (!document.getElementById('vne-toast-css')) {
    const style = document.createElement('style');
    style.id = 'vne-toast-css';
    style.textContent = `
      .vne-toast-container {
        position: fixed; top: 70px; right: 16px; z-index: 9999;
        display: flex; flex-direction: column; gap: 8px;
        max-width: 420px; pointer-events: none;
        font-family: var(--font-mono, monospace);
      }
      .vne-toast {
        pointer-events: auto;
        background: var(--bg-card, #0f1f3d);
        border: 1px solid var(--border, rgba(255,255,255,0.1));
        border-left: 3px solid var(--text-muted, #5a7090);
        color: var(--text-primary, #e6edf7);
        padding: 10px 14px;
        border-radius: 6px;
        box-shadow: 0 4px 16px rgba(0,0,0,0.45);
        font-size: 0.78rem;
        line-height: 1.4;
        opacity: 0;
        transform: translateX(20px);
        transition: opacity 200ms ease, transform 200ms ease;
        white-space: pre-wrap;
        word-break: break-word;
        cursor: pointer;
      }
      .vne-toast.is-shown { opacity: 1; transform: translateX(0); }
      .vne-toast--info  { border-left-color: var(--blue, #4cb8ff); }
      .vne-toast--ok    { border-left-color: var(--green, #00ff9d); }
      .vne-toast--warn  { border-left-color: var(--yellow, #ffd700); }
      .vne-toast--error { border-left-color: var(--red, #ff3b5c); }
      .vne-toast-close {
        float: right; margin-left: 12px; opacity: 0.5;
        font-size: 1rem; line-height: 1; cursor: pointer;
      }
      .vne-toast-close:hover { opacity: 1; }
    `;
    document.head.appendChild(style);
  }

  function ensureContainer() {
    let c = document.getElementById('vne-toast-root');
    if (!c) {
      c = document.createElement('div');
      c.id = 'vne-toast-root';
      c.className = 'vne-toast-container';
      c.setAttribute('role', 'status');
      c.setAttribute('aria-live', 'polite');
      document.body.appendChild(c);
    }
    return c;
  }

  /**
   * Non-blocking toast notification.
   * @param {string} msg - text
   * @param {string} type - 'info' | 'ok' | 'warn' | 'error' (default: 'warn')
   * @param {number} timeoutMs - auto-dismiss delay (default 6000ms; 0 = no auto)
   */
  window._notify = function (msg, type, timeoutMs) {
    type = type || 'warn';
    timeoutMs = timeoutMs == null ? 6000 : timeoutMs;
    const c = ensureContainer();
    const t = document.createElement('div');
    t.className = 'vne-toast vne-toast--' + type;
    const close = document.createElement('span');
    close.className = 'vne-toast-close';
    close.textContent = '×';
    close.title = 'dismiss';
    const body = document.createElement('span');
    body.textContent = String(msg);
    t.appendChild(close);
    t.appendChild(body);
    c.appendChild(t);
    requestAnimationFrame(() => t.classList.add('is-shown'));

    function dismiss() {
      t.classList.remove('is-shown');
      setTimeout(() => { if (t.parentNode) t.parentNode.removeChild(t); }, 250);
    }
    t.addEventListener('click', dismiss);
    close.addEventListener('click', e => { e.stopPropagation(); dismiss(); });
    if (timeoutMs > 0) setTimeout(dismiss, timeoutMs);
    return t;
  };

  // Convenience aliases
  window._notifyOk    = function (m) { return _notify(m, 'ok',   4000); };
  window._notifyWarn  = function (m) { return _notify(m, 'warn', 6000); };
  window._notifyError = function (m) { return _notify(m, 'error', 8000); };
  window._notifyInfo  = function (m) { return _notify(m, 'info', 5000); };
})();
