/* Chief Quant Continuous Briefing widget — 2026-04-28
 *
 * Fetches /api/chief-quant/latest (markdown text) and renders it in a
 * scroll-friendly panel. Polls every 60s. Architect-facing surface for
 * "what's the bot doing right now and what should I do next".
 *
 * Mount: <section id="chief-quant-panel"></section>
 */
(function () {
  const PANEL_ID = 'chief-quant-panel';
  const REFRESH_MS = 60000;

  // Lightweight markdown → HTML (only headers, lists, tables, bold, code)
  function md2html(md) {
    let html = md;
    // escape
    html = html.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    // tables
    html = html.replace(/((?:^\|.+\|\s*$\n?){2,})/gm, function(block) {
      const rows = block.trim().split('\n');
      const header = rows[0].split('|').slice(1, -1).map(s => `<th>${s.trim()}</th>`).join('');
      // skip rows[1] = separator
      const body = rows.slice(2).map(r => {
        const cells = r.split('|').slice(1, -1).map(s => `<td>${s.trim()}</td>`).join('');
        return `<tr>${cells}</tr>`;
      }).join('');
      return `<table class="cq-table"><thead><tr>${header}</tr></thead><tbody>${body}</tbody></table>`;
    });
    // headers
    html = html.replace(/^#### (.+)$/gm, '<h4>$1</h4>');
    html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
    html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
    html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');
    // hr
    html = html.replace(/^---$/gm, '<hr>');
    // bold
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    // inline code
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    // unordered list
    html = html.replace(/(?:^- (.+)\n?)+/gm, function(block) {
      const items = block.trim().split('\n').map(line => {
        const m = line.match(/^- (.+)$/);
        return m ? `<li>${m[1]}</li>` : '';
      }).join('');
      return `<ul>${items}</ul>`;
    });
    // ordered list
    html = html.replace(/(?:^\d+\. (.+)\n?)+/gm, function(block) {
      const items = block.trim().split('\n').map(line => {
        const m = line.match(/^\d+\. (.+)$/);
        return m ? `<li>${m[1]}</li>` : '';
      }).join('');
      return `<ol>${items}</ol>`;
    });
    // paragraphs (simple)
    html = html.replace(/^([^<\s].+)$/gm, '<p>$1</p>');
    // collapse double newlines
    return html;
  }

  async function refresh() {
    const el = document.getElementById(PANEL_ID);
    if (!el) return;
    try {
      const r = await fetch('/api/chief-quant/latest', { cache: 'no-store' });
      if (!r.ok) {
        el.innerHTML = `<div class="cq-empty">Briefing fetch failed (HTTP ${r.status})</div>`;
        return;
      }
      const md = await r.text();
      el.innerHTML = `
        <div class="cq-header">
          <span class="cq-title">🧠 Chief Quant — Continuous Briefing</span>
          <span class="cq-meta">refreshes every 60s · updated ${new Date().toLocaleTimeString('en-IN', {timeZone:'Asia/Kolkata',hour12:false})} IST</span>
        </div>
        <div class="cq-body">${md2html(md)}</div>`;
    } catch (e) {
      el.innerHTML = `<div class="cq-empty">Error: ${String(e)}</div>`;
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
