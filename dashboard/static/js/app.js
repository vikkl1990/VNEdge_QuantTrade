/* ═══════════════════════════════════════════════════════════
   VN Edge Dashboard — Combined JavaScript
   Extracted from index.html monolith
   ═══════════════════════════════════════════════════════════ */

// ═══ BLOCK 1 (original lines 2611-7316) ═══
// ── AUTH ─────────────────────────────────────────────────
async function checkSession() {
    try {
        const r = await fetch("/api/session", {credentials: "same-origin"});
        if (r.ok) {
            const d = await r.json();
            document.getElementById("login-overlay").classList.add("hidden");
            document.getElementById("logout-btn").style.display = "";
            if (d.auth_enabled) {
                document.getElementById("session-info").textContent = d.user;
            }
            return true;
        }
    } catch (e) {}
    document.getElementById("login-overlay").classList.remove("hidden");
    return false;
}

async function handleLogin(e) {
    e.preventDefault();
    const email = document.getElementById("login-email").value;
    const pass = document.getElementById("login-pass").value;
    const errEl = document.getElementById("login-error");
    errEl.style.display = "none";
    try {
        const r = await fetch("/api/login", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            credentials: "same-origin",
            body: JSON.stringify({email: email, password: pass})
        });
        if (r.ok) {
            document.getElementById("login-overlay").classList.add("hidden");
            document.getElementById("logout-btn").style.display = "";
            document.getElementById("session-info").textContent = email;
            return false;
        }
        errEl.textContent = "Invalid credentials";
        errEl.style.display = "block";
    } catch (err) {
        errEl.textContent = "Connection failed";
        errEl.style.display = "block";
    }
    return false;
}

async function handleLogout() {
    await fetch("/api/logout", {method: "POST"});
    document.getElementById("login-overlay").classList.remove("hidden");
    document.getElementById("logout-btn").style.display = "none";
    document.getElementById("session-info").textContent = "";
}

// Check auth on page load
checkSession();

// ── REGISTER / LOGIN TOGGLE ──────────────────────────────
function switchToRegister() {
    document.getElementById("login-overlay").classList.add("hidden");
    document.getElementById("register-overlay").classList.remove("hidden");
}
function switchToLogin() {
    document.getElementById("register-overlay").classList.add("hidden");
    document.getElementById("login-overlay").classList.remove("hidden");
}

async function handleRegister(e) {
    e.preventDefault();
    const name = document.getElementById("reg-name").value;
    const email = document.getElementById("reg-email").value;
    const pass = document.getElementById("reg-pass").value;
    const passConfirm = document.getElementById("reg-pass-confirm").value;
    const errEl = document.getElementById("register-error");
    errEl.style.display = "none";
    if (pass !== passConfirm) {
        errEl.textContent = "Passwords do not match";
        errEl.style.display = "block";
        return false;
    }
    if (pass.length < 8) {
        errEl.textContent = "Password must be at least 8 characters";
        errEl.style.display = "block";
        return false;
    }
    try {
        const r = await fetch("/api/register", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            credentials: "same-origin",
            body: JSON.stringify({name: name, email: email, password: pass})
        });
        if (r.ok) {
            switchToLogin();
            document.getElementById("login-email").value = email;
            document.getElementById("login-error").textContent = "Account created! Please sign in.";
            document.getElementById("login-error").style.display = "block";
            document.getElementById("login-error").style.color = "var(--green)";
            return false;
        }
        const data = await r.json().catch(() => ({}));
        errEl.textContent = data.error || "Registration failed";
        errEl.style.display = "block";
    } catch (err) {
        errEl.textContent = "Connection failed";
        errEl.style.display = "block";
    }
    return false;
}

// ── PROFILE FUNCTIONS ────────────────────────────────────
async function loadProfile() {
    try {
        const r = await fetch("/api/user/profile", {credentials: "same-origin"});
        if (!r.ok) return;
        const p = await r.json();
        const el = id => document.getElementById(id);
        if (p.name) el("prof-name").value = p.name;
        if (p.email) el("prof-email").value = p.email;
        if (p.phone) el("prof-phone").value = p.phone;
        if (p.timezone) el("prof-timezone").value = p.timezone;
        if (p.address) {
            if (p.address.line1) el("prof-addr1").value = p.address.line1;
            if (p.address.line2) el("prof-addr2").value = p.address.line2;
            if (p.address.city) el("prof-city").value = p.address.city;
            if (p.address.state) el("prof-state").value = p.address.state;
            if (p.address.country) el("prof-country").value = p.address.country;
            if (p.address.postal) el("prof-postal").value = p.address.postal;
        }
        if (p.bot_mode) { el("prof-bot-mode").value = p.bot_mode; updateModeBtns(p.bot_mode); }
        if (p.leverage) { el("prof-leverage").value = p.leverage; el("prof-leverage-val").textContent = p.leverage; }
        if (p.risk_per_trade) el("prof-risk").value = p.risk_per_trade;
        if (p.max_daily_loss) el("prof-max-loss").value = p.max_daily_loss;
        if (p.max_positions) el("prof-max-pos").value = p.max_positions;
        if (p.trading_pairs && Array.isArray(p.trading_pairs)) {
            document.querySelectorAll("#prof-pairs input[type='checkbox']").forEach(cb => {
                cb.checked = p.trading_pairs.includes(cb.value);
            });
        }
        if (p.telegram_chat_id) el("prof-tg-chat").value = p.telegram_chat_id;
        if (p.notifications) {
            if (p.notifications.signals !== undefined) el("notif-signals").checked = p.notifications.signals;
            if (p.notifications.trades !== undefined) el("notif-trades").checked = p.notifications.trades;
            if (p.notifications.tp !== undefined) el("notif-tp").checked = p.notifications.tp;
            if (p.notifications.sl !== undefined) el("notif-sl").checked = p.notifications.sl;
            if (p.notifications.system !== undefined) el("notif-system").checked = p.notifications.system;
        }
        if (p.tier) el("prof-tier").textContent = p.tier.toUpperCase();
        if (p.verified) el("prof-verified").textContent = p.verified ? "Verified" : "Pending";
        if (p.verified) el("prof-verified").style.color = "var(--green)";
        if (p.member_since) el("prof-member-since").textContent = p.member_since;
        if (p.last_login) el("prof-last-login").textContent = p.last_login;
    } catch (e) { console.warn("loadProfile error:", e); }
}

function updateModeBtns(mode) {
    var modes = ["paper", "demo", "live"];
    var colors = {paper:"#00d4ff", demo:"#ffd700", live:"#ff3b5c"};
    var msgs = {paper:"Paper mode — no real orders placed", demo:"Demo mode — trades on Delta testnet (fake money)", live:"LIVE mode — real money on Delta exchange"};
    for (var i = 0; i < modes.length; i++) {
        var btn = document.getElementById("mode-btn-" + modes[i]);
        if (!btn) continue;
        if (modes[i] === mode) {
            btn.style.borderColor = colors[modes[i]] + "80";
            btn.style.background = colors[modes[i]] + "15";
            btn.style.color = colors[modes[i]];
        } else {
            btn.style.borderColor = "rgba(255,255,255,.1)";
            btn.style.background = "transparent";
            btn.style.color = "#555";
        }
    }
    var msgEl = document.getElementById("mode-status-msg");
    if (msgEl) { msgEl.textContent = msgs[mode] || ""; msgEl.style.color = colors[mode] || "#5a7090"; }
}

function switchUserMode(mode) {
    if (mode === "live") {
        if (!confirm("Switch to LIVE trading?\n\nReal money will be at risk.\nMake sure your live API key is configured.")) return;
    }
    if (mode === "demo") {
        if (!confirm("Switch to DEMO trading?\n\nTrades will execute on Delta testnet.\nMake sure your demo API key is configured.")) return;
    }
    var sel = document.getElementById("prof-bot-mode");
    if (sel) sel.value = mode;
    updateModeBtns(mode);
    // Auto-save the mode change immediately
    fetch("/api/user/real/toggle", {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        credentials: "same-origin",
        body: JSON.stringify({bot_mode: mode})
    }).then(function(r) { return r.json(); }).then(function(d) {
        if (d.success) {
            var msgEl = document.getElementById("mode-status-msg");
            if (msgEl) msgEl.textContent += " (saved)";
        }
    }).catch(function(e) { console.error("Mode switch failed:", e); });
}

async function saveProfile() {
    const el = id => document.getElementById(id);
    const errEl = el("profile-save-error");
    const okEl = el("profile-save-ok");
    errEl.style.display = "none";
    okEl.style.display = "none";
    const pairs = [];
    document.querySelectorAll("#prof-pairs input[type='checkbox']:checked").forEach(cb => pairs.push(cb.value));
    const payload = {
        name: el("prof-name").value,
        phone: el("prof-phone").value,
        timezone: el("prof-timezone").value,
        address: {
            line1: el("prof-addr1").value, line2: el("prof-addr2").value,
            city: el("prof-city").value, state: el("prof-state").value,
            country: el("prof-country").value, postal: el("prof-postal").value
        },
        bot_mode: el("prof-bot-mode").value,
        trading_pairs: pairs,
        leverage: parseInt(el("prof-leverage").value),
        risk_per_trade: parseFloat(el("prof-risk").value),
        max_daily_loss: parseFloat(el("prof-max-loss").value),
        max_positions: parseInt(el("prof-max-pos").value),
        telegram_chat_id: el("prof-tg-chat").value,
        notifications: {
            signals: el("notif-signals").checked,
            trades: el("notif-trades").checked,
            tp: el("notif-tp").checked,
            sl: el("notif-sl").checked,
            system: el("notif-system").checked
        }
    };
    try {
        const r = await fetch("/api/user/profile", {
            method: "PUT",
            headers: {"Content-Type": "application/json"},
            credentials: "same-origin",
            body: JSON.stringify(payload)
        });
        if (r.ok) {
            okEl.style.display = "block";
            setTimeout(() => okEl.style.display = "none", 3000);
        } else {
            const data = await r.json().catch(() => ({}));
            errEl.textContent = data.error || "Save failed";
            errEl.style.display = "block";
        }
    } catch (err) {
        errEl.textContent = "Connection failed";
        errEl.style.display = "block";
    }
}

async function loadApiKeys() {
    try {
        const r = await fetch("/api/user/api-keys", {credentials: "same-origin"});
        if (!r.ok) return;
        const keys = await r.json();
        const container = document.getElementById("api-keys-list");
        if (!keys || keys.length === 0) return;
        container.innerHTML = "";
        keys.forEach(k => {
            const masked = k.key_masked || "\u2022".repeat(12);
            container.innerHTML += `
                <div class="api-key-row">
                    <div class="api-key-info">
                        <span class="api-key-label">${k.label || k.exchange}</span>
                        <span class="api-key-masked">${masked}</span>
                    </div>
                    <div class="api-key-actions">
                        <button class="btn-sm btn-edit" onclick="openApiKeyModal('${k.id}')">Edit</button>
                        <button class="btn-sm btn-del" onclick="deleteApiKey('${k.id}')">Delete</button>
                    </div>
                </div>`;
        });
    } catch (e) { console.warn("loadApiKeys error:", e); }
}

function openApiKeyModal(id) {
    const modal = document.getElementById("apikey-modal");
    modal.style.display = "flex";
    document.getElementById("ak-edit-id").value = id === "new" ? "" : id;
    document.getElementById("ak-modal-title").textContent = id === "new" ? "Add API Key" : "Edit API Key";
    if (id === "new") {
        document.getElementById("ak-exchange").value = "";
        document.getElementById("ak-label").value = "";
        document.getElementById("ak-key").value = "";
        document.getElementById("ak-secret").value = "";
        document.getElementById("ak-passphrase").value = "";
    }
    document.getElementById("ak-error").style.display = "none";
}

async function submitApiKey() {
    const editId = document.getElementById("ak-edit-id").value;
    const errEl = document.getElementById("ak-error");
    errEl.style.display = "none";
    const payload = {
        exchange: document.getElementById("ak-exchange").value,
        label: document.getElementById("ak-label").value,
        key: document.getElementById("ak-key").value,
        secret: document.getElementById("ak-secret").value,
        passphrase: document.getElementById("ak-passphrase").value
    };
    try {
        const url = editId ? `/api/user/api-keys/${editId}` : "/api/user/api-keys";
        const method = editId ? "PUT" : "POST";
        const r = await fetch(url, {
            method: method,
            headers: {"Content-Type": "application/json"},
            credentials: "same-origin",
            body: JSON.stringify(payload)
        });
        if (r.ok) {
            document.getElementById("apikey-modal").style.display = "none";
            loadApiKeys();
        } else {
            const data = await r.json().catch(() => ({}));
            errEl.textContent = data.error || "Save failed";
            errEl.style.display = "block";
        }
    } catch (err) {
        errEl.textContent = "Connection failed";
        errEl.style.display = "block";
    }
}

async function deleteApiKey(id) {
    if (!confirm("Delete this API key?")) return;
    try {
        await fetch(`/api/user/api-keys/${id}`, {method: "DELETE", credentials: "same-origin"});
        loadApiKeys();
    } catch (e) { console.warn("deleteApiKey error:", e); }
}

function refreshProfile() {
    loadProfile();
    loadApiKeys();
}

// ── GLOBALS ──────────────────────────────────────────────
let activeTab = "live";
let liveTimer = null, analyticsTimer = null, systemTimer = null, latencyTimer = null, agentsTimer = null, brainTimer = null;
let laSelectedSymbol = "BTC/USDT";
let equityChart = null;
let cachedPrices = {};
let prevRealTotalPnl = null;
let equityHistory = [];
const EQUITY_HISTORY_MAX = 720; // ~24h at 2s refresh

// ── UTILITIES ────────────────────────────────────────────
function formatTime(isoStr) {
    if (!isoStr) return "--";
    try {
        const d = new Date(isoStr);
        const day = d.toLocaleDateString("en-IN", { day: "2-digit", month: "short", timeZone: "Asia/Kolkata" });
        const time = d.toLocaleTimeString("en-IN", { hour12: false, hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata" });
        return `${day} ${time}`;
    } catch (e) { return isoStr; }
}

function pnlClass(v) { return v > 0 ? "pnl-pos" : v < 0 ? "pnl-neg" : "pnl-zero"; }
function pnlSign(v) { return v > 0 ? "+" + v.toFixed(2) : v.toFixed(2); }
function pct(v) { return v != null ? (v * 100).toFixed(1) + "%" : "--"; }
function num(v, d=2) { return v != null ? Number(v).toFixed(d) : "--"; }
function esc(s) { const el = document.createElement("span"); el.textContent = s; return el.innerHTML; }

function timeSince(isoStr) {
    if (!isoStr) return "--";
    const ms = Date.now() - new Date(isoStr).getTime();
    if (ms < 0) return "--";
    const mins = Math.floor(ms / 60000);
    if (mins < 60) return mins + "m";
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return hrs + "h " + (mins % 60) + "m";
    return Math.floor(hrs / 24) + "d " + (hrs % 24) + "h";
}

async function api(path) {
    try {
        const r = await fetch(path, {credentials: "same-origin"});
        if (!r.ok) return null;
        return await r.json();
    } catch { return null; }
}

async function setRealMode(mode) {
    if (mode === "live" && !confirm("⚠️ ENABLE LIVE TRADING?\n\nThis will place REAL orders with REAL money on Delta Exchange.\n\nAre you absolutely sure?")) {
        document.getElementById("real-mode-select").value = "dry_run";
        return;
    }
    if (mode === "live" && !confirm("🔴 FINAL CONFIRMATION\n\nReal money will be at risk.\nCircuit breaker: $25/day loss limit.\n\nType OK to confirm.")) {
        document.getElementById("real-mode-select").value = "dry_run";
        return;
    }
    const enabled = mode !== "disabled";
    const dry_run = mode !== "live";
    try {
        const r = await fetch("/api/real/toggle", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            credentials: "same-origin",
            body: JSON.stringify({enabled, dry_run})
        });
        const d = await r.json();
        if (d.error) alert("Error: " + d.error);
    } catch (e) { alert("Mode change failed: " + e); }
}
// Legacy support
// REMOVED: stale toggleRealTrading(enabled) — conflicts with robust version at line ~8224.
// The robust version reads current state from API, confirms, and flips.

function healthColor(pct) {
    if (pct > 85) return "var(--red)";
    if (pct > 70) return "var(--yellow)";
    return "var(--green)";
}

function makeHealthRow(label, value, unit, max) {
    const p = max ? (value / max * 100) : value;
    return `<div style="margin-bottom:12px">
        <div style="display:flex;justify-content:space-between;font-size:.78rem;margin-bottom:3px">
            <span class="text-secondary">${label}</span><span class="font-bold font-mono">${num(value,1)}${unit}</span>
        </div>
        <div class="health-bar"><div class="health-fill" style="width:${Math.min(p,100)}%;background:${healthColor(p)}"></div></div>
    </div>`;
}

function makeKV(label, value, color) {
    const c = color ? `color:${color}` : "";
    return `<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border);font-size:.8rem">
        <span class="text-muted">${label}</span><span style="font-weight:600;font-family:var(--font-mono);${c}">${value}</span>
    </div>`;
}

// ── EQUITY SPARKLINE (24h) ───────────────────────────────
function drawEquitySparkline() {
    const canvas = document.getElementById("equity-sparkline-canvas");
    if (!canvas || equityHistory.length < 2) return;
    const ctx = canvas.getContext("2d");
    const rect = canvas.parentElement.getBoundingClientRect();
    canvas.width = rect.width * (window.devicePixelRatio || 1);
    canvas.height = 40 * (window.devicePixelRatio || 1);
    canvas.style.width = rect.width + "px";
    canvas.style.height = "40px";
    ctx.scale(window.devicePixelRatio || 1, window.devicePixelRatio || 1);
    const w = rect.width, h = 40, pad = 2;
    const data = equityHistory;
    const min = Math.min(...data);
    const max = Math.max(...data);
    const range = max - min || 1;
    ctx.clearRect(0, 0, w, h);
    const isUp = data[data.length - 1] >= data[0];
    const color = isUp ? "#00ff9d" : "#ff3b5c";
    // Draw filled area
    ctx.beginPath();
    for (let i = 0; i < data.length; i++) {
        const x = pad + (i / (data.length - 1)) * (w - 2 * pad);
        const y = pad + (1 - (data[i] - min) / range) * (h - 2 * pad);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.lineTo(w - pad, h - pad);
    ctx.lineTo(pad, h - pad);
    ctx.closePath();
    ctx.fillStyle = isUp ? "rgba(0,255,157,.08)" : "rgba(255,59,92,.08)";
    ctx.fill();
    // Draw line
    ctx.beginPath();
    for (let i = 0; i < data.length; i++) {
        const x = pad + (i / (data.length - 1)) * (w - 2 * pad);
        const y = pad + (1 - (data[i] - min) / range) * (h - 2 * pad);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.stroke();
}

// ── FOOTER UPTIME UPDATE ────────────────────────────────
function updateFooter(status) {
    if (!status) return;
    const uptimeEl = document.getElementById("uptime-badge");
    const restartEl = document.getElementById("last-restart");
    if (uptimeEl && status.uptime) uptimeEl.textContent = "Uptime: " + status.uptime;
    if (restartEl && status.start_time) restartEl.textContent = "Last Restart: " + formatTime(status.start_time);
    // Footer
    const fu = document.getElementById("footer-uptime");
    const fr = document.getElementById("footer-restart");
    if (fu && status.uptime) fu.textContent = status.uptime;
    if (fr && status.start_time) fr.textContent = formatTime(status.start_time);
}

// ── TAB SWITCHING ────────────────────────────────────────
function switchTab(tab) {
    activeTab = tab;
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
    document.querySelectorAll(".tab-content").forEach(c => c.classList.toggle("active", c.id === "tab-" + tab));
    clearInterval(liveTimer); clearInterval(analyticsTimer); clearInterval(systemTimer); clearInterval(latencyTimer); clearInterval(agentsTimer); clearInterval(brainTimer); if(typeof adminTimer!=='undefined')clearInterval(adminTimer);
    if (tab === "live") { refreshLive(); liveTimer = setInterval(refreshLive, 2000); }
    if (tab === "latency") { refreshLatencyArb(); latencyTimer = setInterval(refreshLatencyArb, 1000); }
    if (tab === "analytics") { refreshAnalytics(); analyticsTimer = setInterval(refreshAnalytics, 10000); }
    if (tab === "system") { refreshSystem(); systemTimer = setInterval(refreshSystem, 5000); }
    if (tab === "agents") { refreshAgents(); agentsTimer = setInterval(refreshAgents, 15000); }
    if (tab === "brain") { refreshBrainTab(); brainTimer = setInterval(refreshBrainTab, 10000); }
    if (tab === "admin") { refreshAdmin(); adminTimer = setInterval(refreshAdmin, 15000); }
}

// ══════════════════════════════════════════════════════════
// TAB: AGENTS REFRESH
// ══════════════════════════════════════════════════════════
async function refreshAgents() {
  try {
    const slHours = window._slWindowHours != null ? window._slWindowHours : 4;
    const slUrl = slHours > 0
      ? `/api/pipeline/stage_stats?hours=${slHours}&limit=5000`
      : `/api/pipeline/stage_stats?since_ts=0&limit=5000`;
    const [d, pm, supStatus, stageStats] = await Promise.all([
      fetch("/api/agents/status").then(r => r.json()).catch(() => ({})),
      fetch("/api/pipeline/overview").then(r => r.json()).catch(() => ({})),
      fetch("/api/supervisor/status").then(r => r.json()).catch(() => ({})),
      fetch(slUrl).then(r => r.json()).catch(() => ({})),
    ]);

    // ── Phase 2A + 3.1: Stage Loss Map with time filter ──
    renderStageLossMap(stageStats);

    // ── Phase 0: Pipeline Funnel ──
    if (pm && pm.funnel) {
      const f = pm.funnel;
      const si = n => document.getElementById(n);
      si("pm-scanned") && (si("pm-scanned").textContent = (f.scanned||0).toLocaleString());
      si("pm-paper") && (si("pm-paper").textContent = (f.paper_emitted||0).toLocaleString());
      si("pm-real-exec") && (si("pm-real-exec").textContent = (f.real_executed||0).toLocaleString());
      si("pm-real-qual") && (si("pm-real-qual").textContent = (f.real_qualified||0).toLocaleString());
      si("pm-real-rej") && (si("pm-real-rej").textContent = (f.real_rejected||0).toLocaleString());
      const execEvents = pm.execution ? Object.values(pm.execution).reduce((a,b)=>a+b,0) : 0;
      si("pm-exec-ev") && (si("pm-exec-ev").textContent = execEvents.toLocaleString());
      // Uptime
      const upt = pm.uptime_sec || 0;
      si("pm-uptime") && (si("pm-uptime").textContent = upt > 3600 ? (upt/3600).toFixed(1)+"h" : Math.round(upt/60)+"m");
      // Rejection leaderboard
      const rl = document.getElementById("pm-reject-list");
      if (rl && pm.rejections && pm.rejections.top && pm.rejections.top.length) {
        rl.innerHTML = pm.rejections.top.slice(0,5).map(r =>
          `<div style="display:flex;justify-content:space-between;font-size:.7rem;padding:2px 0;border-bottom:1px solid var(--border)">
            <span style="color:var(--text-dim)">${r.reason}</span>
            <span style="font-weight:700;color:var(--red)">${r.count}</span>
          </div>`
        ).join("");
      } else if (rl) {
        rl.innerHTML = '<div class="text-sm text-muted">No rejections today</div>';
      }
      // CB status
      const cbEl = document.getElementById("pm-cb-status");
      if (cbEl && pm.circuit_breaker) {
        const cb = pm.circuit_breaker;
        cbEl.textContent = cb.is_tripped ? `TRIPPED: ${cb.trip_reason}` : `OK | PnL $${(cb.daily_pnl||0).toFixed(2)} | losses: ${cb.consecutive_losses||0}`;
        cbEl.style.color = cb.is_tripped ? "var(--red)" : "var(--green)";
      }
    }

    // ── Phase 0: Agent Heartbeats ──
    const hbList = document.getElementById("pm-heartbeat-list");
    if (hbList && pm && pm.agents) {
      const hbs = pm.agents;
      const keys = Object.keys(hbs);
      if (keys.length) {
        hbList.innerHTML = keys.map(comp => {
          const h = hbs[comp];
          const age = h.last_seen_sec || 0;
          const st = h.status || "unknown";
          const col = st === "ok" ? "var(--green)" : st === "stale" ? "var(--yellow)" : "var(--red)";
          return `<div style="display:flex;justify-content:space-between;align-items:center;font-size:.72rem;padding:3px 0;border-bottom:1px solid var(--border)">
            <span style="color:var(--text-dim)">${comp}</span>
            <span><span style="color:${col};font-weight:700">${st.toUpperCase()}</span> <span class="text-muted">${age.toFixed(0)}s ago</span></span>
          </div>`;
        }).join("");
      } else {
        hbList.innerHTML = '<div class="text-sm text-muted">No heartbeats yet (grace period)</div>';
      }
    }

    // ── Supervisor status indicator ──
    const supEl = document.getElementById("pm-sup-indicator");
    if (supEl && supStatus) {
      const running = supStatus.running;
      const anomalies = supStatus.anomaly_count || 0;
      supEl.textContent = running ? `Supervisor ✓ (${anomalies} alerts)` : "Supervisor ✗ OFF";
      supEl.style.background = running ? (anomalies > 0 ? "rgba(255,59,92,.12)" : "rgba(0,255,157,.08)") : "rgba(255,59,92,.12)";
      supEl.style.color = running ? (anomalies > 0 ? "var(--red)" : "var(--green)") : "var(--red)";
    }

    // Agent status cards
    const agents = d.agents || {};
    let activeCount = Object.values(agents).filter(a => a.status === "RUNNING").length;
    const el = id => document.getElementById(id);
    el("agents-count").textContent = activeCount + " active";
    el("agents-count").className = "badge " + (activeCount > 0 ? "badge-green" : "badge-yellow");

    // QA Agent
    const qa = agents.qa || {};
    el("qa-status").textContent = qa.status || "IDLE";
    el("qa-status").className = "badge " + (qa.status === "RUNNING" ? "badge-green" : "");
    el("qa-last-run").textContent = "Last run: " + (qa.last_run || "--");
    el("qa-summary").textContent = qa.summary || "Waiting for first run...";

    // Loss Analyzer
    const la = agents.loss_analyzer || {};
    el("loss-analyzer-status").textContent = la.status || "IDLE";
    el("loss-analyzer-status").className = "badge " + (la.status === "RUNNING" ? "badge-green" : "");
    el("loss-last-run").textContent = "Last run: " + (la.last_run || "--");
    el("loss-summary").textContent = la.summary || "Monitoring losses...";

    // ML Trainer — also check /api/ml/health for fresh model dates
    const ml = agents.ml_trainer || {};
    el("ml-trainer-status").textContent = ml.status || "IDLE";
    el("ml-trainer-status").className = "badge " + (ml.status === "RUNNING" ? "badge-purple" : "");
    // Fetch fresh model dates from ML health (more reliable than agent status)
    try {
        const mlh = await fetch("/api/ml/health").then(r => r.json()).catch(() => null);
        if (mlh && mlh.scanners) {
            const scanners = Object.values(mlh.scanners);
            const latestTrain = scanners.reduce((best, s) => {
                const t = s.trained_at || "";
                return t > best ? t : best;
            }, "");
            const modelCount = scanners.length;
            const bestScanner = scanners.reduce((best, s) =>
                (s.age_hours || 999) < (best.age_hours || 999) ? s : best, scanners[0] || {});
            el("ml-last-run").textContent = "Last run: " + (latestTrain ? latestTrain.substring(0, 16).replace("T", " ") : (ml.last_run || "--"));
            el("ml-summary").textContent = modelCount + " models | Best: " + (bestScanner.scanner || "?") + " AUC=" + ((bestScanner.n_features || 0) > 0 ? "48f" : "?");
        } else {
            el("ml-last-run").textContent = "Last run: " + (ml.last_run || "--");
            el("ml-summary").textContent = ml.summary || "Loading...";
        }
    } catch(e) {
        el("ml-last-run").textContent = "Last run: " + (ml.last_run || "--");
        el("ml-summary").textContent = ml.summary || "Loading...";
    }

    // ML Model table
    const models = d.ml_models || [];
    const mtb = el("ml-model-table");
    if (models.length) {
      mtb.innerHTML = models.map(m => {
        const auc = (m.auc || 0).toFixed(3);
        const cls = m.auc >= 0.60 ? "color:var(--green)" : m.auc >= 0.53 ? "color:var(--yellow)" : "color:var(--red)";
        return `<tr>
          <td>${m.scanner}</td>
          <td style="${cls};font-weight:700">${auc}</td>
          <td>${(m.spread || 0).toFixed(3)}</td>
          <td>${(m.candidates || 0).toLocaleString()}</td>
          <td><span class="badge ${m.live ? 'badge-green' : ''}">${m.live ? 'LIVE' : 'SHADOW'}</span></td>
        </tr>`;
      }).join("");
    }

    // QA Test Results
    const qa_results = d.qa_results || {};
    el("qa-unit-result").textContent = qa_results.unit || "--";
    el("qa-unit-result").style.color = (qa_results.unit || "").includes("PASS") ? "var(--green)" : "var(--red)";
    el("qa-integration-result").textContent = qa_results.integration || "--";
    el("qa-integration-result").style.color = (qa_results.integration || "").includes("PASS") ? "var(--green)" : "var(--red)";
    el("qa-api-result").textContent = qa_results.api || "--";
    el("qa-api-result").style.color = (qa_results.api || "").includes("PASS") ? "var(--green)" : "var(--red)";
    el("qa-data-result").textContent = qa_results.data_integrity || "--";
    el("qa-data-result").style.color = (qa_results.data_integrity || "").includes("PASS") ? "var(--green)" : "var(--red)";
    const passRate = qa_results.pass_rate || "--";
    el("qa-pass-rate").textContent = passRate;
    el("qa-pass-rate").className = "badge " + (String(passRate).includes("100") ? "badge-green" : "badge-yellow");

    // Defects table
    const defects = d.defects || [];
    el("defect-count").textContent = defects.length;
    const dtb = el("defects-table");
    if (defects.length) {
      dtb.innerHTML = defects.map(df => {
        const sevCls = df.severity === "CRITICAL" ? "color:var(--red)" : df.severity === "HIGH" ? "color:var(--yellow)" : "";
        return `<tr>
          <td>${df.id}</td>
          <td style="${sevCls};font-weight:700">${df.severity}</td>
          <td style="font-size:.75rem">${df.description}</td>
          <td><span class="badge ${df.status === 'FIXED' ? 'badge-green' : 'badge-red'}">${df.status}</span></td>
        </tr>`;
      }).join("");
    } else {
      dtb.innerHTML = '<tr><td colspan="4" class="text-muted">No defects</td></tr>';
    }

    // Loss analysis
    const losses = d.recent_losses || [];
    const lal = el("loss-analysis-list");
    if (losses.length) {
      lal.innerHTML = losses.map(l => `
        <div class="stat-card" style="margin-bottom:6px;border-left:3px solid var(--red)">
          <div class="flex justify-between">
            <strong>${l.symbol} ${l.side}</strong>
            <span class="text-danger font-bold">${l.pnl}</span>
          </div>
          <div class="text-muted" class="text-sm">${l.time} | ${l.scanner} | ${l.reason}</div>
          <div style="font-size:.75rem;margin-top:4px;color:var(--text-muted)">${l.analysis || ''}</div>
        </div>
      `).join("");
    } else {
      lal.innerHTML = '<div class="text-muted">No recent losses to analyze</div>';
    }
  } catch(e) { console.error("Agents refresh error:", e); }
}

// ══════════════════════════════════════════════════════════
// TAB 1: LIVE TRADING REFRESH
// ══════════════════════════════════════════════════════════
async function refreshLive() { window._refreshLiveActive = true; await _refreshLiveInner(); } async function _refreshLiveInner() {
    const [status, decision, active, signals, funnel, alerts, closed, realStatus, trkStats] = await Promise.all([
        api("/api/status"), api("/api/decision"), api("/api/tracker/active"),
        api("/api/signals"), api("/api/opportunity-funnel"), api("/api/alerts"),
        api("/api/tracker/closed"), api("/api/real/status"), api("/api/tracker/stats")
    ]);

    // Real Trading Panel — update BOTH analytics and live tab versions
    if (realStatus) {
        const modeVal = !realStatus.enabled ? "disabled" : realStatus.dry_run ? "dry_run" : "live";
        const modeLabel = !realStatus.enabled ? "DISABLED" : realStatus.dry_run ? "DRY RUN" : "LIVE";
        const modeColor = !realStatus.enabled ? "var(--text-muted)" : realStatus.dry_run ? "var(--cyan)" : "var(--red)";

        // ── MODE BANNER (persistent top strip) ──
        const mb = document.getElementById("mode-banner");
        if (mb) {
            const bannerBg = modeVal === "live" ? "rgba(255,59,92,.12)" : modeVal === "dry_run" ? "rgba(0,212,255,.08)" : "rgba(0,100,255,.06)";
            const bannerBorder = modeVal === "live" ? "rgba(255,59,92,.4)" : modeVal === "dry_run" ? "rgba(0,212,255,.3)" : "rgba(0,100,255,.15)";
            const bannerText = modeVal === "live" ? "var(--red)" : modeVal === "dry_run" ? "var(--cyan)" : "rgba(100,180,255,.8)";
            const icon = modeVal === "live" ? "\uD83D\uDD34" : modeVal === "dry_run" ? "\uD83D\uDFE1" : "\uD83D\uDFE2";
            mb.style.background = bannerBg;
            mb.style.borderBottom = "1px solid " + bannerBorder;
            mb.style.color = bannerText;
            const ml = document.getElementById("mode-label");
            if (ml) ml.textContent = icon + " " + (modeVal === "live" ? "LIVE TRADING — REAL MONEY" : modeVal === "dry_run" ? "DRY RUN — DEMO EXCHANGE (TESTNET)" : "PAPER TRADING MODE");
            const ms = document.getElementById("mode-stats");
            const cb2 = realStatus.circuit_breaker || {};
            if (ms) ms.innerHTML = "Balance: $" + (realStatus.balance||0).toFixed(2) + " | Today: " + (cb2.trade_count_today||0) + " trades | PnL: $" + (cb2.daily_pnl||0).toFixed(2);
        }
        const bal = realStatus.balance || 0;
        const cb = realStatus.circuit_breaker || {};
        const dp = cb.daily_pnl || 0;
        const tp = cb.total_pnl || dp;
        const cbOk = !cb.is_tripped;

        // Mode-specific styling
        const panelBorderColor = modeVal === "live" ? "rgba(255,59,92,.5)" : modeVal === "dry_run" ? "rgba(0,212,255,.4)" : "rgba(90,112,144,.2)";
        const posLabel = modeVal === "live" ? "LIVE POSITIONS" : modeVal === "dry_run" ? "DEMO POSITIONS (TESTNET)" : "POSITIONS";

        // Update both panel instances (analytics + live tab)
        for (const suffix of ["", "-live"]) {
            const sel = document.getElementById("real-mode-select" + suffix);
            const selLive2 = document.getElementById("real-mode-select-live");
            if (selLive2) selLive2.value = modeVal;
            if (sel) sel.value = modeVal;
            const badge = document.getElementById("real-mode-badge" + suffix);
            if (badge) { badge.textContent = modeLabel; badge.style.color = modeColor; badge.style.borderColor = modeColor; }
            // Update panel border color based on mode
            const panel = document.getElementById("real-trading-panel" + suffix);
            if (panel) panel.style.borderColor = panelBorderColor;
            // Update positions label
            const posLabelEl = document.getElementById("real-positions-label" + suffix);
            if (posLabelEl) posLabelEl.textContent = posLabel;
            const balEl = document.getElementById("real-balance" + suffix);
            if (balEl) balEl.textContent = "$" + num(bal, 2);
            const dpEl = document.getElementById("real-daily-pnl" + suffix);
            if (dpEl) { dpEl.textContent = "$" + (dp >= 0 ? "+" : "") + num(dp, 2); dpEl.className = dp >= 0 ? "positive" : "negative"; }
            const tpEl = document.getElementById("real-total-pnl" + suffix);
            if (tpEl) { tpEl.textContent = "$" + (tp >= 0 ? "+" : "") + num(tp, 2); tpEl.className = tp >= 0 ? "positive" : "negative"; }
            const ocEl = document.getElementById("real-open-count" + suffix);
            if (ocEl) ocEl.textContent = realStatus.open_count || 0;
            const ctEl = document.getElementById("real-closed-today" + suffix);
            if (ctEl) ctEl.textContent = realStatus.closed_today || 0;
            const cbEl = document.getElementById("real-cb-status" + suffix);
            if (cbEl) { cbEl.textContent = cbOk ? "OK" : "TRIPPED"; cbEl.style.color = cbOk ? "var(--green)" : "var(--red)"; }

            // Positions table
            const posTable = document.getElementById("real-positions-table" + suffix);
            const posBody = document.getElementById("real-positions-body" + suffix);
            if (posTable && posBody && realStatus.open_positions && realStatus.open_positions.length > 0) {
                posTable.style.display = "block";
                let ph = "";
                for (const p of realStatus.open_positions) {
                    const sideColor = p.side === "long" ? "var(--green)" : "var(--red)";
                    const upnlColor = p.upnl_usd >= 0 ? "var(--green)" : "var(--red)";
                    const dur = p.duration_min >= 60 ? num(p.duration_min/60,1) + "h" : num(p.duration_min,0) + "m";
                    ph += "<tr><td>" + p.symbol + "</td><td style='color:" + sideColor + "'>" + (p.side||"").toUpperCase() + "</td>" +
                        "<td style='text-align:right'>" + num(p.entry_price,4) + "</td><td style='text-align:right'>" + num(p.current_price,4) + "</td>" +
                        "<td style='text-align:right;color:" + upnlColor + "'>$" + num(p.upnl_usd,2) + " (" + num(p.upnl_pct,2) + "%)</td>" +
                        "<td style='text-align:right;color:var(--red)'>" + num(p.stop_loss,4) + "</td>" +
                        "<td style='text-align:right;color:var(--green)'>" + (p.tp1 ? num(p.tp1,4) : "--") + "</td>" +
                        "<td style='text-align:right'>$" + num(p.margin,2) + "</td><td style='text-align:right'>$" + num(p.position_usd,2) + "</td>" +
                        "<td style='text-align:center'>" + p.leverage + "x</td><td>" + (p.scanner||"--") + "</td>" +
                        "<td style='text-align:center'>" + (p.ml_prob ? num(p.ml_prob*100,0) + "%" : "--") + "</td>" +
                        "<td style='text-align:right'>" + dur + "</td></tr>";
                }
                posBody.innerHTML = ph;
            } else if (posTable) {
                posTable.style.display = "none";
            }
        }
    }
    if (realStatus) {
        const rp = document.getElementById("real-trading-panel");
        if (!rp) return; // GUARD: element may not exist
        {
            const mode = realStatus.mode || "DISABLED";
            const badge = document.getElementById("real-mode-badge");
            if (!badge) return; // GUARD: element may not exist
            badge.textContent = mode;
            badge.style.background = mode === "LIVE" ? "var(--red)" : mode === "DRY RUN" ? "var(--cyan)" : "var(--text-muted)";
            badge.style.color = mode === "LIVE" ? "#fff" : "#000";
            const bal = realStatus.balance || 0;
            (document.getElementById("real-balance") || {textContent:"",innerHTML:"",style:{},classList:{add:()=>{},remove:()=>{}},value:""}).textContent = "$" + bal.toFixed(2);
            const dp = realStatus.circuit_breaker ? realStatus.circuit_breaker.daily_pnl : 0;
            const dpEl = document.getElementById("real-daily-pnl");
            if (!dpEl) return; // GUARD: element may not exist
            dpEl.textContent = (dp >= 0 ? "+$" : "-$") + Math.abs(dp).toFixed(2);
            dpEl.style.color = dp >= 0 ? "var(--green)" : "var(--red)";
            const tp = realStatus.circuit_breaker ? realStatus.circuit_breaker.total_pnl : 0;
            const tpEl = document.getElementById("real-total-pnl");
            if (!tpEl) return; // GUARD: element may not exist
            tpEl.textContent = (tp >= 0 ? "+$" : "-$") + Math.abs(tp).toFixed(2);
            tpEl.style.color = tp >= 0 ? "var(--green)" : "var(--red)";
            // Flash on PnL change
            if (prevRealTotalPnl !== null && tp !== prevRealTotalPnl) {
                const flashClass = tp > prevRealTotalPnl ? "flash-green" : "flash-red";
                rp.classList.remove("flash-green", "flash-red");
                void rp.offsetWidth; // force reflow
                rp.classList.add(flashClass);
                setTimeout(() => rp.classList.remove(flashClass), 600);
            }
            prevRealTotalPnl = tp;
            (document.getElementById("real-open-count") || {textContent:"",innerHTML:"",style:{},classList:{add:()=>{},remove:()=>{}},value:""}).textContent = realStatus.open_count || 0;
            (document.getElementById("real-closed-today") || {textContent:"",innerHTML:"",style:{},classList:{add:()=>{},remove:()=>{}},value:""}).textContent = realStatus.closed_today || 0;
            const cb = realStatus.circuit_breaker || {};
            const cbEl = document.getElementById("real-cb-status");
            if (!cbEl) return; // GUARD: element may not exist
            cbEl.textContent = cb.is_tripped ? "TRIPPED" : "OK";
            cbEl.style.color = cb.is_tripped ? "var(--red)" : "var(--green)";
            // Render open positions table
            const posTable = document.getElementById("real-positions-table");
            if (!posTable) return; // GUARD: element may not exist
            const posBody = document.getElementById("real-positions-body");
            if (!posBody) return; // GUARD: element may not exist
            const positions = realStatus.open_positions || [];
            if (positions.length > 0) {
                posTable.style.display = "block";
                posBody.innerHTML = positions.map(p => {
                    const sideColor = p.side === "long" ? "var(--green)" : "var(--red)";
                    const mlColor = (p.ml_prob || 0) >= 0.5 ? "var(--green)" : (p.ml_prob || 0) >= 0.45 ? "var(--yellow)" : "var(--red)";
                    const posUsd = p.position_usd || (p.margin || 0) * (p.leverage || 1);
                    const upnl = p.upnl_usd || 0;
                    const upnlPct = p.upnl_pct || 0;
                    const upnlColor = upnl >= 0 ? "var(--green)" : "var(--red)";
                    const dec = p.symbol.includes("BTC") ? 2 : p.symbol.includes("ETH") ? 2 : 4;
                    const durMin = p.duration_min || 0;
                    const durStr = durMin >= 60 ? (durMin/60).toFixed(1)+"h" : durMin.toFixed(0)+"m";
                    const coin = p.symbol.split("/")[0];
                    const size = p.position_size || 0;
                    const sizeStr = size >= 1 ? size.toFixed(2) : size.toFixed(6);
                    return `<tr class="border-b">
                        <td style="padding:4px;font-weight:600">${p.symbol}</td>
                        <td style="padding:4px;color:${sideColor};font-weight:600;text-transform:uppercase">${p.side}</td>
                        <td class="p-1 text-right font-mono">${(p.entry_price||0).toFixed(dec)}</td>
                        <td class="p-1 text-right font-mono">${(p.current_price||0).toFixed(dec)}</td>
                        <td style="padding:4px;text-align:right;font-family:var(--font-mono);color:${upnlColor};font-weight:600">${upnl>=0?"+":""}$${upnl.toFixed(2)} (${upnlPct>=0?"+":""}${upnlPct.toFixed(2)}%)</td>
                        <td style="padding:4px;text-align:right;font-family:var(--font-mono);color:var(--red)">${(p.stop_loss||0).toFixed(dec)}</td>
                        <td style="padding:4px;text-align:right;font-family:var(--font-mono);color:var(--green)">${(p.tp1||0).toFixed(dec)}</td>
                        <td class="p-1 text-right font-mono">$${(p.margin||0).toFixed(2)}</td>
                        <td class="p-1 text-right font-mono">$${posUsd.toFixed(2)}</td>
                        <td class="p-1 text-right font-mono">${sizeStr} ${coin}</td>
                        <td style="padding:4px;text-align:center">${p.leverage||0}x</td>
                        <td style="padding:4px;color:var(--cyan)">${p.scanner||"--"}</td>
                        <td style="padding:4px;text-align:center;color:${mlColor}">${((p.ml_prob||0)*100).toFixed(0)}% ${p.ml_verdict||""}</td>
                        <td style="padding:4px;color:var(--text-muted)">${p.regime||"--"}</td>
                        <td style="padding:4px;text-align:right;color:var(--text-muted)">${durStr}</td>
                    </tr>`;
                }).join("");
            } else {
                posTable.style.display = "none";
            }
            // Update real closed trades tab
            _lastRealStatus = realStatus;  // cache for tab switching
            updateRealClosedTrades(realStatus);
        }
    }

    // Update header status
    if (status) {
        const dot = document.getElementById("hdr-dot");
        const hdrStatus = document.getElementById("hdr-status");
        if (status.bot_status === "running") {
            dot.className = "dot on";
            hdrStatus.textContent = "Running";
            hdrStatus.style.color = "var(--green)";
        } else {
            dot.className = "dot off";
            hdrStatus.textContent = "Offline";
            hdrStatus.style.color = "var(--red)";
        }
    }

    // Cache prices & update ticker
    if (status && status.prices) {
        const prevPrices = {...cachedPrices};
        cachedPrices = status.prices;
        updatePriceTicker(cachedPrices, prevPrices, status);
    }

    // Paper balance
    try {
        const trkSnap = await api("/api/tracker/stats");
        if (trkSnap) {
            const balEl = document.getElementById("price-bal");
            const isPaper = trkSnap.is_paper_mode !== false;
            const startBal = trkSnap.paper_start_balance || 1000;
            const netPnl = trkSnap.paper_pnl_usd || 0;
            const displayVal = isPaper ? (startBal + netPnl) : (trkSnap.exchange_balance || 0);
            const cls = netPnl >= 0 ? "green" : "red";
            balEl.textContent = "$" + displayVal.toFixed(2);
            balEl.className = cls;
            balEl.style.fontFamily = "var(--font-mono)";
            balEl.style.fontWeight = "700";
        }
    } catch(e) {}

    // Track equity for sparkline
    if (status) {
        const eqHist = status.equity_history;
        if (eqHist && Array.isArray(eqHist) && eqHist.length > 0) {
            equityHistory = eqHist.slice(-EQUITY_HISTORY_MAX);
        } else {
            // Fallback: track client-side from balance
            try {
                const trkS = await api("/api/tracker/stats");
                if (trkS) {
                    const bal = (trkS.paper_start_balance || 1000) + (trkS.paper_pnl_usd || 0);
                    equityHistory.push(bal);
                    if (equityHistory.length > EQUITY_HISTORY_MAX) equityHistory.shift();
                }
            } catch(e) {}
        }
        drawEquitySparkline();
    }

    // Update footer
    updateFooter(status);

    updateBanner(status, decision);
    updateCommand(decision, status);
    updateActiveTrades(active);
    updateSignals(signals);
    updateFunnel(funnel);
    updateAlerts(alerts);
    updateVetoStats(funnel);
    try { updateKanbanFunnel(funnel); } catch (e) {}
    // per-symbol status removed (consolidated into Symbol Scanner Status)
    updateSetupLifecycle(status, closed);
    updateRecentClosed(closed);
    // #2 + #9 MTF chain
    updateMTFChain(decision);
    // #10 Real active trades
    if (realStatus) updateRealActiveTrades(realStatus);
    // Command Center 2.0: live PnL + real toggle state
    try { updateCC20(status, realStatus); } catch (e) {}

    // Vision Tier 1: Attention Rail + KPIs + Signal Radar
    try { updateAttentionRail(status, decision, active, realStatus, funnel); } catch (e) {}
    try { updateDeployableCapital(realStatus); } catch (e) {}
    try { updateLiveEdge(closed); } catch (e) {}
    try { updateRealEdge(realStatus); } catch (e) {}
    try { updateSignalRadar(signals, status); } catch (e) {}

    // Execution Quality metrics (slippage tracking)
    if (closed && Array.isArray(closed) && closed.length > 0) {
        const recent50 = closed.slice(-50);
        const slips = recent50.map(t => t.slippage_bps || 0);
        const slipTicks = recent50.map(t => t.slippage_ticks || 0);
        const slipR = recent50.map(t => t.slippage_impact_r || 0);
        const avgSlipBps = slips.reduce((a,b)=>a+b,0) / slips.length;
        const avgSlipTicks = slipTicks.reduce((a,b)=>a+b,0) / slipTicks.length;
        const dragR = slipR.reduce((a,b)=>a+b,0);
        const maxSlip = Math.max(...slipTicks);
        const el = (id, val) => { const e = document.getElementById(id); if (e) e.textContent = val; };
        el("eq-avg-slip", avgSlipTicks.toFixed(2));
        el("eq-avg-bps", avgSlipBps.toFixed(1));
        el("eq-drag-r", dragR.toFixed(4) + "R");
        el("eq-max-slip", maxSlip.toFixed(1));
        el("eq-fill-rate", "100%");
        // Color coding
        const setColor = (id, val, g, y) => { const e = document.getElementById(id); if (e) e.style.color = val <= g ? "var(--green)" : val <= y ? "var(--yellow)" : "var(--red)"; };
        setColor("eq-avg-slip", avgSlipTicks, 1.5, 3.0);
        setColor("eq-max-slip", maxSlip, 3.0, 5.0);
    }

    // Mini bar (tracker is source of truth for balance)
    try {
        const tStats = await api("/api/tracker/stats");
        if (tStats) {
            const ppEl = document.getElementById("paper-bal-mini");
            if (ppEl) ppEl.textContent = "$" + (tStats.paper_balance || 0).toLocaleString("en-US", {minimumFractionDigits:2});
            const scEl = document.getElementById("signals-mini-count");
            if (scEl) scEl.textContent = tStats.closed || 0;
        }
    } catch(e) {}

    // Latency
    try {
        const t0 = performance.now();
        const ping = await api("/api/ping");
        const t1 = performance.now();
        const latMs = Math.round(t1 - t0);
        const latEl = document.getElementById("latency-ms");
        if (latEl) {
            latEl.textContent = latMs + "ms";
            latEl.style.color = latMs < 100 ? "var(--green)" : latMs < 500 ? "var(--yellow)" : "var(--red)";
        }
    } catch(e) {}
}

const SYMBOL_COLORS = {"BTC":"#f7931a","ETH":"#627eea","AVAX":"#e84142","SOL":"#9945ff","LINK":"#2a5ada","DOGE":"#c3a634","SUI":"#4da2ff","WIF":"#8b5cf6"};

function updatePriceTicker(prices, prev, status) {
    const container = document.getElementById("ticker-symbols");
    if (!container) return;
    // Only show pairs that are actively traded (have real volume/signals)
    const ACTIVE_PAIRS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT"];
    const symbols = Object.keys(prices).filter(s => ACTIVE_PAIRS.includes(s)).sort();
    let html = "";
    for (const sym of symbols) {
        const price = prices[sym] || 0;
        const prevPrice = prev[sym] || price;
        const base = sym.split("/")[0];
        const color = SYMBOL_COLORS[base] || "#9ca3af";
        const chg = prevPrice > 0 ? ((price - prevPrice) / prevPrice * 100) : 0;
        const chgColor = chg >= 0 ? "var(--green)" : "var(--red)";
        const arrow = chg >= 0 ? "\u25B2" : "\u25BC";
        const decimals = price > 100 ? 2 : price > 1 ? 2 : 4;
        const fmtPrice = price > 100 ? "$" + price.toLocaleString("en-US", {minimumFractionDigits:2, maximumFractionDigits:2}) : "$" + price.toFixed(decimals);
        html += `<div class="ticker-item">
            <span class="ticker-sym">${base}/USDT</span>
            <span class="ticker-price" style="color:${color}">${fmtPrice}</span>
            <span class="ticker-chg" style="color:${chgColor}">${arrow}${Math.abs(chg).toFixed(3)}%</span>
            <span class="sparkline-wrap" id="spark-${base}"></span>
        </div>`;
    }
    container.innerHTML = html;
    updateSparklines(prices);
    const timeEl = document.getElementById("price-update-time");
    if (timeEl) timeEl.textContent = status.last_data_update || "";
}

function updateBanner(status, decision) {
    const banner = document.getElementById("alert-banner");
    const icon = document.getElementById("alert-icon");
    const text = document.getElementById("alert-text");
    const msgs = [];
    let level = "normal";

    if (status) {
        if (status.risk_state && status.risk_state !== "normal" && status.risk_state !== "active") {
            level = "critical"; msgs.push("Risk: " + status.risk_state);
        }
        if (status.exchange_connected === false) {
            level = "critical"; msgs.push("Exchange disconnected");
        }
    }
    if (decision) {
        if (decision.action === "BLOCKED") {
            if (level !== "critical") level = "warning";
            const reasons = decision.blockers || decision.reasons || [];
            if (reasons.length) msgs.push(reasons[0]);
        }
    }

    banner.className = "alert-banner " + level;
    if (msgs.length === 0 && level === "normal") {
        icon.textContent = "\u2713";
        text.textContent = "All systems operational";
    } else if (level === "critical") {
        icon.textContent = "\u26A0";
        text.textContent = msgs.join(" \u2022 ");
    } else if (level === "warning") {
        icon.textContent = "\u26A0";
        text.textContent = msgs.join(" \u2022 ");
    } else {
        icon.textContent = "\u2713";
        text.textContent = "All systems operational";
    }
    banner.style.display = "flex";
}

function updateCommand(d, status) {
    if (!d) return;
    const actionEl = document.getElementById("cmd-action");
    const action = (d.action || "WAIT").toUpperCase();
    actionEl.textContent = action;
    actionEl.className = "cmd-action " + action;

    document.getElementById("cmd-confidence").textContent = (d.best_score != null ? num(d.best_score, 0) + "%" : (d.confidence != null ? num(d.confidence, 0) + "%" : "--"));
    document.getElementById("cmd-grade").textContent = d.best_grade || d.best_tier || d.grade || "--";
    document.getElementById("cmd-reason").textContent = (d.reasons && d.reasons.length) ? d.reasons.join(" | ") : (d.reason || "");

    const regime = d.regime || (status && status.market_regime) || "--";
    document.getElementById("cmd-regime").textContent = regime;
    document.getElementById("cmd-session").textContent = d.session || "--";
    document.getElementById("cmd-ev").textContent = "EV: " + (d.best_ev != null && d.best_ev !== 0 ? num(d.best_ev, 3) : (d.rolling_expectancy != null ? num(d.rolling_expectancy, 3) + "R" : "--"));

    const riskState = d.risk_state || (status && status.risk_state) || (status && !status.paused ? "NORMAL" : "PAUSED");
    const riskEl = document.getElementById("cmd-risk");
    riskEl.textContent = riskState;
    riskEl.style.borderLeftColor = riskState === "NORMAL" || riskState === "normal" ? "var(--green)" : "var(--red)";

    const blockerWrap = document.getElementById("cmd-blockers");
    const blockers = d.blockers || [];
    blockerWrap.innerHTML = blockers.map(b =>
        `<span style="display:inline-block;font-size:.7rem;padding:2px 6px;margin:2px;border-radius:3px;background:var(--red-dim);color:var(--red);border:1px solid rgba(239,68,68,.1)">${esc(b)}</span>`
    ).join("");
}

function updateActiveTrades(trades) {
    const wrap = document.getElementById("active-trades-cards");
    const countEl = document.getElementById("active-trade-count");
    if (!trades || !Array.isArray(trades) || trades.length === 0) {
        wrap.innerHTML = '<div class="empty">No active trades</div>';
        countEl.textContent = "";
        return;
    }
    countEl.textContent = `${trades.length} open`;

    wrap.innerHTML = trades.map(t => {
        const sym = t.symbol || "--";
        const entry = Number(t.entry_price || 0);
        const current = cachedPrices[sym] || entry;
        const side = (t.side || "long").toUpperCase();
        const mult = side === "SHORT" ? -1 : 1;
        const lev = Number(t.leverage || 1);
        const margin = Number(t.paper_stake || 25);
        const posSize = Number(t.position_size_usd || margin * lev);
        const contracts = Number(t.contracts || 0);
        const qty = Number(t.quantity || 0);

        const upnlPct = entry > 0 ? ((current - entry) / entry * 100 * mult) : 0;
        const upnlUsd = posSize > 0 ? (posSize * upnlPct / 100) : 0;
        const roe = margin > 0 ? (upnlUsd / margin * 100) : 0;

        const sl = Number(t.stop_loss || 0);
        const tp1 = Number(t.tp1 || 0);
        const tp2 = Number(t.tp2 || 0);
        const tp3 = Number(t.tp3 || 0);
        const risk = Math.abs(entry - sl);
        const rMult = risk > 0 ? ((current - entry) * mult / risk) : 0;
        const mfeR = Number(t.mfe_r || 0);

        const mmRate = 0.005;
        const liqPrice = side === "LONG" ? entry * (1 - 1/lev + mmRate) : entry * (1 + 1/lev - mmRate);
        const liqBuffer = Math.abs(entry - liqPrice) / entry * 100;
        const slVsLiq = liqBuffer > 0 ? (Math.abs(entry - sl) / entry * 100 / liqBuffer * 100) : 0;

        const estFees = posSize * 0.0018;
        const tp1Pnl = tp1 > 0 ? ((Math.abs(tp1 - entry) / entry * 100) * posSize / 100 - estFees) : 0;
        const tp2Pnl = tp2 > 0 ? ((Math.abs(tp2 - entry) / entry * 100) * posSize / 100 - estFees) : 0;
        const tp3Pnl = tp3 > 0 ? ((Math.abs(tp3 - entry) / entry * 100) * posSize / 100 - estFees) : 0;
        const slDir = side === "LONG" ? (sl - entry) : (entry - sl);
        const slPnl = sl > 0 ? ((slDir / entry * 100) * posSize / 100 - estFees) : 0;

        const beSet = t.breakeven_set ? 'BE' : '';
        const tp1Hit = t.tp1_hit ? 'TP1' : '';
        const tp2Hit = t.tp2_hit ? 'TP2' : '';

        const borderColor = upnlUsd > 0.5 ? 'var(--green)' : upnlUsd < -0.5 ? 'var(--red)' : 'var(--border)';
        const dur = timeSince(t.entry_time);

        const statusBadges = [beSet, tp1Hit, tp2Hit].filter(Boolean).map(b =>
            `<span style="font-size:.62rem;padding:2px 5px;border-radius:3px;background:var(--green-dim);color:var(--green);font-weight:600">${b}</span>`
        ).join(" ");

        const conf = Number(t.confidence || 0);
        const grade = t.grade || (conf >= 80 ? 'A' : conf >= 60 ? 'B' : 'C');
        const gradeColors = {A:'var(--green)',B:'var(--yellow)',C:'var(--orange)',D:'var(--red)'};
        const reason = t.reason || (t.metadata && t.metadata.reason) || '';
        const rr1 = risk > 0 && tp1 > 0 ? (Math.abs(tp1 - entry) / risk).toFixed(1) : '—';
        const rr2 = risk > 0 && tp2 > 0 ? (Math.abs(tp2 - entry) / risk).toFixed(1) : '—';
        const rr3 = risk > 0 && tp3 > 0 ? (Math.abs(tp3 - entry) / risk).toFixed(1) : '—';
        const regime = (t.metadata && t.metadata.regime) || '';

        const tid = t.trade_id || '';
        return `<div class="trade-card" style="border-color:${borderColor};border-left:3px solid ${borderColor};cursor:${tid?'pointer':'default'}" ${tid?`onclick="showJourney('${tid}')" title="Click to view signal journey"`:''}>
            <div class="tc-header">
                <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
                    <span style="font-weight:800;font-size:1rem">${esc(sym)}</span>
                    <span style="font-size:.55rem;padding:1px 5px;border-radius:3px;background:rgba(0,212,255,.12);color:var(--cyan);font-weight:700;letter-spacing:.5px;border:1px solid rgba(0,212,255,.25)">PAPER</span>
                    <span class="sig-side ${side}" style="font-size:.78rem">${side}</span>
                    <span style="color:var(--yellow);font-weight:700;font-size:.85rem">${lev}x</span>
                    <span style="color:var(--text-muted);font-size:.73rem">${esc(t.setup_type || '')}</span>
                    ${(function(){
                        const tt = t.trade_type || (t.metadata && t.metadata.trade_type) || '';
                        if (!tt) return '';
                        const ttColors = {SCALP:'#f59e0b',INTRADAY:'#6366f1',RUNNER:'#10b981'};
                        const ttBg = {SCALP:'rgba(245,158,11,.12)',INTRADAY:'rgba(99,102,241,.12)',RUNNER:'rgba(16,185,129,.12)'};
                        const ttIcon = {SCALP:'\u26A1',INTRADAY:'\uD83C\uDFAF',RUNNER:'\uD83C\uDFF9'};
                        return `<span style="font-size:.68rem;padding:2px 7px;border-radius:4px;background:${ttBg[tt]||'var(--border)'};color:${ttColors[tt]||'var(--text-muted)'};font-weight:700;letter-spacing:.3px;border:1px solid ${ttColors[tt]||'var(--border)'}30">${ttIcon[tt]||''} ${tt}</span>`;
                    })()}
                    <span style="font-size:.72rem;padding:2px 7px;border-radius:4px;background:rgba(255,255,255,.06);color:${gradeColors[grade]||'var(--text-dim)'};font-weight:800;border:1px solid ${gradeColors[grade]||'var(--border)'}40">${conf}/100 (${grade})</span>
                    ${statusBadges}
                    ${t.metadata && t.metadata.ml_probability != null ? `<span style="font-size:.62rem;padding:2px 6px;border-radius:3px;background:${t.metadata.ml_probability >= 0.6 ? 'var(--green-dim)' : t.metadata.ml_probability >= 0.45 ? 'var(--yellow-dim)' : 'var(--red-dim)'};color:${t.metadata.ml_probability >= 0.6 ? 'var(--green)' : t.metadata.ml_probability >= 0.45 ? 'var(--yellow)' : 'var(--red)'};font-weight:600">ML ${(t.metadata.ml_probability * 100).toFixed(0)}% ${t.metadata.ml_verdict || ''}</span>` : ''}
                    ${regime ? `<span style="font-size:.58rem;padding:2px 5px;border-radius:3px;background:rgba(0,212,255,.08);color:var(--cyan);font-weight:600">${regime}</span>` : ''}
                </div>
                <div style="display:flex;align-items:center;gap:12px">
                    <span style="font-size:1.35rem;font-weight:800" class="${pnlClass(upnlUsd)}">${upnlUsd >= 0 ? '+' : ''}$${upnlUsd.toFixed(2)}</span>
                    <span style="font-size:.82rem;font-weight:600" class="${pnlClass(roe)}">${roe >= 0 ? '+' : ''}${roe.toFixed(1)}% ROE</span>
                    <span style="font-size:.82rem" class="${pnlClass(rMult)}">${rMult >= 0 ? '+' : ''}${rMult.toFixed(2)}R</span>
                    <span style="color:var(--text-muted);font-size:.73rem">${dur}</span>
                    ${(function(){
                        const entryTs = t.entry_time || (t.metadata && t.metadata.entry_time) || t.timestamp;
                        if (!entryTs) return '';
                        const entryDate = new Date(entryTs);
                        const base = sym.split('/')[0];
                        const windowSec = base === 'BTC' ? 27*60 : 12*60;
                        const elapsed = (Date.now() - entryDate.getTime()) / 1000;
                        const remain = Math.max(0, windowSec - elapsed);
                        if (remain <= 0) return '<span style="font-size:.6rem;padding:1px 5px;border-radius:3px;background:rgba(255,59,92,.15);color:var(--red);font-weight:600">SCALP EXPIRED</span>';
                        const mins = Math.floor(remain / 60);
                        const secs = Math.floor(remain % 60);
                        const pct = remain / windowSec * 100;
                        const color = pct > 50 ? 'var(--green)' : pct > 25 ? 'var(--yellow)' : 'var(--red)';
                        return '<span style="font-size:.65rem;padding:2px 6px;border-radius:4px;background:rgba(0,255,157,.08);color:'+color+';font-weight:700;font-family:var(--font-mono);border:1px solid '+color+'30">\u23F1 '+mins+':'+String(secs).padStart(2,'0')+' scalp</span>';
                    })()}
                </div>
            </div>

            ${reason ? `<div style="padding:4px 8px;margin-bottom:4px;background:rgba(0,255,157,.03);border:1px solid var(--border);border-radius:6px;font-size:.7rem;color:var(--text-dim);line-height:1.4"><span class="text-info font-semibold">Setup:</span> ${esc(reason)}</div>` : ''}

            <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:4px">
                <div><div class="tc-label">Margin</div><div class="tc-val">$${margin.toFixed(2)}</div></div>
                <div><div class="tc-label">Position</div><div class="tc-val">$${posSize.toFixed(2)}</div></div>
                <div><div class="tc-label">Contracts</div><div class="tc-val">${contracts} ct (${qty} ${sym.split('/')[0]})</div></div>
            </div>

            <div><div class="tc-label">Entry</div><div class="tc-val">${num(entry, entry > 100 ? 2 : 4)}${(function(){
                const slip = t.metadata && t.metadata.slippage_bps;
                const sigPrice = t.metadata && t.metadata.signal_entry_price;
                let tags = '';
                if (slip != null) tags += '<span class="trade-tag-slip">Slip: ' + Number(slip).toFixed(1) + 'bps</span>';
                else tags += '<span class="trade-tag-slip">Slip: --</span>';
                if (sigPrice != null && entry > 0) {
                    const delta = ((entry - sigPrice) / sigPrice * 10000).toFixed(1);
                    tags += '<span class="trade-tag-fill">Fill: ' + (delta >= 0 ? '+' : '') + delta + 'bps</span>';
                }
                return tags;
            })()}</div></div>
            <div><div class="tc-label">Current</div><div class="tc-val" style="font-size:1.02rem" class="${pnlClass(upnlPct)}">${num(current, current > 100 ? 2 : 4)}</div></div>
            <div><div class="tc-label">uPnL</div><div class="tc-val ${pnlClass(upnlPct)}">${pnlSign(upnlPct)}% | MFE: ${mfeR.toFixed(2)}R</div></div>

            <div class="border-t pt-2">
                <div class="tc-label" style="color:${slPnl >= 0 ? 'var(--green)' : 'var(--red)'}">${slPnl >= 0 ? 'STOP (BE)' : 'STOP LOSS'} <span style="color:var(--text-muted);font-size:.6rem">(${entry > 0 ? (Math.abs(entry - sl) / entry * 100).toFixed(2) : '?'}%)</span></div>
                <div><span style="color:${slPnl >= 0 ? 'var(--green)' : 'var(--red)'};font-weight:600">${num(sl, sl > 100 ? 2 : 4)}</span>
                <span style="color:${slPnl >= 0 ? 'var(--green)' : 'var(--red)'};font-size:.73rem"> ${slPnl >= 0 ? '+' : '-'}$${Math.abs(slPnl).toFixed(2)}</span></div>
            </div>
            <div class="border-t pt-2">
                <div class="tc-label" class="text-success">TP1 (35%) <span style="color:var(--cyan);font-size:.62rem">R:R 1:${rr1}</span></div>
                <div><span class="text-success font-semibold">${num(tp1, tp1 > 100 ? 2 : 4)}</span>
                <span style="color:var(--green);font-size:.73rem"> +$${tp1Pnl.toFixed(2)}</span>
                <span style="color:var(--text-muted);font-size:.62rem"> (+${tp1 > 0 && entry > 0 ? (Math.abs(tp1 - entry) / entry * 100).toFixed(2) : '?'}%)</span></div>
            </div>
            <div class="border-t pt-2">
                <div class="tc-label" class="text-success">TP2 (35%) <span style="color:var(--cyan);font-size:.62rem">R:R 1:${rr2}</span> / TP3 (30%) <span style="color:var(--cyan);font-size:.62rem">1:${rr3}</span></div>
                <div><span class="text-success font-semibold">${num(tp2, tp2 > 100 ? 2 : 4)}</span>
                <span style="color:var(--green);font-size:.73rem"> +$${tp2Pnl.toFixed(2)}</span>
                <span style="color:var(--text-muted);font-size:.68rem"> | ${num(tp3, tp3 > 100 ? 2 : 4)} +$${tp3Pnl.toFixed(2)}</span></div>
            </div>

            <div class="tc-footer">
                <span>Liq: <b style="color:${liqBuffer < 4 ? 'var(--red)' : 'var(--text-secondary)'}">${num(liqPrice, 2)}</b> (${liqBuffer.toFixed(1)}% buffer)</span>
                <span>SL uses: <b style="color:${slVsLiq > 30 ? 'var(--red)' : 'var(--text-secondary)'}">${slVsLiq.toFixed(0)}%</b> of liq buffer</span>
                <span>Fees: ~$${estFees.toFixed(2)}</span>
                <span>Risk: $${Math.abs(slPnl).toFixed(2)}</span>
            </div>
        </div>`;
    }).join("");
}

function updateSignals(signals) {
    const wrap = document.getElementById("signal-feed");
    if (!signals || !Array.isArray(signals) || signals.length === 0) {
        wrap.innerHTML = '<div class="empty">No signals</div>';
        return;
    }
    // Filter: hide REJECT/C grades and non-active pairs
    var activePairs = ["BTC/USDT","ETH/USDT","SOL/USDT","XRP/USDT"];
    var filtered = signals.filter(function(s) {
        var g = s.grade || "";
        var sym = s.symbol || "";
        if (g === "REJECT" || g === "C") return false;
        if (sym && !activePairs.includes(sym)) return false;
        return true;
    });
    if (filtered.length === 0) {
        wrap.innerHTML = '<div class="empty">No qualifying signals (REJECT/C filtered)</div>';
        return;
    }
    wrap.innerHTML = filtered.slice(0, 10).map(s => {
        const side = (s.side || "LONG").toUpperCase();
        const setup = (s.metadata && s.metadata.setup_type) || s.setup_type || s.scanner || (s.metadata && s.metadata.scanner) || (s.metadata && s.metadata.scanner_name) || "--";
        const entry = s.entry_price || s.entry || 0;
        const sl = s.stop_loss || s.sl || 0;
        const tp1 = (s.take_profits && s.take_profits[0]) || s.tp1 || 0;
        const conf = s.confidence != null ? Number(s.confidence) : 0;
        const confClass = conf >= 70 ? "high" : conf >= 45 ? "medium" : "low";
        const confWidth = Math.min(conf, 100);
        return `<div class="sig-card">
            <span class="sig-side ${side}">${side}</span>
            <span class="sig-symbol">${esc(s.symbol || "--")}</span>
            <span class="sig-setup">${esc(setup)}</span>
            ${(function(){const tt=(s.metadata&&s.metadata.trade_type)||'';const c={SCALP:'#f59e0b',INTRADAY:'#6366f1',RUNNER:'#10b981'};return tt?`<span style="font-size:.6rem;padding:1px 5px;border-radius:3px;color:${c[tt]||'#888'};font-weight:700">${tt}</span>`:'';})()}
            <span class="sig-detail">E:${num(entry, 2)} SL:${num(sl, 2)} TP:${num(tp1, 2)}</span>
            <span class="sig-conf">${conf > 0 ? num(conf, 0) + "%" : "--"}</span>
            <span class="cmd-grade" style="font-size:.68rem;padding:2px 6px">${s.grade || "--"}</span>
            ${s.metadata && s.metadata.ml_probability != null ? `<span style="font-size:.62rem;padding:2px 5px;border-radius:3px;background:${s.metadata.ml_probability >= 0.6 ? 'rgba(0,255,157,.1)' : s.metadata.ml_probability >= 0.45 ? 'rgba(255,215,0,.1)' : 'rgba(255,59,92,.1)'};color:${s.metadata.ml_probability >= 0.6 ? 'var(--green)' : s.metadata.ml_probability >= 0.45 ? 'var(--yellow)' : 'var(--red)'};font-weight:600;font-family:var(--font-mono)">ML:${(s.metadata.ml_probability * 100).toFixed(0)}%</span>` : ''}
            <div class="sig-conf-bar ${confClass}" style="width:${confWidth}%"></div>
        </div>`;
    }).join("");
}

function updateSetupLifecycle(status, closed) {
    const grid = document.getElementById("scanner-status-grid");
    const countEl = document.getElementById("scanner-status-count");
    if (!grid) return;

    // Build per-symbol status from status.prices + status.per_symbol + funnel data
    const prices = (status && status.prices) || {};
    // Only show pairs that are actively traded (have real volume/signals)
    const ACTIVE_PAIRS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT"];
    const symbols = Object.keys(prices).filter(s => ACTIVE_PAIRS.includes(s)).sort();
    const perSym = (status && (status.per_symbol || status.symbol_status)) || {};
    const regimes = (status && (status.regimes || status.symbol_regimes)) || {};

    if (countEl) countEl.textContent = symbols.length;
    if (symbols.length === 0) {
        grid.innerHTML = '<div class="empty" style="font-size:11px">No symbols</div>';
        return;
    }

    grid.innerHTML = symbols.map(sym => {
        const base = sym.split("/")[0];
        const price = prices[sym] || 0;
        const symData = perSym[sym] || perSym[base] || {};
        const regime = regimes[sym] || regimes[base] || symData.regime || "";
        const reason = symData.reason || symData.block_reason || "";
        const atrRatio = symData.atr_ratio || 0;
        const lastScanner = symData.last_scanner || "";
        const lastScore = symData.last_score || 0;
        const hasSignal = symData.has_signal || symData.signal || false;

        // Traffic light logic
        let color, bg, border, label, dotColor;
        const regLow = regime.toLowerCase();

        if (hasSignal || lastScore >= 65) {
            // GREEN: actively producing signals
            color = "var(--green)";
            bg = "rgba(0,255,157,.06)";
            border = "rgba(0,255,157,.3)";
            dotColor = "#00ff9d";
            label = lastScanner ? lastScanner + " (" + lastScore + ")" : "SIGNAL";
        } else if (regLow.includes("trending") && !reason.includes("BLOCK")) {
            // AMBER: regime allows trading but no signal yet
            color = "var(--yellow)";
            bg = "rgba(255,215,0,.05)";
            border = "rgba(255,215,0,.2)";
            dotColor = "#ffd700";
            label = regime;
        } else if (regLow === "quiet" || reason.includes("quiet")) {
            // RED: regime blocks all scanners
            color = "var(--red)";
            bg = "rgba(255,59,92,.04)";
            border = "rgba(255,59,92,.15)";
            dotColor = "#ff3b5c";
            label = "quiet";
        } else if (reason.includes("ATR PREFILTER") || reason.includes("extreme")) {
            // RED: ATR too high
            color = "var(--red)";
            bg = "rgba(255,59,92,.04)";
            border = "rgba(255,59,92,.15)";
            dotColor = "#ff3b5c";
            label = "ATR extreme";
        } else if (reason.includes("VWAP")) {
            // AMBER: VWAP noise zone
            color = "var(--yellow)";
            bg = "rgba(255,215,0,.05)";
            border = "rgba(255,215,0,.2)";
            dotColor = "#ffd700";
            label = "VWAP noise";
        } else if (regLow.includes("rang") || regLow.includes("volatile") || regLow.includes("breakout")) {
            // AMBER: special regime
            color = "var(--yellow)";
            bg = "rgba(255,215,0,.05)";
            border = "rgba(255,215,0,.2)";
            dotColor = "#ffd700";
            label = regime || "scanning";
        } else {
            // GREY: unknown / scanning
            color = "var(--text-muted)";
            bg = "rgba(255,255,255,.02)";
            border = "rgba(255,255,255,.06)";
            dotColor = "#5a7090";
            label = reason ? reason.substring(0, 20) : "scanning";
        }

        const dec = price > 100 ? 2 : price > 1 ? 2 : 4;
        const symColor = ({"BTC":"#f7931a","ETH":"#627eea","SOL":"#9945ff","DOGE":"#c3a634","LINK":"#2a5ada","XRP":"#00aae4","ADA":"#0033ad","LTC":"#bfbbbb","DOT":"#e6007a","TAO":"#00d4aa","BNB":"#f3ba2f","AVAX":"#e84142"})[base] || "#9ca3af";

        // Per-symbol stats from closed trades
        var symWins = 0, symTotal = 0;
        if (closed && closed.length > 0) {
            var today = new Date().toISOString().slice(0, 10);
            for (var ci = 0; ci < closed.length; ci++) {
                var ct = closed[ci];
                if (ct.symbol === sym && (ct.exit_time || "").startsWith(today)) {
                    symTotal++;
                    if ((ct.pnl_usd || 0) > 0) symWins++;
                }
            }
        }
        var symWR = symTotal > 0 ? Math.round(symWins / symTotal * 100) : 0;
        var wrColor = symWR >= 60 ? "var(--green)" : symWR >= 40 ? "var(--yellow)" : symTotal > 0 ? "var(--red)" : "var(--text-muted)";
        var regimeDisplay = regime || (window._symRegimes && window._symRegimes[sym]) || label || "scanning";
        if (regimeDisplay === "scanning" && window._symRegimes && window._symRegimes[sym]) {
            regimeDisplay = window._symRegimes[sym];
            // Update colors based on actual regime
            var regC = {"trending_up":"var(--green)","trending_down":"var(--red)","sideways":"var(--yellow)",
                "ranging":"var(--yellow)","breakout":"var(--cyan)","high_volatility":"var(--orange)",
                "volatile":"var(--orange)","quiet":"var(--text-muted)","mean_reversion":"var(--purple)",
                "low_liquidity":"var(--red)"};
            color = regC[regimeDisplay] || color;
            dotColor = regC[regimeDisplay] ? regC[regimeDisplay].replace("var(--","").replace(")","") : dotColor;
        }

        return '<div style="padding:10px;border-radius:8px;background:' + bg + ';border:1px solid ' + border + ';text-align:center;transition:all .3s">' +
            '<div style="display:flex;align-items:center;justify-content:center;gap:5px;margin-bottom:4px">' +
            '<span style="width:8px;height:8px;border-radius:50%;background:' + dotColor + ';box-shadow:0 0 6px ' + dotColor + '40;display:inline-block"></span>' +
            '<span style="font-weight:800;font-size:.85rem;color:' + symColor + '">' + base + '</span>' +
            '<span style="font-size:.62rem;padding:1px 5px;border-radius:3px;background:rgba(255,255,255,.05);color:' + color + ';font-weight:600">' + esc(regimeDisplay) + '</span>' +
            '</div>' +
            '<div style="font-size:.75rem;font-family:var(--font-mono);color:var(--text-secondary);margin-bottom:4px">$' + price.toFixed(dec) + '</div>' +
            '<div style="display:flex;justify-content:center;gap:10px;font-size:.62rem">' +
            '<span style="color:' + wrColor + ';font-weight:700">WR: ' + (symTotal > 0 ? symWR + "%" : "--") + '</span>' +
            '<span class="text-muted">' + symTotal + ' trades</span>' +
            '</div>' +
            '</div>';
    }).join("");
}

function updateFunnel(raw) {
    if (!raw) return;
    const f = raw.funnel || raw;
    document.getElementById("fn-scanned").textContent = f.scanned || 0;
    var fs2=document.getElementById("fn-scanned2");if(fs2)fs2.textContent=f.scanned||0;
    document.getElementById("fn-strong").textContent = f.strong || 0;
    var fp2=document.getElementById("fn-strong2");if(fp2)fp2.textContent=f.strong||0;
    document.getElementById("fn-valid").textContent = f.valid || 0;
    document.getElementById("fn-weak").textContent = f.weak || 0;
    const blocked = (f.blocked_regime || 0) + (f.blocked_cost || 0) + (f.blocked_ev || 0) + (f.blocked_htf || 0);
    document.getElementById("fn-blocked").textContent = blocked;
    var fb2=document.getElementById("fn-blocked2");if(fb2)fb2.textContent=blocked;
}

function updateRecentClosed(closed) {
    const body = document.getElementById("recent-closed-body");
    if (!body) return;
    if (!closed || !Array.isArray(closed) || closed.length === 0) {
        body.innerHTML = '<tr><td colspan="9" class="empty">No closed trades</td></tr>';
        return;
    }
    const sorted = [...closed].sort((a, b) => new Date(b.closed_at || b.exit_time || 0) - new Date(a.closed_at || a.exit_time || 0));
    body.innerHTML = sorted.slice(0, 10).map(t => {
        const pnlUsd = Number(t.pnl_usd || 0);
        const r = Number(t.exit_r || t.r_multiple || t.r || 0);
        const tradeType = t.trade_type || (t.metadata && t.metadata.trade_type) || '';
        const typeColors = {SCALP:'#f59e0b', INTRADAY:'#6366f1', RUNNER:'#10b981'};
        let dur = "--";
        try {
            const et = new Date(t.entry_time);
            const xt = new Date(t.exit_time || t.closed_at);
            const mins = Math.round((xt - et) / 60000);
            dur = mins >= 60 ? Math.floor(mins/60) + "h" + (mins%60) + "m" : mins + "m";
        } catch(e) {}
        const rowBg = pnlUsd > 0 ? "rgba(34,197,94,.03)" : pnlUsd < 0 ? "rgba(239,68,68,.03)" : "";
        const tradeJson = JSON.stringify(t).replace(/'/g, "\\'").replace(/"/g, "&quot;");
        const rowTid = t.trade_id || '';
        return `<tr style="background:${rowBg};cursor:pointer" onclick='openTradeDetail(JSON.parse(this.dataset.trade))' data-trade='${JSON.stringify(t).replace(/'/g, "&#39;")}'>
            <td>${formatTime(t.closed_at || t.exit_time)}</td>
            <td class="font-semibold">${esc(t.symbol || "--")}</td>
            <td><span class="sig-side ${(t.side||"LONG").toUpperCase()}">${(t.side||"--").toUpperCase()}</span></td>
            <td>${tradeType ? `<span style="color:${typeColors[tradeType]||'var(--text-muted)'};font-weight:600;font-size:.72rem">${tradeType}</span>` : '--'}</td>
            <td class="${pnlUsd >= 0 ? 'pnl-pos' : 'pnl-neg'}">${pnlUsd >= 0 ? "+$" : "-$"}${Math.abs(pnlUsd).toFixed(2)}</td>
            <td class="${r >= 0 ? 'pnl-pos' : 'pnl-neg'}">${r >= 0 ? "+" : ""}${r.toFixed(2)}R</td>
            <td>${esc(t.exit_reason || "--")}</td>
            <td class="text-muted">${dur}</td>
            <td>${rowTid ? `<span onclick="event.stopPropagation();showJourney('${rowTid}')" style="cursor:pointer;font-size:.6rem;padding:1px 5px;border-radius:3px;background:rgba(99,102,241,.15);color:#818cf8;border:1px solid rgba(99,102,241,.25)" title="View journey">🔍</span>` : ''}</td>
        </tr>`;
    }).join("");
}

function updateAlerts(alerts) {
    const wrap = document.getElementById("alerts-list");
    if (!wrap) return;
    if (!alerts || !Array.isArray(alerts) || alerts.length === 0) {
        wrap.innerHTML = '<div class="empty">No alerts</div>';
        return;
    }
    wrap.innerHTML = alerts.slice(0, 15).map(a => {
        const lvl = (a.level || "info").toLowerCase();
        const dotColor = lvl === "error" || lvl === "critical" ? "var(--red)" :
                     lvl === "warning" ? "var(--yellow)" : "var(--accent)";
        return `<div class="alert-row">
            <span class="alert-time">${formatTime(a.timestamp || a.time)}</span>
            <span class="alert-level" style="color:${dotColor}">\u25CF</span>
            <span class="alert-msg">${esc(a.message || a.msg || "")}</span>
        </div>`;
    }).join("");
    console.log("HEADER_UPDATE: trkStats=", typeof trkStats, "closed=", typeof closed, closed ? closed.length : 0); try { updateDashboardHeader(trkStats, closed, decision); } catch(e) { console.error("Dashboard header update failed:", e); }
}

// ══════════════════════════════════════════════════════════
// TAB 2: ANALYTICS REFRESH
// ══════════════════════════════════════════════════════════
async function refreshAnalytics() {
    // Update portfolio overview cards
    try {
        var as = await api("/api/tracker/stats");
        if (as) {
            var ab = document.getElementById("an-balance");
            if(ab) ab.textContent = "$" + (as.paper_balance||0).toFixed(2);
            var ar = document.getElementById("an-return");
            if(ar){var ret=((as.paper_balance||1000)-1000)/1000*100;ar.textContent=(ret>=0?"+":"")+ret.toFixed(1)+"%";ar.style.color=ret>=0?"var(--green)":"var(--red)";}
            var aw = document.getElementById("an-wr");
            if(aw) aw.textContent = (as.win_rate||0).toFixed(1)+"%";
            var ap = document.getElementById("an-pf");
            if(ap) ap.textContent = (as.profit_factor||0).toFixed(2);
            var at2 = document.getElementById("an-trades");
            if(at2) at2.textContent = as.total_signals||0;
        }
        // Real account data
        var rs = await api("/api/real/status");
        if (rs) {
            var arb = document.getElementById("an-real-bal");
            if(arb) arb.textContent = "$" + (rs.balance||0).toFixed(2);
            var arp = document.getElementById("an-real-pnl");
            if(arp) {
                var tp2 = (rs.circuit_breaker||{}).total_pnl||0;
                arp.textContent = (tp2>=0?"+":"") + "$" + Math.abs(tp2).toFixed(2);
                arp.style.color = tp2 >= 0 ? "var(--green)" : "var(--red)";
            }
            var art = document.getElementById("an-real-trades");
            if(art) art.textContent = rs.total_closed||0;
        }
    } catch(e){}

    const [closed, rMetrics, exitQ, scannerH, aiData, monitor, trkStats, riskData, heatmapData] = await Promise.all([
        api("/api/tracker/closed"), api("/api/r-metrics"), api("/api/exit-quality"),
        api("/api/scanner-health"), api("/api/ai/insights"), api("/api/monitor/report"),
        api("/api/tracker/stats"), api("/api/risk-metrics"), api("/api/session-heatmap")
    ]);
    updateEquityChart(closed);
    updateRMetrics(rMetrics);
    updateExitQuality(exitQ);
    updateScannerTable(scannerH);
    updateAIInsights(aiData);
    updateSessionAnalysis(monitor, trkStats);
    updatePaperBalance(monitor);
    updateDashboardHeader(trkStats, closed, null);
    updateClosedTrades(closed);
    updateDailyPnl(trkStats);
    updateRiskMetrics(riskData);
    updateSessionHeatmap(heatmapData);
    // New analytics panels
    updatePerScannerPerf(closed);       // #1
    updateScannerDiversity(closed);     // #3
    updateDailyPnlChart(closed);
    updatePnlCalendar(trkStats);
    updateSessionSummary(closed);        // #13
    updateFeeImpact(closed);            // #8
    updateMLAccuracy(closed);           // #5
    // #12 Trade correlation
    const realSt = await api("/api/real/status");
    updateTradeCorrelation(closed, realSt);
    // Risk-return scatter table — REMOVED: risk-return-body element doesn't exist (dead code cleanup)
}

// REMOVED: refreshRiskReturn() — dead code cleaned up 2026-04-12

function updateEquityChart(closed) {
    if (!closed || !Array.isArray(closed) || closed.length === 0) return;
    const sorted = [...closed].sort((a, b) => new Date(a.closed_at || a.exit_time || 0) - new Date(b.closed_at || b.exit_time || 0));
    let equity = 100;
    const labels = [], eqData = [], ddData = [];
    let peak = 100;
    sorted.forEach(t => {
        const pnl = Number(t.pnl_pct || t.pnl || 0);
        equity += pnl;
        peak = Math.max(peak, equity);
        const dd = ((equity - peak) / peak) * 100;
        labels.push(formatTime(t.closed_at || t.exit_time));
        eqData.push(Number(equity.toFixed(2)));
        ddData.push(Number(dd.toFixed(2)));
    });
    const ctx = document.getElementById("equity-chart").getContext("2d");
    if (equityChart) equityChart.destroy();
    equityChart = new Chart(ctx, {
        type: "line",
        data: {
            labels,
            datasets: [
                { label: "Equity", data: eqData, borderColor: "#6394ff", backgroundColor: "rgba(99,148,255,.06)", fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: "y" },
                { label: "Drawdown %", data: ddData, borderColor: "rgba(239,68,68,.5)", backgroundColor: "rgba(239,68,68,.04)", fill: true, tension: 0.3, pointRadius: 0, borderWidth: 1, yAxisID: "y1" }
            ]
        },
        options: {
            responsive: true, maintainAspectRatio: false,
            interaction: { mode: "index", intersect: false },
            plugins: {
                legend: { labels: { color: "#9ba3b5", font: { size: 11 } } },
                tooltip: { backgroundColor: "rgba(22,26,37,.95)", borderColor: "rgba(99,148,255,.15)", borderWidth: 1, titleColor: "#e8ecf4", bodyColor: "#e8ecf4" }
            },
            scales: {
                x: { display: false },
                y: { position: "left", grid: { color: "rgba(255,255,255,.03)" }, ticks: { color: "#5f6a7d", font: { size: 10 } } },
                y1: { position: "right", grid: { display: false }, ticks: { color: "rgba(239,68,68,.35)", font: { size: 10 }, callback: v => v + "%" }, max: 0 }
            }
        }
    });
}

function updateRMetrics(raw) {
    if (!raw) return;
    const m = raw.global || raw;
    document.getElementById("rm-avg-r").textContent = num(m.avg_r, 3);
    document.getElementById("rm-total-r").textContent = num(m.total_r, 2);
    document.getElementById("rm-expectancy").textContent = num(m.expectancy_r || m.expectancy, 3);
    document.getElementById("rm-pf").textContent = num(m.edge_ratio || m.profit_factor, 2);
    document.getElementById("rm-avg-win").textContent = num(m.avg_win_r, 2);
    document.getElementById("rm-avg-loss").textContent = num(m.avg_loss_r, 2);
    const scanners = raw.by_scanner || [];
    let totalTrades = 0, totalWins = 0;
    scanners.forEach(s => { totalTrades += (s.trades || 0); totalWins += (s.wins || 0); });
    const wr = totalTrades > 0 ? (totalWins / totalTrades * 100) : 0;
    document.getElementById("rm-win-rate").textContent = wr > 0 ? num(wr, 1) + "%" : "--";
    document.getElementById("rm-total-trades").textContent = totalTrades || "--";
    document.getElementById("rm-win-rate").className = "stat-value " + (wr >= 50 ? "green" : "red");
    const payoff = Math.abs(m.avg_loss_r) > 0 ? Math.abs(m.avg_win_r / m.avg_loss_r) : 0;
    document.getElementById("rm-payoff").textContent = num(payoff, 2);
    document.getElementById("rm-payoff").className = "stat-value " + (payoff >= 1 ? "green" : "red");
    document.getElementById("rm-best-r").textContent = "+" + num(m.best_r, 2) + "R";
    document.getElementById("rm-worst-r").textContent = num(m.worst_r, 2) + "R";
    document.getElementById("rm-r-std").textContent = num(m.r_std, 2);
    document.getElementById("analytics-trades-count").textContent = totalTrades + " trades recorded";
}

function updateExitQuality(eq) {
    if (!eq || !eq.total) return;
    document.getElementById("eq-mae").textContent = num(eq.avg_mae_r || eq.avg_mae || 0, 3) + "R";
    document.getElementById("eq-mfe").textContent = num(eq.avg_mfe_r || eq.avg_mfe || 0, 3) + "R";
    document.getElementById("eq-efficiency").textContent = num(eq.exit_efficiency || 0, 1) + "%";
}

function updateScannerTable(data) {
    const body = document.getElementById("scanner-body");
    if (!body) return; // Element removed in analytics cleanup
    if (!data || (!Array.isArray(data) && !data.scanners)) {
        body.innerHTML = '<tr><td colspan="7" class="empty">No scanner data</td></tr>';
        return;
    }
    const scanners = Array.isArray(data) ? data : (data.scanners || Object.entries(data).map(([k,v]) => ({name:k,...v})));
    if (scanners.length === 0) {
        body.innerHTML = '<tr><td colspan="7" class="empty">No scanner data</td></tr>';
        return;
    }
    body.innerHTML = scanners.map(s => {
        const wr = s.win_rate != null ? num(s.win_rate, 1) + "%" : "--";
        const statusColor = s.status === "active" ? "var(--green)" : s.status === "reduced" ? "var(--yellow)" : "var(--text-muted)";
        return `<tr>
            <td class="font-semibold">${esc(s.name || s.scanner || "--")}</td>
            <td>${s.trades || 0}</td>
            <td class="${(s.win_rate||0) >= 50 ? 'green' : 'red'}">${wr}</td>
            <td>${num(s.edge_ratio || s.profit_factor, 2)}</td>
            <td>${num(s.expectancy_r || s.expectancy, 3)}</td>
            <td class="${pnlClass(s.total_r)}">${num(s.total_r, 2)}R</td>
            <td style="color:${statusColor}">${s.status || "--"} (${num(s.weight, 1)}x)</td>
        </tr>`;
    }).join("");
}

function updateAIInsights(data) {
    const wrap = document.getElementById("ai-insights");
    if (!wrap) return; // Element removed in analytics cleanup
    if (!data) { wrap.innerHTML = '<div class="empty">No AI data</div>'; return; }
    let html = "";
    const boosted = data.boosted_setups || data.boosted || [];
    const penalized = data.penalized_setups || data.penalized || [];
    const blocked = data.blocked_combos || data.blocked || [];
    if (boosted.length) {
        html += '<div style="margin-bottom:8px"><span class="metric-label">Boosted</span><br>';
        html += boosted.map(s => `<span class="ai-tag ai-boost">${esc(typeof s === "string" ? s : s.name || s.setup || JSON.stringify(s))}</span>`).join("");
        html += "</div>";
    }
    if (penalized.length) {
        html += '<div style="margin-bottom:8px"><span class="metric-label">Penalized</span><br>';
        html += penalized.map(s => `<span class="ai-tag ai-penalize">${esc(typeof s === "string" ? s : s.name || s.setup || JSON.stringify(s))}</span>`).join("");
        html += "</div>";
    }
    if (blocked.length) {
        html += '<div><span class="metric-label">Blocked</span><br>';
        html += blocked.map(s => `<span class="ai-tag ai-block">${esc(typeof s === "string" ? s : JSON.stringify(s))}</span>`).join("");
        html += "</div>";
    }
    wrap.innerHTML = html || '<div class="empty">No adjustments active</div>';
}

function updateSessionAnalysis(report, trkStats) {
    const wrap = document.getElementById("session-analysis");
    if (!wrap) return; // Element removed in analytics cleanup
    if (!report) { wrap.innerHTML = '<div class="empty">No data</div>'; return; }
    let html = "";
    html += `<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin-bottom:16px">
        <div class="stat-card"><div class="stat-value">${report.total_analyzed || 0}</div><div class="stat-label">Total Trades</div></div>
        <div class="stat-card"><div class="stat-value ${(report.overall_wr||0)>=50?'green':'red'}">${num(report.overall_wr,1)}%</div><div class="stat-label">Win Rate</div></div>
        <div class="stat-card"><div class="stat-value ${(report.current_streak||0)>=0?'green':'red'}">${report.current_streak||0}</div><div class="stat-label">Streak</div></div>
        <div class="stat-card"><div class="stat-value ${(report.rolling_20_wr||0)>=50?'green':'red'}">${num(report.rolling_20_wr,1)}%</div><div class="stat-label">Rolling 20 WR</div></div>
        <div class="stat-card"><div class="stat-value ${(report.rolling_20_pnl||0)>=0?'green':'red'}">${num(report.rolling_20_pnl,2)}%</div><div class="stat-label">Rolling 20 PnL</div></div>
        <div class="stat-card"><div class="stat-value" id="session-paper-bal">--</div><div class="stat-label">Paper Balance</div></div>
        <div class="stat-card"><div class="stat-value red">${num(report.max_drawdown,1)}%</div><div class="stat-label">Max Drawdown</div></div>
        <div class="stat-card"><div class="stat-value">${num(report.sharpe_ratio,2)}</div><div class="stat-label">Sharpe</div></div>
    </div>`;
    const sides = report.side_performance || {};
    if (Object.keys(sides).length > 0) {
        html += '<div style="font-size:.7rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:1.2px;margin:12px 0 8px;font-weight:700">Side Performance</div>';
        html += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:8px;margin-bottom:16px">';
        for (const [side, data] of Object.entries(sides)) {
            html += `<div class="stat-card">
                <div class="stat-value ${(data.win_rate||0)>=50?'green':'red'}">${num(data.win_rate,1)}%</div>
                <div class="stat-label">${side.toUpperCase()} (${data.trades})</div>
                <div style="font-size:.68rem;color:var(--text-muted);font-family:var(--font-mono)">PnL: ${num(data.pnl,2)}%</div>
            </div>`;
        }
        html += '</div>';
    }
    const setups = report.setup_performance || [];
    if (setups.length > 0) {
        html += '<div style="font-size:.7rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:1.2px;margin:12px 0 8px;font-weight:700">Setup Performance</div>';
        html += '<table><thead><tr><th>Setup</th><th>Trades</th><th>WR</th><th>PnL</th><th>Avg</th></tr></thead><tbody>';
        setups.forEach(s => {
            html += `<tr><td class="font-semibold">${esc(s.setup)}</td><td>${s.trades}</td>
                <td class="${(s.win_rate||0)>=50?'green':'red'}">${num(s.win_rate,1)}%</td>
                <td class="${(s.pnl||0)>=0?'green':'red'}">${num(s.pnl,3)}%</td>
                <td>${num(s.avg_pnl,3)}%</td></tr>`;
        });
        html += '</tbody></table>';
    }
    const losses = report.loss_causes || [];
    if (losses.length > 0) {
        html += '<div style="font-size:.7rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:1.2px;margin:12px 0 8px;font-weight:700">Top Loss Causes</div>';
        html += '<div style="display:flex;flex-wrap:wrap;gap:6px">';
        losses.slice(0, 6).forEach(l => {
            html += `<span style="background:var(--bg-elevated);padding:4px 10px;border-radius:4px;font-size:.73rem;border:1px solid var(--border)">${esc(l.cause)} <b class="text-danger">(${l.count})</b></span>`;
        });
        html += '</div>';
    }
    const recs = report.recommendations || [];
    if (recs.length > 0) {
        html += '<div style="font-size:.7rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:1.2px;margin:12px 0 8px;font-weight:700">Recommendations</div>';
        recs.forEach(r => {
            const color = r.severity === "high" ? "var(--red)" : r.severity === "medium" ? "var(--yellow)" : "var(--text-muted)";
            html += `<div style="padding:6px 10px;border-left:3px solid ${color};margin-bottom:4px;font-size:.78rem;background:rgba(255,255,255,.015);border-radius:0 4px 4px 0">${esc(r.message)}</div>`;
        });
    }
    wrap.innerHTML = html;
    // Set paper balance from tracker stats
    if (trkStats) {
        const balEl = document.getElementById("session-paper-bal");
        if (balEl) {
            const startBal = trkStats.paper_start_balance || 1000;
            const netPnl = trkStats.paper_pnl_usd || 0;
            const bal = startBal + netPnl;
            balEl.textContent = "$" + bal.toFixed(2);
            balEl.className = netPnl >= 0 ? "green" : "red";
        }
    }
}

async function updatePaperBalance(report) {
    if (!report) return;
    // Use tracker stats as single source of truth for P&L
    let stats = {};
    try { stats = await api("/api/tracker/stats"); } catch(e) {}

    const netPnl = stats.paper_pnl_usd || 0;
    const grossPnl = stats.paper_gross_pnl_usd || 0;
    const totalFees = stats.paper_total_fees_usd || 0;
    const totalTrades = stats.closed || 0;
    const dd = report.current_drawdown || 0;
    const peak = report.peak_balance || 0;

    // Hero: paper mode shows $1000 + PnL, live mode shows exchange balance
    const isPaper = stats.is_paper_mode !== false;
    const startBal = stats.paper_start_balance || 1000;
    const heroVal = isPaper ? (startBal + netPnl) : (stats.exchange_balance || 0);
    const cls = netPnl >= 0 ? "green" : "red";
    const heroEl = document.getElementById("paper-balance-hero");
    heroEl.textContent = "$" + Math.abs(heroVal).toLocaleString("en-US", {minimumFractionDigits:2, maximumFractionDigits:2});
    heroEl.className = cls;
    heroEl.style.fontFamily = "var(--font-mono)";

    // Net P&L
    const netEl = document.getElementById("paper-net-pnl");
    netEl.textContent = "Net P&L: " + (netPnl >= 0 ? "+$" : "-$") + Math.abs(netPnl).toFixed(2);
    netEl.className = cls;

    // Return % against starting balance
    const retEl = document.getElementById("paper-return-pct");
    const returnPct = startBal > 0 ? ((netPnl / startBal) * 100) : 0;
    retEl.textContent = "Return: " + (netPnl >= 0 ? "+" : "") + returnPct.toFixed(2) + "%";
    retEl.className = cls;

    // Drawdown and peak
    document.getElementById("paper-dd").textContent = dd.toFixed(1) + "%";
    document.getElementById("paper-peak").textContent = "$" + peak.toFixed(2);

    // Actual fees from tracker (not estimated)
    document.getElementById("paper-fees").textContent = "$" + totalFees.toFixed(2);

    // Trades per day
    const daily = stats.daily_pnl || {};
    const tradingDays = Math.max(1, Object.keys(daily).length);
    document.getElementById("paper-tpd").textContent = (totalTrades / tradingDays).toFixed(1);
}

function updateDailyPnl(stats) {
    const tbody = document.getElementById("daily-pnl-body");
    if (!tbody) return; // Element removed in analytics cleanup
    if (!stats || !stats.daily_pnl) { tbody.innerHTML = '<tr><td colspan="7" class="empty">No daily data</td></tr>'; return; }
    const daily = stats.daily_pnl;
    const days = Object.keys(daily).sort().reverse();
    if (days.length === 0) { tbody.innerHTML = '<tr><td colspan="7" class="empty">No trades yet</td></tr>'; return; }
    let totalTrades = 0, totalWins = 0, totalGross = 0, totalFees = 0, totalNet = 0;
    let html = "";
    days.forEach(d => {
        const r = daily[d];
        const cls = r.net_pnl >= 0 ? "green" : "red";
        html += `<tr>
            <td class="font-semibold">${d}</td>
            <td>${r.trades}</td>
            <td>${r.wins}</td>
            <td class="${r.wr >= 50 ? 'green' : 'red'}">${r.wr.toFixed(1)}%</td>
            <td>$${r.gross_pnl.toFixed(2)}</td>
            <td class="red">$${r.fees.toFixed(2)}</td>
            <td class="${cls}">$${r.net_pnl >= 0 ? '+' : ''}${r.net_pnl.toFixed(2)}</td>
        </tr>`;
        totalTrades += r.trades; totalWins += r.wins;
        totalGross += r.gross_pnl; totalFees += r.fees; totalNet += r.net_pnl;
    });
    const totalWR = totalTrades > 0 ? (totalWins / totalTrades * 100) : 0;
    html += `<tr style="border-top:2px solid var(--border);font-weight:700">
        <td>TOTAL</td><td>${totalTrades}</td><td>${totalWins}</td>
        <td class="${totalWR >= 50 ? 'green' : 'red'}">${totalWR.toFixed(1)}%</td>
        <td>$${totalGross.toFixed(2)}</td>
        <td class="red">$${totalFees.toFixed(2)}</td>
        <td class="${totalNet >= 0 ? 'green' : 'red'}">$${totalNet >= 0 ? '+' : ''}${totalNet.toFixed(2)}</td>
    </tr>`;
    tbody.innerHTML = html;
}

function updateRiskMetrics(data) {
    if (!data || data.error) return;
    const el = id => document.getElementById(id);
    el("rk-sharpe").textContent = num(data.sharpe, 2);
    el("rk-sharpe").className = "stat-value " + (data.sharpe >= 1 ? "green" : data.sharpe >= 0 ? "yellow" : "red");
    el("rk-sortino").textContent = num(data.sortino, 2);
    el("rk-sortino").className = "stat-value " + (data.sortino >= 1.5 ? "green" : data.sortino >= 0 ? "yellow" : "red");
    el("rk-calmar").textContent = num(data.calmar, 2);
    el("rk-calmar").className = "stat-value " + (data.calmar >= 2 ? "green" : "blue");
    el("rk-maxdd").textContent = num(data.max_drawdown_pct, 2) + "%";
    el("rk-dd-dur").textContent = data.max_dd_duration_trades + " trades";
    el("rk-win-streak").textContent = data.max_win_streak;
    el("rk-loss-streak").textContent = data.max_loss_streak;
    el("rk-pf").textContent = num(data.profit_factor, 2);
    el("rk-pf").className = "stat-value " + (data.profit_factor >= 2 ? "green" : data.profit_factor >= 1 ? "yellow" : "red");
}

function updateSessionHeatmap(data) {
    if (!data || !Array.isArray(data)) return;
    const container = document.getElementById("session-heatmap");
    const maxTrades = Math.max(...data.map(d => d.trades), 1);
    container.innerHTML = data.map(d => {
        const intensity = d.trades / maxTrades;
        const bgColor = d.trades === 0 ? "rgba(255,255,255,.02)" :
            d.wr >= 80 ? `rgba(0,255,157,${0.1 + intensity * 0.4})` :
            d.wr >= 60 ? `rgba(0,212,255,${0.1 + intensity * 0.3})` :
            d.wr >= 40 ? `rgba(255,215,0,${0.1 + intensity * 0.3})` :
            `rgba(255,59,92,${0.1 + intensity * 0.3})`;
        const textColor = d.trades === 0 ? "var(--text-muted)" :
            d.wr >= 80 ? "var(--green)" :
            d.wr >= 60 ? "var(--cyan)" :
            d.wr >= 40 ? "var(--yellow)" : "var(--red)";
        return `<div style="background:${bgColor};border-radius:4px;padding:4px 2px;text-align:center;min-height:48px;display:flex;flex-direction:column;justify-content:center;cursor:default" title="${d.hour}:00 UTC | ${d.trades} trades | WR ${d.wr}% | Avg R ${d.avg_r} | PnL $${d.pnl}">
            <div class="text-xs text-muted">${String(d.hour).padStart(2,'0')}</div>
            <div style="font-size:.75rem;font-weight:700;color:${textColor}">${d.trades > 0 ? d.wr + '%' : '-'}</div>
            <div class="metric-label">${d.trades}t</div>
        </div>`;
    }).join("");
}

let _currentClosedTab = "paper";
let _lastRealStatus = null;  // cache for tab switching
function switchClosedTab(tab) {
    _currentClosedTab = tab;
    document.getElementById("paper-closed-wrap").style.display = tab === "paper" ? "block" : "none";
    document.getElementById("real-closed-wrap").style.display = (tab === "demo" || tab === "real") ? "block" : "none";
    const paperBtn = document.getElementById("tab-paper-closed");
    const demoBtn = document.getElementById("tab-demo-closed");
    if (!demoBtn) return; // GUARD: element may not exist
    const realBtn = document.getElementById("tab-real-closed");
    paperBtn.style.background = tab === "paper" ? "var(--cyan)" : "transparent";
    paperBtn.style.color = tab === "paper" ? "#000" : "var(--text-muted)";
    paperBtn.style.fontWeight = tab === "paper" ? "600" : "400";
    demoBtn.style.background = tab === "demo" ? "rgba(0,212,255,.8)" : "transparent";
    demoBtn.style.color = tab === "demo" ? "#000" : "var(--text-muted)";
    demoBtn.style.fontWeight = tab === "demo" ? "600" : "400";
    realBtn.style.background = tab === "real" ? "var(--red)" : "transparent";
    realBtn.style.color = tab === "real" ? "#fff" : "var(--text-muted)";
    realBtn.style.fontWeight = tab === "real" ? "600" : "400";
    // Re-render the table with correct data for selected tab
    if (_lastRealStatus && (tab === "demo" || tab === "real")) {
        updateRealClosedTrades(_lastRealStatus);
    }
}

function updateRealClosedTrades(realStatus) {
    const body = document.getElementById("real-closed-body");
    if (!body) return;
    // Use the correct trade set based on active tab
    let trades;
    if (_currentClosedTab === "demo") {
        trades = (realStatus && realStatus.demo_trades) || [];
    } else if (_currentClosedTab === "real") {
        trades = (realStatus && realStatus.live_trades) || [];
    } else {
        trades = (realStatus && realStatus.recent_trades) || [];
    }
    const tabLabel = _currentClosedTab === "demo" ? "demo" : "real";
    if (trades.length === 0) {
        body.innerHTML = '<tr><td colspan="15" class="empty">No ' + tabLabel + ' trades closed yet</td></tr>';
        return;
    }
    const badge = document.getElementById("closed-count-badge");
    if (_currentClosedTab !== "paper") badge.textContent = trades.length + " " + tabLabel;
    body.innerHTML = trades.slice().reverse().map(t => {
        const netPnl = t.pnl_usd || 0;
        const margin = t.margin || 0;
        const lev = t.leverage || 1;
        const notional = margin * lev;
        const feeEst = notional * 0.0015;
        const grossPnl = netPnl + feeEst;
        const pnlColor = netPnl >= 0 ? "var(--green)" : "var(--red)";
        const sideColor = t.side === "long" ? "var(--green)" : "var(--red)";
        const dec = (t.symbol || "").includes("BTC") ? 2 : 4;
        const ts = formatTime(t.timestamp || "");
        const slip = t.slippage_bps || 0;
        const slipColor = slip <= 1.5 ? "var(--green)" : slip <= 3 ? "var(--yellow)" : "var(--red)";
        return `<tr>
            <td>${ts}</td>
            <td class="font-semibold">${t.symbol || "?"}</td>
            <td style="color:${sideColor};font-weight:600;text-transform:uppercase">${t.side || "?"}</td>
            <td class="font-mono">${(t.entry_price || 0).toFixed(dec)}</td>
            <td class="font-mono">${(t.exit_price || 0).toFixed(dec)}</td>
            <td style="font-family:var(--font-mono);color:${slipColor}">${slip > 0 ? slip.toFixed(1) : "--"}</td>
            <td class="font-mono">$${margin.toFixed(2)}</td>
            <td class="font-mono">$${notional.toFixed(2)}</td>
            <td>${lev}x</td>
            <td class="font-mono">${grossPnl>=0?"+":""}$${grossPnl.toFixed(2)}</td>
            <td style="font-family:var(--font-mono);color:var(--yellow)">$${feeEst.toFixed(2)}</td>
            <td style="color:${pnlColor};font-weight:600;font-family:var(--font-mono)">${netPnl>=0?"+":""}$${netPnl.toFixed(2)}</td>
            <td style="color:${pnlColor};font-family:var(--font-mono)">${(t.pnl_pct||0)>=0?"+":""}${(t.pnl_pct || 0).toFixed(1)}%</td>
            <td class="text-info">${t.scanner || "--"}</td>
            <td>${t.reason || "?"}</td>
        </tr>`;
    }).join("");
}

function updateClosedTrades(closed) {
    const body = document.getElementById("closed-trades-body");
    if (!closed || !Array.isArray(closed) || closed.length === 0) {
        body.innerHTML = '<tr><td colspan="17" class="empty">No closed trades</td></tr>';
        return;
    }
    const sorted = [...closed].sort((a, b) => new Date(b.closed_at || b.exit_time || 0) - new Date(a.closed_at || a.exit_time || 0));
    _closedTradesCache = sorted.slice(0, 50);
    body.innerHTML = _closedTradesCache.map((t, idx) => {
        const pnl = Number(t.pnl_pct || t.pnl || 0);
        const pnlUsd = Number(t.pnl_usd || 0);
        const r = Number(t.exit_r || t.r_multiple || t.r || 0);
        const mfe = Number(t.mfe_r || 0);
        const lev = t.leverage || "--";
        const tp = [t.tp1_hit?"1":"", t.tp2_hit?"2":"", t.tp3_hit?"3":""].filter(Boolean).join(",") || "\u2014";
        let dur = "--";
        try {
            const et = new Date(t.entry_time);
            const xt = new Date(t.exit_time || t.closed_at);
            const mins = Math.round((xt - et) / 60000);
            dur = mins >= 60 ? Math.floor(mins/60) + "h" + (mins%60) + "m" : mins + "m";
        } catch(e) {}
        const rowBg = pnl > 0 ? "rgba(34,197,94,.03)" : pnl < -0.3 ? "rgba(239,68,68,.03)" : "";
        return `<tr style="background:${rowBg};cursor:pointer" onclick="showTradeDetail(${idx})" title="Click for details">
            <td>${formatTime(t.closed_at || t.exit_time)}</td>
            <td class="font-semibold">${esc(t.symbol || "--")}</td>
            <td><span class="sig-side ${(t.side||"LONG").toUpperCase()}">${(t.side||"--").toUpperCase()}</span></td>
            <td>${(function(){const tt=t.trade_type||(t.metadata&&t.metadata.trade_type)||'';const c={SCALP:'#f59e0b',INTRADAY:'#6366f1',RUNNER:'#10b981'};return tt?`<span style="color:${c[tt]||'var(--text-muted)'};font-weight:600;font-size:.72rem">${tt}</span>`:'--';})()}</td>
            <td>${esc(t.setup_type || t.scanner || "--")}</td>
            <td class="text-warning">${lev}x</td>
            <td>${num(t.entry_price || t.entry, t.entry_price > 100 ? 2 : 4)}</td>
            <td>${num(t.exit_price || t.exit, t.exit_price > 100 ? 2 : 4)}</td>
            <td style="color:${(t.slippage_bps||0) <= 1.5 ? 'var(--green)' : (t.slippage_bps||0) <= 3 ? 'var(--yellow)' : 'var(--red)'};font-family:var(--font-mono);font-size:.72rem">${t.slippage_bps != null ? Number(t.slippage_bps).toFixed(1) + 'bp' : '--'}</td>
            <td class="${pnlClass(pnl)}" class="font-semibold">${pnlSign(pnl)}%</td>
            <td class="${pnlClass(pnlUsd)}" class="font-bold">${pnlUsd >= 0 ? "+$" : "-$"}${Math.abs(pnlUsd).toFixed(2)}</td>
            <td class="${pnlClass(r)}">${pnlSign(r)}R</td>
            <td style="color:${mfe > 1 ? 'var(--green)' : 'var(--text-muted)'}">${mfe.toFixed(2)}R</td>
            <td>${t.metadata && t.metadata.ml_probability != null ? `<span style="color:${t.metadata.ml_probability >= 0.6 ? 'var(--green)' : t.metadata.ml_probability >= 0.45 ? 'var(--yellow)' : 'var(--red)'};font-weight:600;font-family:var(--font-mono)">${(t.metadata.ml_probability * 100).toFixed(0)}%</span>` : '--'}</td>
            <td>${esc(t.exit_reason || "--")}</td>
            <td class="text-muted">${dur}</td>
            <td style="font-size:.73rem">${tp !== "\u2014" ? "TP" + tp : "\u2014"}</td>
        </tr>`;
    }).join("");
}

// ══════════════════════════════════════════════════════════
// TAB: LATENCY ARB REFRESH
// ══════════════════════════════════════════════════════════
async function refreshLatencyArb() {
    const [data, hist, analysis] = await Promise.all([
        api("/api/latency-arb"),
        api(`/api/latency-arb/dislocations?symbol=${encodeURIComponent(laSelectedSymbol)}&n=50`),
        api("/api/latency-arb/analysis")
    ]);

    if (!data) return;

    // Status badge
    const badge = document.getElementById("la-status-badge");
    if (data.active && data.running) {
        badge.textContent = data.measure_only ? "MEASURING" : "LIVE TRADING";
        badge.style.background = data.measure_only ? "rgba(0,212,255,.08)" : "rgba(0,255,157,.08)";
        badge.style.color = data.measure_only ? "var(--cyan)" : "var(--green)";
        badge.style.borderColor = data.measure_only ? "rgba(0,212,255,.2)" : "rgba(0,255,157,.2)";
    } else {
        badge.textContent = data.active ? "PAUSED" : "OFFLINE";
        badge.style.background = "rgba(90,112,144,.08)";
        badge.style.color = "var(--text-muted)";
        badge.style.borderColor = "rgba(90,112,144,.15)";
    }

    // Uptime
    const up = data.uptime_s || 0;
    const um = Math.floor(up / 60), us = Math.floor(up % 60);
    document.getElementById("la-uptime").textContent = `Uptime: ${um}m ${us}s`;

    // Global stats
    document.getElementById("la-binance-msgs").textContent = (data.binance_msgs || 0).toLocaleString();
    document.getElementById("la-delta-msgs").textContent = (data.delta_msgs || 0).toLocaleString();
    document.getElementById("la-dislocations").textContent = (data.dislocations_detected || 0).toLocaleString();
    document.getElementById("la-signals").textContent = (data.signals_generated || 0).toLocaleString();

    // Per-symbol cards
    const symbols = data.symbols || [];
    const cardsWrap = document.getElementById("la-symbol-cards");
    if (symbols.length === 0) {
        cardsWrap.innerHTML = '<div class="empty">No exchange data yet — engine starting...</div>';
    } else {
        cardsWrap.innerHTML = symbols.map(s => {
            const disl = s.dislocation_pct || 0;
            const absDisl = Math.abs(disl);
            const dir = s.direction || "FLAT";
            const dirColor = dir === "LONG" ? "var(--green)" : dir === "SHORT" ? "var(--red)" : "var(--text-muted)";
            const edgeBg = absDisl >= 0.20 ? "rgba(0,255,157,.06)" : absDisl >= 0.10 ? "rgba(255,215,0,.05)" : "rgba(255,255,255,.02)";
            const edgeBorder = absDisl >= 0.20 ? "rgba(0,255,157,.25)" : absDisl >= 0.10 ? "rgba(255,215,0,.2)" : "var(--border)";
            const shortSym = (s.symbol || "").replace("/USDT", "");

            const costPct = data.cost_rt_pct || 0.14;
            const netEdge = absDisl - costPct;
            const netColor = netEdge > 0 ? "var(--green)" : "var(--red)";

            return `<div style="background:${edgeBg};border:1px solid ${edgeBorder};border-radius:var(--radius-lg);padding:14px;transition:all .3s">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
                    <div class="flex items-center gap-2">
                        <span style="font-size:1.1rem;font-weight:900;color:var(--text)">${esc(shortSym)}</span>
                        <span style="font-size:.65rem;padding:2px 8px;border-radius:4px;font-weight:700;color:${dirColor};background:${dir==='LONG'?'rgba(0,255,157,.08)':dir==='SHORT'?'rgba(255,59,92,.08)':'rgba(90,112,144,.06)'}">${dir}</span>
                    </div>
                    <div style="text-align:right">
                        <div style="font-size:1.3rem;font-weight:900;font-family:var(--font-mono);color:${absDisl>=0.20?'var(--green)':absDisl>=0.10?'var(--yellow)':'var(--text-muted)'}">${disl>=0?'+':''}${disl.toFixed(4)}%</div>
                        <div style="font-size:.62rem;color:var(--text-muted);letter-spacing:.5px">DISLOCATION</div>
                    </div>
                </div>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;font-size:.73rem;font-family:var(--font-mono)">
                    <div style="display:flex;justify-content:space-between;padding:4px 8px;background:rgba(255,255,255,.02);border-radius:4px">
                        <span class="text-muted">Binance</span>
                        <span style="color:var(--cyan);font-weight:600">$${num(s.binance_mid)}</span>
                    </div>
                    <div style="display:flex;justify-content:space-between;padding:4px 8px;background:rgba(255,255,255,.02);border-radius:4px">
                        <span class="text-muted">Delta</span>
                        <span style="color:var(--purple);font-weight:600">$${num(s.delta_mid)}</span>
                    </div>
                    <div style="display:flex;justify-content:space-between;padding:4px 8px;background:rgba(255,255,255,.02);border-radius:4px">
                        <span class="text-muted">Δ USD</span>
                        <span style="font-weight:600;color:${dirColor}">$${num(s.dislocation_usd)}</span>
                    </div>
                    <div style="display:flex;justify-content:space-between;padding:4px 8px;background:rgba(255,255,255,.02);border-radius:4px">
                        <span class="text-muted">Net Edge</span>
                        <span style="font-weight:700;color:${netColor}">${netEdge>=0?'+':''}${netEdge.toFixed(3)}%</span>
                    </div>
                    <div style="display:flex;justify-content:space-between;padding:4px 8px;background:rgba(255,255,255,.02);border-radius:4px">
                        <span class="text-muted">Spread</span>
                        <span>${num(s.spread_delta_pct,3)}%</span>
                    </div>
                    <div style="display:flex;justify-content:space-between;padding:4px 8px;background:rgba(255,255,255,.02);border-radius:4px">
                        <span class="text-muted">Latency</span>
                        <span>${num(s.avg_latency_ms,0)}ms</span>
                    </div>
                </div>
                <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-top:8px;font-size:.65rem;text-align:center">
                    <div style="padding:4px;background:rgba(255,255,255,.015);border-radius:4px">
                        <div style="font-weight:700;font-family:var(--font-mono);color:var(--yellow)">${num(s.avg_disl,3)}%</div>
                        <div class="text-muted">AVG</div>
                    </div>
                    <div style="padding:4px;background:rgba(255,255,255,.015);border-radius:4px">
                        <div style="font-weight:700;font-family:var(--font-mono);color:var(--orange)">${num(s.p95_disl,3)}%</div>
                        <div class="text-muted">P95</div>
                    </div>
                    <div style="padding:4px;background:rgba(255,255,255,.015);border-radius:4px">
                        <div style="font-weight:700;font-family:var(--font-mono);color:${(s.tradeable_pct||0)>=5?'var(--green)':'var(--red)'}">${num(s.tradeable_pct,1)}%</div>
                        <div class="text-muted">TRADEABLE</div>
                    </div>
                </div>
            </div>`;
        }).join("");
    }

    // Symbol tabs for history
    const tabsWrap = document.getElementById("la-sym-tabs");
    tabsWrap.innerHTML = symbols.map(s => {
        const sym = s.symbol || "";
        const short = sym.replace("/USDT","");
        const isActive = sym === laSelectedSymbol;
        return `<button onclick="laSelectSymbol('${sym}')" style="padding:4px 12px;border-radius:4px;border:1px solid ${isActive?'var(--cyan)':'var(--border)'};background:${isActive?'rgba(0,212,255,.1)':'rgba(255,255,255,.02)'};color:${isActive?'var(--cyan)':'var(--text-muted)'};font-size:.72rem;font-weight:600;cursor:pointer;font-family:var(--font-sans)">${short}</button>`;
    }).join("");
    document.getElementById("la-history-sym").textContent = laSelectedSymbol.replace("/USDT","");

    // History table
    const tbody = document.getElementById("la-history-body");
    const disls = hist?.dislocations || [];
    if (disls.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" class="empty">No dislocation data yet</td></tr>';
    } else {
        tbody.innerHTML = disls.map(d => {
            const absD = Math.abs(d.disl_pct);
            const rowBg = absD >= 0.20 ? "rgba(0,255,157,.04)" : absD >= 0.10 ? "rgba(255,215,0,.02)" : "";
            const dirCol = d.direction === "long" ? "var(--green)" : "var(--red)";
            return `<tr style="background:${rowBg}">
                <td>${esc(d.time)}</td>
                <td class="text-info">$${num(d.binance)}</td>
                <td class="text-purple">$${num(d.delta)}</td>
                <td style="font-weight:700;color:${absD>=0.20?'var(--green)':absD>=0.10?'var(--yellow)':'var(--text-muted)'}">${d.disl_pct>=0?'+':''}${d.disl_pct.toFixed(4)}%</td>
                <td style="color:${dirCol};font-weight:600">${d.direction.toUpperCase()}</td>
                <td>${num(d.latency_ms,0)}ms</td>
            </tr>`;
        }).join("");
    }

    // Net Edge Analysis (per-pair with classification)
    const edgeWrap = document.getElementById("la-edge-analysis");
    if (symbols.length > 0) {
        let edgeHtml = '';
        symbols.forEach(s => {
            const short = (s.symbol||"").replace("/USDT","");
            const ne = s.net_edge || {};
            const cls = ne.classification || "NO_TRADE";
            const clsColor = cls === "EXECUTABLE" ? "var(--green)" : cls === "WATCH" ? "var(--yellow)" : "var(--text-muted)";
            const clsBg = cls === "EXECUTABLE" ? "rgba(0,255,157,.06)" : cls === "WATCH" ? "rgba(255,215,0,.04)" : "rgba(255,255,255,.02)";
            const netEdge = ne.net_edge_pct || 0;
            const dailySigs = ((s.tradeable_pct || 0) / 100) * (data.binance_msgs || 0) / Math.max(1, (data.uptime_s || 1) / 86400);
            edgeHtml += `<div style="margin-top:8px;padding:10px;background:${clsBg};border-radius:6px;border:1px solid var(--border);border-left:3px solid ${clsColor}">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
                    <span style="font-weight:700;font-size:.85rem">${short}</span>
                    <span style="font-size:.65rem;padding:2px 8px;border-radius:4px;font-weight:700;color:${clsColor};background:${cls==='EXECUTABLE'?'rgba(0,255,157,.1)':cls==='WATCH'?'rgba(255,215,0,.08)':'rgba(90,112,144,.08)'};border:1px solid ${clsColor}">${cls}</span>
                </div>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;font-size:.72rem;font-family:var(--font-mono)">
                    <span class="text-muted">Gross Disl:</span><span>${num(ne.gross_disl_pct,4)}%</span>
                    <span class="text-muted">Spread:</span><span class="text-warning">${num(ne.spread_pct,4)}%</span>
                    <span class="text-muted">Total Cost:</span><span class="text-danger">${num(ne.total_cost_pct,4)}%</span>
                    <span class="text-muted">Net Edge:</span><span style="font-weight:700;color:${netEdge>0?'var(--green)':'var(--red)'}"> ${netEdge>0?'+':''}${num(netEdge,4)}%</span>
                    <span class="text-muted">Safety Buffer:</span><span>${num(ne.safety_buffer,2)}%</span>
                    <span class="text-muted">Est. Sigs/Day:</span><span class="text-info">${num(dailySigs,0)}</span>
                </div>
            </div>`;
        });
        edgeWrap.innerHTML = edgeHtml;
    }

    // Distribution
    const distWrap = document.getElementById("la-disl-distribution");
    if (symbols.length > 0 && disls.length > 10) {
        const allDisls = disls.map(d => Math.abs(d.disl_pct));
        const buckets = [0.05, 0.10, 0.15, 0.20, 0.30, 0.50, 1.00];
        let distHtml = '<div style="font-size:.72rem">';
        buckets.forEach(b => {
            const count = allDisls.filter(d => d >= b).length;
            const pct = (count / allDisls.length * 100);
            const barW = Math.min(pct, 100);
            const barColor = b >= 0.20 ? "var(--green)" : b >= 0.10 ? "var(--yellow)" : "var(--text-muted)";
            distHtml += `<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
                <span style="width:55px;text-align:right;color:var(--text-muted);font-family:var(--font-mono)">≥${b.toFixed(2)}%</span>
                <div style="flex:1;height:18px;background:rgba(255,255,255,.03);border-radius:3px;overflow:hidden">
                    <div style="height:100%;width:${barW}%;background:${barColor};border-radius:3px;transition:width .5s"></div>
                </div>
                <span style="width:50px;font-family:var(--font-mono);font-weight:600;color:${barColor}">${pct.toFixed(1)}%</span>
            </div>`;
        });
        distHtml += '</div>';
        distWrap.innerHTML = distHtml;
    }

    // ── ANALYSIS LAYERS (from /api/latency-arb/analysis) ──
    if (analysis && analysis.active) {
        // Layer 2: Decay Analysis
        const decayWrap = document.getElementById("la-decay-analysis");
        const decay = analysis.decay || {};
        const decaySymbols = Object.keys(decay);
        if (decaySymbols.length > 0) {
            let dh = '<table><thead><tr><th>Pair</th><th>Spikes</th><th>Avg Dur</th><th>1s</th><th>2s</th><th>5s</th><th>10s</th><th>Avg Peak</th></tr></thead><tbody>';
            decaySymbols.forEach(sym => {
                const d = decay[sym];
                if (!d || !d.total_spikes) return;
                const short = sym.replace("/USDT","");
                const s1 = (d.survived_1s_pct||0).toFixed(0);
                const s2 = (d.survived_2s_pct||0).toFixed(0);
                const s5 = (d.survived_5s_pct||0).toFixed(0);
                const s10 = (d.survived_10s_pct||0).toFixed(0);
                dh += `<tr>
                    <td class="font-bold">${short}</td>
                    <td>${d.total_spikes}</td>
                    <td>${num(d.avg_duration_s,1)}s</td>
                    <td style="color:${s1>50?'var(--green)':'var(--red)'}">${s1}%</td>
                    <td style="color:${s2>50?'var(--green)':'var(--red)'}">${s2}%</td>
                    <td style="color:${s5>30?'var(--green)':'var(--red)'}">${s5}%</td>
                    <td style="color:${s10>20?'var(--green)':'var(--yellow)'}">${s10}%</td>
                    <td class="text-info">${num(d.avg_peak_disl_pct,3)}%</td>
                </tr>`;
            });
            dh += '</tbody></table>';
            dh += '<div style="font-size:.62rem;color:var(--text-muted);margin-top:6px">Survival rate = % of spikes still above threshold at N seconds. Higher = more time to act.</div>';
            decayWrap.innerHTML = dh;
        }

        // Layer 3: Convergence
        const convWrap = document.getElementById("la-convergence");
        const conv = analysis.convergence || {};
        const convSymbols = Object.keys(conv);
        if (convSymbols.length > 0) {
            let ch = '';
            convSymbols.forEach(sym => {
                const c = conv[sym];
                if (!c || !c.total_resolved) return;
                const short = sym.replace("/USDT","");
                const dcPct = (c.delta_converged_pct||0);
                const brPct = (c.binance_reverted_pct||0);
                const mxPct = (c.mixed_pct||0);
                const dcW = Math.min(dcPct, 100);
                const brW = Math.min(brPct, 100);
                ch += `<div style="margin-bottom:12px;padding:8px;background:rgba(255,255,255,.02);border-radius:6px;border:1px solid var(--border)">
                    <div style="font-weight:700;font-size:.82rem;margin-bottom:6px">${short} <span style="color:var(--text-muted);font-weight:400">(${c.total_resolved} resolved)</span></div>
                    <div style="margin-bottom:4px;display:flex;align-items:center;gap:8px;font-size:.72rem">
                        <span style="width:90px;color:var(--green)">Delta→Binance</span>
                        <div style="flex:1;height:14px;background:rgba(255,255,255,.03);border-radius:3px;overflow:hidden">
                            <div style="height:100%;width:${dcW}%;background:var(--green);border-radius:3px"></div>
                        </div>
                        <span style="width:40px;font-family:var(--font-mono);color:var(--green);font-weight:600">${dcPct.toFixed(0)}%</span>
                    </div>
                    <div style="display:flex;align-items:center;gap:8px;font-size:.72rem">
                        <span style="width:90px;color:var(--red)">Binance←revert</span>
                        <div style="flex:1;height:14px;background:rgba(255,255,255,.03);border-radius:3px;overflow:hidden">
                            <div style="height:100%;width:${brW}%;background:var(--red);border-radius:3px"></div>
                        </div>
                        <span style="width:40px;font-family:var(--font-mono);color:var(--red);font-weight:600">${brPct.toFixed(0)}%</span>
                    </div>
                    <div style="font-size:.65rem;color:var(--text-muted);margin-top:4px">Avg convergence: ${num(c.avg_convergence_pct,1)}% of gap closed</div>
                </div>`;
            });
            convWrap.innerHTML = ch || '<div class="empty">No resolved spikes yet</div>';
        }

        // Layer 4: Simulation
        const simWrap = document.getElementById("la-simulation");
        const sim = analysis.simulation || {};
        const simSymbols = Object.keys(sim);
        if (simSymbols.length > 0) {
            let sh = '';
            simSymbols.forEach(sym => {
                const s = sim[sym];
                if (!s || !s.total_trades) return;
                const short = sym.replace("/USDT","");
                const wr = s.win_rate || 0;
                const netPnl = s.total_net_pnl || 0;
                sh += `<div style="margin-bottom:10px;padding:10px;background:rgba(255,255,255,.02);border-radius:6px;border:1px solid var(--border);border-left:3px solid ${netPnl>=0?'var(--green)':'var(--red)'}">
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
                        <span style="font-weight:700;font-size:.85rem">${short}</span>
                        <span style="font-size:.72rem;color:var(--text-muted)">${s.total_trades} simulated</span>
                    </div>
                    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:6px;text-align:center;font-size:.72rem">
                        <div><div style="font-weight:800;font-family:var(--font-mono);color:${wr>=50?'var(--green)':'var(--red)'}">${wr.toFixed(1)}%</div><div class="text-muted">Win Rate</div></div>
                        <div><div style="font-weight:800;font-family:var(--font-mono);color:${netPnl>=0?'var(--green)':'var(--red)'}">${netPnl>=0?'+':''}${netPnl.toFixed(3)}%</div><div class="text-muted">Total P&L</div></div>
                        <div><div style="font-weight:800;font-family:var(--font-mono)">${num(s.avg_net_pnl,3)}%</div><div class="text-muted">Avg Trade</div></div>
                        <div><div style="font-weight:800;font-family:var(--font-mono)">${num(s.avg_hold_time_s,1)}s</div><div class="text-muted">Avg Hold</div></div>
                    </div>
                </div>`;
            });
            simWrap.innerHTML = sh || '<div class="empty">No simulated trades yet</div>';
        }

        // Layer 5: Session Heatmap (Net Edge by Pair × UTC Hour)
        const sessWrap = document.getElementById("la-session-heatmap");
        const sess = analysis.session || {};
        const sessSymbols = Object.keys(sess);
        if (sessSymbols.length > 0) {
            // Build heatmap: rows = pairs, cols = UTC hours 0-23
            const hours = Array.from({length:24}, (_,i)=>i);
            let hh = '<div style="overflow-x:auto"><table style="font-size:.65rem;min-width:700px"><thead><tr><th style="position:sticky;left:0;background:var(--bg);z-index:1">Pair</th>';
            hours.forEach(h => {
                hh += `<th style="text-align:center;min-width:28px;padding:2px 3px">${h}</th>`;
            });
            hh += '</tr></thead><tbody>';
            sessSymbols.forEach(sym => {
                const sd = sess[sym];
                if (!sd || !sd.by_hour) return;
                const short = sym.replace("/USDT","");
                hh += `<tr><td style="font-weight:700;position:sticky;left:0;background:var(--bg);z-index:1">${short}</td>`;
                hours.forEach(h => {
                    const hd = sd.by_hour[String(h)] || {};
                    const avgD = hd.avg_disl || 0;
                    const count = hd.count || 0;
                    const tPct = hd.pct_tradeable || 0;
                    // Color: green if avg_disl > cost (profitable), yellow if close, dim if no data
                    let bg = "transparent";
                    let color = "var(--text-muted)";
                    if (count === 0) {
                        bg = "rgba(255,255,255,.01)";
                        color = "var(--text-muted)";
                    } else if (avgD >= 0.15) {
                        bg = `rgba(0,255,157,${Math.min(avgD/0.5,0.3).toFixed(2)})`;
                        color = "var(--green)";
                    } else if (avgD >= 0.08) {
                        bg = `rgba(255,215,0,${Math.min(avgD/0.3,0.15).toFixed(2)})`;
                        color = "var(--yellow)";
                    } else {
                        bg = "rgba(255,255,255,.02)";
                    }
                    const title = `${short} UTC ${h}:00\\nAvg: ${avgD.toFixed(3)}%\\nCount: ${count}\\nTradeable: ${tPct.toFixed(1)}%`;
                    hh += `<td style="text-align:center;background:${bg};color:${color};font-family:var(--font-mono);font-weight:600;padding:4px 2px;border:1px solid rgba(255,255,255,.02)" title="${title}">${count>0?avgD.toFixed(2):'-'}</td>`;
                });
                hh += '</tr>';
            });
            hh += '</tbody></table></div>';
            hh += '<div style="display:flex;gap:12px;margin-top:8px;font-size:.6rem;color:var(--text-muted)">';
            hh += '<span>■ <span class="text-success">Green</span> = avg disl ≥ 0.15% (likely profitable)</span>';
            hh += '<span>■ <span class="text-warning">Yellow</span> = 0.08-0.15% (watch)</span>';
            hh += '<span>■ Dim = < 0.08% (no edge)</span>';
            hh += '</div>';

            // Add active/quiet hour summary
            sessSymbols.forEach(sym => {
                const sd = sess[sym];
                if (!sd) return;
                const short = sym.replace("/USDT","");
                const active = sd.active_hours || [];
                const quiet = sd.quiet_hours || [];
                if (active.length > 0 || quiet.length > 0) {
                    hh += `<div style="margin-top:6px;font-size:.68rem">
                        <span class="font-semibold">${short}:</span>
                        ${active.length > 0 ? `<span class="text-success"> Active: UTC ${active.join(', ')}</span>` : ''}
                        ${quiet.length > 0 ? `<span class="text-muted"> | Quiet: UTC ${quiet.join(', ')}</span>` : ''}
                    </div>`;
                }
            });
            sessWrap.innerHTML = hh;
        }
    }
}

function laSelectSymbol(sym) {
    laSelectedSymbol = sym;
    refreshLatencyArb();
}

// ══════════════════════════════════════════════════════════
// TAB 3: SYSTEM REFRESH
// ══════════════════════════════════════════════════════════
async function refreshSystem() {
    // Each fetch independent — one failure can't block others
    var infra = null, status = null, ml4 = null;
    try { infra = await fetch("/api/infra").then(function(r){return r.json();}).catch(function(){return null;}); } catch(e){}
    try { status = await fetch("/api/status").then(function(r){return r.json();}).catch(function(){return null;}); } catch(e){}
    try { ml4 = await fetch("/api/ml/health").then(function(r){return r.json();}).catch(function(){return null;}); } catch(e){}

    try { updateVMHealth(infra, ml4); } catch(e) { console.error("updateVMHealth:", e); }
    try { updateExchangeStatus(status); } catch(e) { console.error("updateExchangeStatus:", e); }
    try { updateBotEngine(status); } catch(e) { console.error("updateBotEngine:", e); }
    try { updateConfigSnapshot(status); } catch(e) { console.error("updateConfigSnapshot:", e); }
    try { await refreshGridBot(); } catch(e) {}
    try { await refreshBrainTab(); } catch(e) { console.error("brainTab:", e); }
    try { await refreshInfraHealth(); } catch(e) { console.error("infraHealth:", e); }
}

// Track B.8: VM1 (bot host) + VM4 (ML server) side-by-side health cards.
function updateVMHealth(infra, ml4) {
    const wrap = document.getElementById("vm-health");
    if (!infra && !ml4) { wrap.innerHTML = '<div class="empty">No infra data</div>'; return; }
    const cpuPct = (infra && (infra.cpu?.percent || infra.cpu_percent)) || 0;
    const memPct = (infra && (infra.memory?.percent || infra.memory_percent)) || 0;
    const memUsed = (infra && infra.memory?.used_mb) || 0;
    const memTotal = (infra && infra.memory?.total_mb) || 0;
    const swapUsed = (infra && infra.memory?.swap_used_mb) || 0;
    const swapTotal = (infra && infra.memory?.swap_total_mb) || 0;
    const swapPct = swapTotal > 0 ? (swapUsed / swapTotal * 100) : 0;
    const diskPct = (infra && (infra.disk?.percent || infra.disk_percent)) || 0;
    const diskUsed = (infra && infra.disk?.used_gb) || 0;
    const diskTotal = (infra && infra.disk?.total_gb) || 0;
    const uptime = (infra && (infra.os_uptime || infra.uptime)) || "--";
    const shape = (infra && infra.shape) || "--";

    // VM1 — bot host
    let vm1 = '<div style="border-left:3px solid var(--green);padding:6px 8px;margin-bottom:8px">';
    vm1 += '<div style="display:flex;justify-content:space-between;font-size:.7rem;font-weight:700;margin-bottom:6px"><span class="text-success">🟢 VM1 · bot host</span><span style="color:var(--text-muted);font-family:var(--font-mono);font-size:.6rem">150.230.171.48</span></div>';
    vm1 += makeHealthRow("CPU", cpuPct, "%", 100);
    vm1 += makeHealthRow("Memory", memPct, `% (${memUsed}/${memTotal} MB)`, 100);
    if (swapTotal > 0) vm1 += makeHealthRow("Swap", swapPct, `% (${swapUsed}/${swapTotal} MB)`, 100);
    vm1 += makeHealthRow("Disk", diskPct, `% (${diskUsed}/${diskTotal} GB)`, 100);
    vm1 += makeKV("Uptime", uptime);
    vm1 += makeKV("Shape", shape);
    vm1 += '</div>';

    // VM4 — ML server
    let vm4 = '';
    if (ml4 && (ml4.ok || ml4.status === "ok" || ml4.healthy)) {
        const ml_uptime = ml4.uptime_sec ? (Math.floor(ml4.uptime_sec / 60) + "m") : (ml4.uptime || "--");
        const ml_mem = ml4.memory_mb || ml4.mem_mb || "--";
        const ml_cpu = ml4.cpu_percent || ml4.cpu || "--";
        const ml_models = ml4.models_loaded || ml4.model_count || "--";
        const ml_latency = ml4.avg_score_latency_ms || ml4.latency_ms || "--";
        vm4 = '<div style="border-left:3px solid var(--purple);padding:6px 8px">';
        vm4 += '<div style="display:flex;justify-content:space-between;font-size:.7rem;font-weight:700;margin-bottom:6px"><span class="text-purple">🟢 VM4 · ML server</span><span style="color:var(--text-muted);font-family:var(--font-mono);font-size:.6rem">10.0.2.4:8081</span></div>';
        if (typeof ml_cpu === "number") vm4 += makeHealthRow("CPU", ml_cpu, "%", 100);
        if (typeof ml_mem === "number") vm4 += makeKV("Memory", ml_mem + " MB");
        vm4 += makeKV("Models Loaded", ml_models);
        vm4 += makeKV("Avg Score Latency", typeof ml_latency === "number" ? ml_latency.toFixed(1) + " ms" : ml_latency);
        vm4 += makeKV("Uptime", ml_uptime);
        vm4 += '</div>';
    } else {
        vm4 = '<div style="border-left:3px solid var(--red);padding:6px 8px">';
        vm4 += '<div style="display:flex;justify-content:space-between;font-size:.7rem;font-weight:700;margin-bottom:6px"><span class="text-danger">🔴 VM4 · ML server</span><span style="color:var(--text-muted);font-family:var(--font-mono);font-size:.6rem">10.0.2.4:8081</span></div>';
        vm4 += '<div style="font-size:.65rem;color:var(--text-muted);padding:6px 0">Proxy unreachable — check /api/ml/health from bot host</div>';
        vm4 += '</div>';
    }

    wrap.innerHTML = vm1 + vm4;
}

function updateExchangeStatus(status) {
    const wrap = document.getElementById("exchange-status");
    if (!status) { wrap.innerHTML = '<div class="empty">No status data</div>'; return; }
    const connected = status.exchange_status === "connected";
    const fees = status.fees || {};
    const makerPct = fees.maker != null ? (fees.maker * 100).toFixed(2) + "%" : "--";
    const takerPct = fees.taker != null ? (fees.taker * 100).toFixed(2) + "%" : "--";
    const settlePct = fees.settlement != null ? (fees.settlement * 100).toFixed(2) + "%" : "--";
    let html = "";
    html += makeKV("Connection", connected ? "Connected" : "Disconnected", connected ? "var(--green)" : "var(--red)");
    html += makeKV("Balance", "$" + (typeof realStatus !== "undefined" && realStatus && realStatus.balance ? realStatus.balance.toFixed(2) : "--"), "var(--green)");
    html += makeKV("Maker Fee", makerPct);
    html += makeKV("Taker Fee", takerPct);
    html += makeKV("Settlement Fee", settlePct);
    html += makeKV("Total Round-Trip", fees.taker && fees.settlement ? ((fees.taker + fees.taker + fees.settlement) * 100).toFixed(2) + "%" : "--");
    html += makeKV("Latency", (status.exchange_latency_ms || 0) + " ms");
    html += makeKV("Last Data", status.last_data_update || "--");
    wrap.innerHTML = html;
}

function updateBotEngine(status) {
    const wrap = document.getElementById("bot-engine");
    if (!status) { wrap.innerHTML = '<div class="empty">No status data</div>'; return; }
    let html = "";
    html += makeKV("Mode", (status.mode || "--").toUpperCase(), status.mode === "live" ? "var(--green)" : "var(--yellow)");
    html += makeKV("Strategy", status.active_strategy || status.strategy || "--");
    html += makeKV("Symbols", Array.isArray(status.symbols) ? status.symbols.join(", ") : (status.symbols || "--"));
    html += makeKV("Uptime", status.uptime || "--");
    html += makeKV("Memory", (status.memory_mb || status.memory || "--") + " MB");
    html += makeKV("Bot Status", status.bot_status || "--", status.bot_status === "running" ? "var(--green)" : "var(--red)");
    html += makeKV("Paused", status.paused ? "YES" : "No", status.paused ? "var(--red)" : "var(--green)");
    wrap.innerHTML = html;
}

function updateConfigSnapshot(status) {
    const wrap = document.getElementById("config-snapshot");
    if (!status) { wrap.innerHTML = '<div class="empty">No status data</div>'; return; }
    let html = "";
    html += makeKV("Bot Name", status.bot_name || "VN Edge");
    html += makeKV("Version", status.bot_version || "--");
    html += makeKV("Exchange", status.exchange_status || "--", status.exchange_status === "connected" ? "var(--green)" : "var(--red)");
    html += makeKV("Refresh", (status.refresh_interval || 5) + "s");
    html += makeKV("Server Time", formatTime(status.server_time));
    wrap.innerHTML = html || '<div class="empty">No config data</div>';
}

// ── VETO STATS ──────────────────────────────────────────
function updateVetoStats(data) {
    var el = document.getElementById("veto-stats-body");
    var el2 = document.getElementById("veto-stats-body2");
    if (!el) return;
    if (!data || !data.veto_stats) { el.textContent = "None"; return; }
    var vs = data.veto_stats;
    var parts = [];
    for (var k in vs) {
        if (vs[k] > 0) parts.push('<span class="text-danger">' + k + '</span>: ' + vs[k]);
    }
    var vetoHtml = parts.length > 0 ? parts.join(' <span class="text-muted">|</span> ') : '<span class="text-success">None active</span>';
}

function renderVetoRows(wrap, vetoData) {
    const maxCount = Math.max(...vetoData.map(v => v.count), 1);
    wrap.innerHTML = vetoData.map(v => {
        const pct = (v.count / maxCount * 100).toFixed(0);
        return `<div class="veto-row">
            <span class="veto-name">${esc(v.name)}</span>
            <span class="veto-count">${v.count}</span>
            <div class="veto-bar"><div class="veto-bar-fill" style="width:${pct}%"></div></div>
        </div>`;
    }).join("");
}

// ── SPARKLINES ──────────────────────────────────────────
const priceHistory = {};
function updateSparklines(prices) {
    if (!prices) return;
    for (const sym of Object.keys(prices)) {
        const base = sym.split("/")[0];
        if (!priceHistory[base]) priceHistory[base] = [];
        priceHistory[base].push(prices[sym]);
        if (priceHistory[base].length > 20) priceHistory[base].shift();
        renderSparkline(base);
    }
}
function renderSparkline(base) {
    const el = document.getElementById("spark-" + base);
    if (!el) return;
    const data = priceHistory[base];
    if (!data || data.length < 2) return;
    const w = 56, h = 18, pad = 1;
    const min = Math.min(...data);
    const max = Math.max(...data);
    const range = max - min || 1;
    const points = data.map((v, i) => {
        const x = pad + (i / (data.length - 1)) * (w - 2 * pad);
        const y = pad + (1 - (v - min) / range) * (h - 2 * pad);
        return `${x.toFixed(1)},${y.toFixed(1)}`;
    });
    const isUp = data[data.length - 1] >= data[0];
    const color = isUp ? "var(--green)" : "var(--red)";
    const areaPoints = points.join(" ") + ` ${w - pad},${h - pad} ${pad},${h - pad}`;
    el.innerHTML = `<svg viewBox="0 0 ${w} ${h}">
        <polygon class="sparkline-area" points="${areaPoints}" fill="${color}"/>
        <polyline class="sparkline-line" points="${points.join(" ")}" stroke="${color}"/>
    </svg>`;
}

// ── CONTACT FORM (Telegram Webhook) ─────────────────────
function showToast(msg, type) {
    if (!document.getElementById("vn-toast")) return;
    let toast = document.getElementById("vn-toast");
    if (!toast) return; // GUARD: element may not exist
    if (!toast) {
        toast = document.createElement("div");
        toast.id = "vn-toast";
        toast.className = "toast";
        document.body.appendChild(toast);
    }
    toast.textContent = msg;
    toast.className = "toast " + type;
    requestAnimationFrame(() => { toast.classList.add("show"); });
    setTimeout(() => { toast.classList.remove("show"); }, 4000);
}

async function handleContact(e) {
    e.preventDefault();
    const name = document.getElementById("contact-name").value.trim();
    const email = document.getElementById("contact-email").value.trim();
    const message = document.getElementById("contact-message").value.trim();
    const btn = document.getElementById("contact-submit");

    if (!name || !email || !message) {
        showToast("Please fill in all fields.", "error");
        return false;
    }

    btn.disabled = true;
    btn.textContent = "SENDING...";

    const text = `\u{1F4E9} *VN Edge Contact Form*\n\n*Name:* ${name}\n*Email:* ${email}\n*Message:*\n${message}\n\n_Sent from VN Edge Dashboard_`;

    try {
        const r = await fetch("https://api.telegram.org/bot8736266250:AAHqK3pV15VlnkOfDoK3VPh1J2drbp2-kPA/sendMessage", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ chat_id: "-5232134151", text: text, parse_mode: "Markdown" })
        });
        if (r.ok) {
            showToast("Message sent successfully!", "success");
            document.getElementById("contact-form").reset();
        } else {
            showToast("Failed to send message. Please try again.", "error");
        }
    } catch (err) {
        showToast("Network error. Please try again.", "error");
    }

    btn.disabled = false;
    btn.textContent = "SEND MESSAGE";
    return false;
}

// ── TRADE DETAIL MODAL ───────────────────────────────────
let _closedTradesCache = [];
function showTradeDetail(idx) {
    const t = _closedTradesCache[idx];
    if (!t) return;
    const m = document.getElementById("trade-modal");
    const dec = (t.symbol||"").includes("BTC") ? 2 : 4;
    const dur = t.duration_sec ? Math.round(t.duration_sec/60) + "m" : "--";
    const ml = t.metadata && t.metadata.ml_probability != null ? (t.metadata.ml_probability*100).toFixed(0)+"%" : "--";
    const regime = (t.metadata && t.metadata.regime) || "--";
    const exitEff = t.mfe_r > 0 ? ((t.exit_r/t.mfe_r)*100).toFixed(0)+"%" : "--";
    m.innerHTML = `<div style="position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.7);z-index:999;display:flex;align-items:center;justify-content:center" onclick="this.remove()">
    <div style="background:var(--bg);border:1px solid var(--border-active);border-radius:12px;padding:24px;max-width:600px;width:90%;max-height:80vh;overflow-y:auto" onclick="event.stopPropagation()">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
        <h3 style="color:var(--accent);font-size:1rem">${t.symbol} ${(t.side||"").toUpperCase()} — Trade Detail</h3>
        <span onclick="this.closest('[style*=fixed]').remove()" style="cursor:pointer;color:var(--text-muted);font-size:1.2rem">&times;</span>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:.8rem">
        <div class="metric"><span class="label">Entry</span><span class="value">${(t.entry_price||0).toFixed(dec)}</span></div>
        <div class="metric"><span class="label">Exit</span><span class="value">${(t.exit_price||0).toFixed(dec)}</span></div>
        <div class="metric"><span class="label">SL</span><span class="value">${(t.stop_loss||0).toFixed(dec)}</span></div>
        <div class="metric"><span class="label">TP1</span><span class="value">${(t.tp1||0).toFixed(dec)}</span></div>
        <div class="metric"><span class="label">PnL %</span><span class="value ${t.pnl_pct>=0?"positive":"negative"}">${(t.pnl_pct||0).toFixed(2)}%</span></div>
        <div class="metric"><span class="label">PnL $</span><span class="value ${t.pnl_usd>=0?"positive":"negative"}">$${(t.pnl_usd||0).toFixed(2)}</span></div>
        <div class="metric"><span class="label">R Multiple</span><span class="value ${t.exit_r>=0?"positive":"negative"}">${(t.exit_r||0).toFixed(3)}R</span></div>
        <div class="metric"><span class="label">MFE</span><span class="value">${(t.mfe_r||0).toFixed(3)}R</span></div>
        <div class="metric"><span class="label">MAE</span><span class="value">${(t.mae_r||0).toFixed(3)}R</span></div>
        <div class="metric"><span class="label">Exit Efficiency</span><span class="value">${exitEff}</span></div>
        <div class="metric"><span class="label">Duration</span><span class="value">${dur}</span></div>
        <div class="metric"><span class="label">Exit Reason</span><span class="value">${t.exit_reason||"--"}</span></div>
        <div class="metric"><span class="label">Scanner</span><span class="value" class="text-info">${t.setup_type||t.scanner||"--"}</span></div>
        <div class="metric"><span class="label">Trade Type</span><span class="value">${t.trade_type||"--"}</span></div>
        <div class="metric"><span class="label">ML Prob</span><span class="value">${ml}</span></div>
        <div class="metric"><span class="label">Regime</span><span class="value">${regime}</span></div>
        <div class="metric"><span class="label">Leverage</span><span class="value">${t.leverage||"--"}x</span></div>
        <div class="metric"><span class="label">Position</span><span class="value">$${(t.position_size_usd||0).toFixed(0)}</span></div>
        <div class="metric"><span class="label">Fees</span><span class="value negative">$${(t.total_fees_usd||0).toFixed(2)}</span></div>
        <div class="metric"><span class="label">Slippage</span><span class="value">${t.slippage_bps!=null?t.slippage_bps.toFixed(1)+"bp":"--"}</span></div>
      </div>
      <div style="margin-top:12px;font-size:.7rem;color:var(--text-muted)">
        <div>Signal: ${t.reason||"--"}</div>
        <div>ID: ${t.trade_id||"--"}</div>
        <div>Time: ${t.entry_time||"--"} → ${t.exit_time||t.closed_at||"--"}</div>
      </div>
    </div></div>`;
}

// ── CSV EXPORT ───────────────────────────────────────────
function exportCSV() {
    if (!_closedTradesCache.length) { alert("No trades to export"); return; }
    const headers = ["Date","Symbol","Side","Type","Scanner","Lev","Entry","Exit","PnL%","PnL$","R","MFE","MAE","ML","ExitReason","Duration_sec","Slippage_bps","Fees$"];
    const rows = _closedTradesCache.map(t => [
        t.closed_at||t.exit_time||"", t.symbol||"", t.side||"", t.trade_type||"",
        t.setup_type||t.scanner||"", t.leverage||"", t.entry_price||0, t.exit_price||0,
        (t.pnl_pct||0).toFixed(4), (t.pnl_usd||0).toFixed(4), (t.exit_r||0).toFixed(4),
        (t.mfe_r||0).toFixed(4), (t.mae_r||0).toFixed(4),
        t.metadata&&t.metadata.ml_probability!=null?(t.metadata.ml_probability).toFixed(4):"",
        t.exit_reason||"", t.duration_sec||0, (t.slippage_bps||0).toFixed(2), (t.total_fees_usd||0).toFixed(4)
    ]);
    const csv = [headers.join(","), ...rows.map(r=>r.join(","))].join("\n");
    const blob = new Blob([csv], {type:"text/csv"});
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "vn_edge_trades_" + new Date().toISOString().slice(0,10) + ".csv";
    a.click();
}

// ── SIGNAL FILTER ────────────────────────────────────────
let _signalFilter = "ALL";
function filterSignals(sym) {
    _signalFilter = sym;
    document.querySelectorAll(".filter-btn").forEach(b => {
        b.style.background = b.textContent === sym ? "var(--accent-dim)" : "transparent";
        b.style.color = b.textContent === sym ? "var(--accent)" : "var(--text-muted)";
        if (b.textContent === sym) b.classList.add("active");
        else b.classList.remove("active");
    });
    // Re-render signal feed with filter
    const cards = document.querySelectorAll("#signal-feed .signal-card");
    cards.forEach(c => {
        if (sym === "ALL" || c.dataset.symbol && c.dataset.symbol.includes(sym)) c.style.display = "";
        else c.style.display = "none";
    });
}

// ── EMERGENCY STOP ───────────────────────────────────────
async function emergencyStop() {
    if (!confirm("EMERGENCY STOP: This will immediately halt ALL trading. Are you sure?")) return;
    if (!confirm("CONFIRM: This action cannot be undone without restarting the bot.")) return;
    try {
        const r = await fetch("/api/emergency-stop", {method:"POST", credentials:"include"});
        const d = await r.json();
        if (d.status === "emergency_stop_activated") {
            document.getElementById("emergency-btn").style.background = "var(--red)";
            document.getElementById("emergency-btn").style.color = "#fff";
            document.getElementById("emergency-btn").textContent = "STOPPED";
            alert("EMERGENCY STOP ACTIVATED. All trading halted. Restart bot to resume.");
        } else {
            alert("Failed: " + JSON.stringify(d));
        }
    } catch(e) { alert("Emergency stop failed: " + e.message); }
}

// ── CONFIG TAB ───────────────────────────────────────────
function populateConfigTab() {
    // Trade Type Config table
    const cfg = {
        "SL ATR Mult":       ["0.9", "1.15", "1.5"],
        "TP1 R:R":           ["0.8", "1.2", "1.5"],
        "TP2 R:R":           ["1.2", "2.0", "3.0"],
        "TP3 R:R":           ["none", "3.0", "5.0"],
        "Time Stop (bars)":  ["3 (hard)", "8 (soft)", "none"],
        "Early Kill":        ["120s / 0.10R", "300s / 0.15R", "disabled"],
        "Trail ATR Mult":    ["0.6", "1.0", "1.5"],
        "Max Age":           ["15 min", "60 min", "8 hours"],
    };
    let html = "";
    for (const [param, vals] of Object.entries(cfg)) {
        html += "<tr><td style='color:var(--text-secondary)'>" + param + "</td>";
        html += "<td style='text-align:center;color:var(--cyan);font-weight:600'>" + vals[0] + "</td>";
        html += "<td style='text-align:center;color:var(--yellow);font-weight:600'>" + vals[1] + "</td>";
        html += "<td style='text-align:center;color:var(--purple);font-weight:600'>" + vals[2] + "</td></tr>";
    }
    document.getElementById("config-table").innerHTML = html;

    // Scanner tiers
    const tiers = {
        "structure_bounce": {mult: "1.0", status: "ACTIVE"},
        "bos_choch": {mult: "0.8", status: "ACTIVE"},
        "liquidity_sweep": {mult: "0.8", status: "ACTIVE"},
        "order_block_entry": {mult: "0.8", status: "ACTIVE"},
        "ema_momentum": {mult: "0.0", status: "ML ONLY"},
        "trend_continuation": {mult: "0.0", status: "ML ONLY"},
        "vwap_mean_revert": {mult: "0.0", status: "ML ONLY"},
        "rsi_divergence": {mult: "0.0", status: "ML ONLY"},
    };
    let thtml = "";
    for (const [sc, info] of Object.entries(tiers)) {
        const statusCls = info.status === "ACTIVE" ? "color:var(--green)" : "color:var(--text-muted)";
        thtml += "<tr><td>" + sc + "</td><td style='text-align:center;font-weight:600'>" + info.mult + "</td>";
        thtml += "<td style='text-align:center;" + statusCls + ";font-weight:600'>" + info.status + "</td></tr>";
    }
    document.getElementById("scanner-tiers-table").innerHTML = thtml;

    // Risk params
    const rp = [
        ["Account Size", "$1,000 (paper)"],
        ["Risk Per Trade", "0.75% ($7.50)"],
        ["Max Margin", "$100/trade"],
        ["Leverage Cap (paper)", "75x (conf 90+)"],
        ["Leverage Cap (real)", "10x"],
        ["Min Setup Strength", "65/100"],
        ["Hard Loss Cap", "-1.2R"],
        ["Circuit Breaker", "$25/day or 5 consec losses"],
        ["Symbol Cooling", "30 min after 3 consec losses"],
        ["VWAP Noise Zone", "< 0.12 ATR from VWAP"],
    ];
    let rphtml = "";
    for (const [k, v] of rp) {
        rphtml += "<div style='display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--border)'>";
        rphtml += "<span style='color:var(--text-secondary)'>" + k + "</span>";
        rphtml += "<span style='font-weight:600;font-family:var(--font-mono)'>" + v + "</span></div>";
    }
    document.getElementById("risk-params").innerHTML = rphtml;

    // Veto gates
    const vetos = [
        {name: "1. Regime Router", desc: "Only trades trending markets. Quiet = zero trades", color: "var(--red)"},
        {name: "2. HTF Alignment", desc: "15min trend must agree with 1min signal direction", color: "var(--red)"},
        {name: "3. VWAP Filter", desc: "Blocks trades < 0.12 ATR from VWAP (noise zone)", color: "var(--red)"},
        {name: "4. ATR Prefilter", desc: "Blocks extreme volatility (ATR ratio > 3.0)", color: "var(--yellow)"},
        {name: "5. Setup Strength", desc: "Score must exceed 65/100 (removes weak entries)", color: "var(--yellow)"},
        {name: "6. Counter-Trend Block", desc: "Cannot short in trending_up, long in trending_down", color: "var(--red)"},
        {name: "7. Symbol Cooling", desc: "30 min pause after 3 consecutive losses on same pair", color: "var(--cyan)"},
        {name: "8. Circuit Breaker", desc: "$25 daily loss limit or 5 consecutive losses", color: "var(--red)"},
        {name: "9. Hard Loss Cap", desc: "Force close at -1.2R (prevents catastrophic losses)", color: "var(--red)"},
    ];
    let vhtml = "";
    for (const v of vetos) {
        vhtml += "<div style='display:flex;gap:12px;padding:6px 0;border-bottom:1px solid var(--border);align-items:center'>";
        vhtml += "<span style='color:" + v.color + ";font-weight:700;min-width:180px'>" + v.name + "</span>";
        vhtml += "<span style='color:var(--text-secondary);font-size:.72rem'>" + v.desc + "</span></div>";
    }
    document.getElementById("veto-config").innerHTML = vhtml;
}

// Populate trade type performance from feedback data
async function loadTradeTypePerf() {
    try {
        const data = await api("/api/tracker/closed");
        if (!data || !data.length) return;
        const types = {};
        for (const t of data) {
            const tt = t.trade_type || t.metadata?.trade_type || "SCALP";
            if (!types[tt]) types[tt] = {w:0, l:0, pnl:0, r:[], dur:[], trail:0, timeout:0, sl:0};
            const d = types[tt];
            if ((t.pnl_usd||0) > 0) d.w++; else d.l++;
            d.pnl += (t.pnl_usd||0);
            d.r.push(t.exit_r||0);
            d.dur.push(t.trade_duration_sec||t.duration_sec||0);
            const ex = t.exit_reason||"";
            if (ex.includes("trail") || ex === "smart_extend_exit") d.trail++;
            if (ex === "max_age" || ex === "expired" || ex === "hard_cap" || ex === "dead_market" || ex === "no_momentum" || ex === "early_kill") d.timeout++;
            if (ex === "stop_loss") d.sl++;
        }
        let html = "";
        for (const [tt, d] of Object.entries(types).sort((a,b) => b[1].w+b[1].l - a[1].w-a[1].l)) {
            const total = d.w + d.l;
            const wr = total > 0 ? (d.w/total*100) : 0;
            const avgR = d.r.length > 0 ? d.r.reduce((a,b)=>a+b,0)/d.r.length : 0;
            const avgDur = d.dur.length > 0 ? d.dur.reduce((a,b)=>a+b,0)/d.dur.length/60 : 0;
            const trailPct = total > 0 ? (d.trail/total*100) : 0;
            const toPct = total > 0 ? (d.timeout/total*100) : 0;
            const slPct = total > 0 ? (d.sl/total*100) : 0;
            const color = tt === "SCALP" ? "var(--cyan)" : tt === "INTRADAY" ? "var(--yellow)" : "var(--purple)";
            html += "<tr>";
            html += "<td style='color:" + color + ";font-weight:700'>" + tt + "</td>";
            html += "<td style='text-align:right'>" + total + "</td>";
            html += "<td style='text-align:right;color:" + (wr>=80?"var(--green)":wr>=70?"var(--yellow)":"var(--red)") + "'>" + wr.toFixed(1) + "%</td>";
            html += "<td style='text-align:right;color:" + (d.pnl>=0?"var(--green)":"var(--red)") + "'>$" + d.pnl.toFixed(2) + "</td>";
            html += "<td style='text-align:right;color:" + (avgR>=0?"var(--green)":"var(--red)") + "'>" + avgR.toFixed(3) + "</td>";
            html += "<td style='text-align:right'>" + avgDur.toFixed(1) + "m</td>";
            html += "<td style='text-align:right;color:var(--green)'>" + trailPct.toFixed(0) + "%</td>";
            html += "<td style='text-align:right;color:var(--yellow)'>" + toPct.toFixed(0) + "%</td>";
            html += "<td style='text-align:right;color:var(--red)'>" + slPct.toFixed(0) + "%</td>";
            html += "</tr>";
        }
        document.getElementById("tradetype-perf-table").innerHTML = html || "<tr><td colspan=9 style='text-align:center;color:var(--text-muted)'>No data yet</td></tr>";
    } catch(e) { console.warn("Config perf load failed:", e); }
}

// ── #10 PAPER/REAL ACTIVE TRADES TABS ────────────────────
let _activeTradeTab = "paper";
function switchActiveTrades(tab) {
    _activeTradeTab = tab;
    document.getElementById("active-trades-paper").style.display = tab === "paper" ? "block" : "none";
    document.getElementById("active-trades-real").style.display = tab === "real" ? "block" : "none";
    document.getElementById("atab-paper").className = "trade-tab-btn" + (tab === "paper" ? " active" : "");
    document.getElementById("atab-real").className = "trade-tab-btn" + (tab === "real" ? " active" : "");
}

// ── #10 Update Real Active Trades cards ──────────────────
function updateRealActiveTrades(realStatus) {
    const wrap = document.getElementById("active-trades-real-cards");
    if (!wrap) return;
    const positions = (realStatus && realStatus.open_positions) || [];
    if (positions.length === 0) {
        wrap.innerHTML = '<div class="empty">No real positions</div>';
        return;
    }
    // Track A.3: Force Flat button (top-level, closes ALL real positions)
    const header = '<div style="display:flex;justify-content:flex-end;margin-bottom:6px">' +
        '<button onclick="forceFlatReal()" style="padding:4px 10px;font-size:.65rem;font-weight:700;background:rgba(255,59,92,.12);color:var(--red);border:1px solid rgba(255,59,92,.35);border-radius:4px;cursor:pointer" title="Close ALL open real positions immediately">🚨 FORCE FLAT (' + positions.length + ')</button>' +
        '</div>';

    wrap.innerHTML = header + positions.map(p => {
        const sideColor = p.side === "long" ? "var(--green)" : "var(--red)";
        const upnl = p.upnl_usd || 0;
        const upnlColor = upnl >= 0 ? "var(--green)" : "var(--red)";
        const dur = (p.duration_min || 0) >= 60 ? (p.duration_min/60).toFixed(1)+"h" : (p.duration_min||0).toFixed(0)+"m";
        const dec = (p.symbol||"").includes("BTC") ? 2 : 4;
        // Track A.2: LOCK 75% is only useful when position is in profit
        const lockBtnDisabled = upnl <= 0;
        const tidSafe = esc(p.trade_id || p.paper_trade_id || "");
        return '<div style="padding:10px;background:rgba(255,59,92,.04);border:1px solid rgba(255,59,92,.15);border-left:3px solid ' + sideColor + ';border-radius:8px;margin-bottom:6px">' +
            '<div class="flex justify-between items-center">' +
            '<div class="flex items-center gap-2">' +
            '<span style="font-weight:800">' + (p.symbol||"?") + '</span>' +
            '<span style="font-size:.55rem;padding:1px 5px;border-radius:3px;background:rgba(255,59,92,.12);color:var(--red);font-weight:700;border:1px solid rgba(255,59,92,.25)">REAL</span>' +
            '<span style="color:' + sideColor + ';font-weight:700;text-transform:uppercase">' + (p.side||"?") + '</span>' +
            '<span class="text-warning font-bold">' + (p.leverage||0) + 'x</span>' +
            '<span style="color:var(--cyan);font-size:.72rem">' + (p.scanner||"--") + '</span>' +
            '</div>' +
            '<div style="display:flex;align-items:center;gap:12px">' +
            '<span style="font-size:1.1rem;font-weight:800;color:' + upnlColor + '">' + (upnl>=0?"+":"") + '$' + upnl.toFixed(2) + '</span>' +
            '<span style="color:var(--text-muted);font-size:.72rem">' + dur + '</span>' +
            '</div></div>' +
            '<div style="display:flex;gap:16px;margin-top:6px;font-size:.72rem;font-family:var(--font-mono)">' +
            '<span class="text-muted">Entry: <span style="color:var(--text)">' + (p.entry_price||0).toFixed(dec) + '</span></span>' +
            '<span class="text-muted">Current: <span style="color:var(--text)">' + (p.current_price||0).toFixed(dec) + '</span></span>' +
            '<span class="text-muted">SL: <span class="text-danger">' + (p.stop_loss||0).toFixed(dec) + '</span></span>' +
            '<span class="text-muted">TP1: <span class="text-success">' + (p.tp1 ? p.tp1.toFixed(dec) : "--") + '</span></span>' +
            '<span class="text-muted">Margin: $' + (p.margin||0).toFixed(2) + '</span>' +
            '</div>' +
            // Track A.2: per-position LOCK 75% button
            '<div style="display:flex;justify-content:flex-end;gap:6px;margin-top:6px">' +
            '<button onclick="lockReal75(\'' + tidSafe + '\')" ' +
            (lockBtnDisabled ? 'disabled title="LOCK 75% requires position in profit"' : 'title="Close 75% of this position to lock profit — keep 25% runner"') +
            ' style="padding:3px 9px;font-size:.58rem;font-weight:700;background:' +
            (lockBtnDisabled ? 'rgba(80,80,80,.1);color:var(--text-muted);cursor:not-allowed' : 'rgba(6,182,212,.12);color:var(--cyan);cursor:pointer') +
            ';border:1px solid ' + (lockBtnDisabled ? 'var(--border)' : 'rgba(6,182,212,.35)') +
            ';border-radius:3px">🔒 LOCK 75%</button>' +
            '</div></div>';
    }).join("");
}

// Track A.2: LOCK 75% action
async function lockReal75(tradeId) {
    if (!tradeId) return;
    if (!confirm("Close 75% of this real position? Keeps 25% runner at current SL/TP.")) return;
    try {
        const r = await fetch("/api/real/lock_75", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({trade_id: tradeId}),
        });
        const d = await r.json();
        if (d.ok) {
            alert("✅ LOCK 75% sent — " + (d.paper_trade_id || tradeId).substring(0,12));
        } else {
            alert("❌ LOCK 75% failed: " + (d.error || "unknown"));
        }
    } catch (e) {
        alert("❌ LOCK 75% network error: " + e.message);
    }
}

// Track A.3: Force Flat action
async function forceFlatReal() {
    if (!confirm("🚨 FORCE FLAT: close ALL open real positions immediately?\n\nThis cannot be undone.")) return;
    try {
        const r = await fetch("/api/real/force_flat", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: "{}",
        });
        const d = await r.json();
        if (d.ok) {
            alert("✅ Force Flat: closed " + (d.closed || 0) + "/" + (d.total || 0) + " positions");
        } else {
            alert("❌ Force Flat failed: " + (d.error || "unknown"));
        }
    } catch (e) {
        alert("❌ Force Flat network error: " + e.message);
    }
}

// ── #2 + #9 MTF CHAIN UPDATE ─────────────────────────────
function updateMTFChain(decision) {
    if (!decision) return;
    const ind = decision.indicators || {};
    // #2 Macro bias badge
    const macroBias = ind.macro_bias_str || ind.macro_bias || "neutral";
    const macroEl = document.getElementById("cmd-macro-bias");
    if (macroEl) {
        const bias = macroBias.toLowerCase();
        macroEl.textContent = macroBias.toUpperCase();
        macroEl.className = "regime-badge " + (bias.includes("bull") ? "trending_up" : bias.includes("bear") ? "trending_down" : "neutral");
    }
    // #9 MTF chain visual
    const htf = ind.htf_bias || ind.htf_alignment || "neutral";
    const mtf15 = ind.mtf_15m_bias || ind.bias_15m || "neutral";
    const mtf5 = ind.mtf_5m_bias || ind.bias_5m || "neutral";
    const signalSide = decision.action === "BUY" ? "bullish" : decision.action === "SELL" ? "bearish" : "neutral";
    function setMTFNode(id, label, bias) {
        const el = document.getElementById(id);
        if (!el) return;
        const b = String(bias).toLowerCase();
        const cls = b.includes("bull") || b.includes("up") || b.includes("long") ? "bullish" : b.includes("bear") || b.includes("down") || b.includes("short") ? "bearish" : "neutral";
        el.className = "mtf-node " + cls;
        el.textContent = label + " " + (cls === "bullish" ? "\u25B2" : cls === "bearish" ? "\u25BC" : "\u25CF");
    }
    setMTFNode("mtf-1h", "1H", macroBias);
    setMTFNode("mtf-15m", "15M", mtf15);
    setMTFNode("mtf-5m", "5M", mtf5);
    setMTFNode("mtf-1m", "1M", signalSide);
}

// ── #3 SCANNER DIVERSITY ─────────────────────────────────
const SCANNER_COLORS = {
    "structure_bounce":"#22c55e","bos_choch":"#6366f1","liquidity_sweep":"#f59e0b",
    "order_block_entry":"#ec4899","ema_momentum":"#06b6d4","trend_continuation":"#8b5cf6",
    "vwap_mean_revert":"#f97316","rsi_divergence":"#ef4444","mean_reversion":"#14b8a6"
};
function updateScannerDiversity(closed) {
    const bar = document.getElementById("scanner-diversity-bar");
    const legend = document.getElementById("scanner-diversity-legend");
    if (!bar || !legend || !closed || !Array.isArray(closed) || closed.length === 0) return;
    const counts = {};
    let total = 0;
    closed.forEach(t => {
        const sc = t.setup_type || t.scanner || "unknown";
        counts[sc] = (counts[sc] || 0) + 1;
        total++;
    });
    const sorted = Object.entries(counts).sort((a,b) => b[1] - a[1]);
    bar.innerHTML = sorted.map(([sc, cnt]) => {
        const pct = (cnt / total * 100);
        const color = SCANNER_COLORS[sc] || "#6b7280";
        return '<div class="scanner-diversity-seg" style="width:' + pct + '%;background:' + color + '" title="' + sc + ': ' + cnt + ' (' + pct.toFixed(1) + '%)">' + (pct > 8 ? sc.split("_")[0] : "") + '</div>';
    }).join("");
    legend.innerHTML = sorted.map(([sc, cnt]) => {
        const color = SCANNER_COLORS[sc] || "#6b7280";
        const pct = (cnt / total * 100).toFixed(1);
        return '<span style="display:flex;align-items:center;gap:3px"><span style="width:8px;height:8px;border-radius:2px;background:' + color + '"></span>' + sc + ' (' + pct + '%)</span>';
    }).join("");
}

// ── #1 PER-SCANNER PERFORMANCE (B.4 + B.5: W/L pie + EV leaderboard) ──
// Stores paper & real datasets separately and renders the selected view.
let _scannerDataPaper = [];
let _scannerDataReal = [];
let _scannerView = "paper";  // "paper" | "real"
let _spPieCharts = [null, null, null];

function setScannerView(view) {
    _scannerView = view;
    document.querySelectorAll("#scanner-perf-toggle .sp-btn").forEach(b => {
        const active = b.dataset.view === view;
        b.className = "sp-btn" + (active ? " sp-active" : "");
        if (active) {
            const color = view === "paper" ? "rgba(6,182,212" : "rgba(255,59,92";
            const text = view === "paper" ? "var(--cyan)" : "var(--red)";
            b.style.cssText = "padding:2px 10px;font-size:.6rem;border:1px solid " + color + ",.3);background:" + color + ",.08);color:" + text + ";border-radius:3px;cursor:pointer;font-weight:700";
        } else {
            b.style.cssText = "padding:2px 10px;font-size:.6rem;border:1px solid var(--border);background:transparent;color:var(--text-muted);border-radius:3px;cursor:pointer;font-weight:600";
        }
    });
    _renderScannerPerfView();
}

function _aggScanner(trades) {
    const scanners = {};
    (trades || []).forEach(t => {
        const sc = t.setup_type || t.scanner || "unknown";
        if (!scanners[sc]) scanners[sc] = {trades:0, wins:0, pnl:0, rs:[]};
        const d = scanners[sc];
        d.trades++;
        if ((t.pnl_usd || 0) > 0) d.wins++;
        d.pnl += (t.pnl_usd || 0);
        d.rs.push(t.exit_r || t.r_multiple || (t.peak_mfe_r || 0));
    });
    return scanners;
}

function _renderScannerPerfView() {
    const body = document.getElementById("per-scanner-perf-body");
    if (!body) return;
    const src = (_scannerView === "real") ? _scannerDataReal : _scannerDataPaper;
    const scanners = _aggScanner(src);
    const total = Object.values(scanners).reduce((a,d)=>a+d.trades,0);
    if (total === 0) {
        body.innerHTML = '<tr><td colspan="7" class="empty">No ' + _scannerView + ' trades yet</td></tr>';
        _renderScannerPies([]);
        return;
    }
    // B.5: sort by EV (pnl per trade) descending
    const rows = Object.entries(scanners).map(([sc, d]) => {
        const wr = d.trades > 0 ? (d.wins / d.trades * 100) : 0;
        const ev = d.trades > 0 ? (d.pnl / d.trades) : 0;
        const avgR = d.rs.length ? d.rs.reduce((a,b)=>a+b,0) / d.rs.length : 0;
        const pct = total ? (d.trades / total * 100) : 0;
        return {sc, d, wr, ev, avgR, pct};
    }).sort((a,b) => b.ev - a.ev);

    body.innerHTML = rows.map((r, idx) => {
        const rankMark = idx === 0 ? '🥇 ' : idx === 1 ? '🥈 ' : idx === 2 ? '🥉 ' : '';
        return '<tr><td style="font-weight:600;color:' + (SCANNER_COLORS[r.sc] || 'var(--text)') + '">' +
            rankMark + esc(r.sc) + '</td>' +
            '<td>' + r.d.trades + '</td>' +
            '<td class="' + (r.wr >= 50 ? 'green' : 'red') + '">' + r.wr.toFixed(0) + '%</td>' +
            '<td class="' + (r.d.pnl >= 0 ? 'pnl-pos' : 'pnl-neg') + '">$' + r.d.pnl.toFixed(2) + '</td>' +
            '<td class="' + (r.ev >= 0 ? 'pnl-pos' : 'pnl-neg') + '" class="font-bold">$' + r.ev.toFixed(3) + '</td>' +
            '<td class="' + (r.avgR >= 0 ? 'green' : 'red') + '">' + r.avgR.toFixed(2) + 'R</td>' +
            '<td>' + r.pct.toFixed(0) + '%</td></tr>';
    }).join("");

    // B.4: render 3 W/L pies for top 3 EV scanners
    _renderScannerPies(rows.slice(0, 3));
}

function _renderScannerPies(topRows) {
    for (let i = 0; i < 3; i++) {
        const canvas = document.getElementById("sp-pie-" + i);
        const lbl = document.getElementById("sp-lbl-" + i);
        if (!canvas) continue;
        if (i >= topRows.length) {
            if (_spPieCharts[i]) { _spPieCharts[i].destroy(); _spPieCharts[i] = null; }
            if (lbl) lbl.textContent = "—";
            continue;
        }
        const r = topRows[i];
        const wins = r.d.wins;
        const losses = Math.max(0, r.d.trades - wins);
        const col = _scannerView === "real" ? ["#ff3b5c", "#4a1220"] : ["#06b6d4", "#164e5c"];
        try {
            if (_spPieCharts[i]) _spPieCharts[i].destroy();
            _spPieCharts[i] = new Chart(canvas.getContext("2d"), {
                type: "doughnut",
                data: {
                    labels: ["Wins", "Losses"],
                    datasets: [{
                        data: [wins, losses],
                        backgroundColor: [col[0], col[1]],
                        borderWidth: 0,
                    }],
                },
                options: {
                    responsive: false, maintainAspectRatio: false,
                    cutout: "65%",
                    plugins: { legend: { display: false }, tooltip: { enabled: true } },
                },
            });
            if (lbl) {
                const wr = r.d.trades > 0 ? (wins / r.d.trades * 100).toFixed(0) : "0";
                lbl.innerHTML = '<span style="color:' + (SCANNER_COLORS[r.sc] || 'var(--text)') + '">' +
                    esc(r.sc.substring(0, 10)) + '</span><br><span style="color:' + col[0] + '">' +
                    wr + '%</span> · $' + r.ev.toFixed(2) + '/tr';
            }
        } catch (e) { /* chart destroy race */ }
    }
}

function updatePerScannerPerf(closed) {
    _scannerDataPaper = Array.isArray(closed) ? closed : [];
    _renderScannerPerfView();
}

// Real-trade scanner stats pulled from /api/real/status recent_trades
function updateRealScannerPerf(recentReal) {
    _scannerDataReal = Array.isArray(recentReal) ? recentReal : [];
    if (_scannerView === "real") _renderScannerPerfView();
}

// ══════════════════════════════════════════════════════════
// SECTION 6.5 RENDERERS (B.2 + B.6 + B.11 + C.1 + C.6/C.7 + A.1)
// ══════════════════════════════════════════════════════════

// ── B.2: DRAWDOWN GAUGE ──
function updateDrawdownGauge(realStatus) {
    const wrap = document.getElementById("dd-gauge");
    if (!wrap) return;
    const dd = (realStatus && realStatus.rolling_drawdown) || {};
    if (!dd || Object.keys(dd).length === 0) {
        wrap.innerHTML = '<div class="empty">No drawdown data</div>';
        return;
    }
    const periods = [
        { key: "1h", label: "1 Hour", limit: 15 },
        { key: "24h", label: "24 Hour", limit: 25 },
        { key: "7d", label: "7 Day", limit: 50 },
    ];
    wrap.innerHTML = periods.map(p => {
        const d = dd[p.key] || {};
        const pnl = d.pnl || 0;
        const limit = d.limit || p.limit;
        const pct = limit > 0 ? Math.min(100, Math.abs(pnl) / limit * 100) : 0;
        const col = pct > 80 ? "var(--red)" : pct > 50 ? "var(--yellow)" : "var(--green)";
        return '<div class="flex items-center gap-2">' +
            '<span style="font-size:.65rem;color:var(--text-muted);min-width:50px">' + p.label + '</span>' +
            '<div style="flex:1;height:14px;background:rgba(255,255,255,.03);border-radius:3px;overflow:hidden;position:relative">' +
            '<div style="height:100%;width:' + pct.toFixed(1) + '%;background:' + col + ';border-radius:3px;transition:width .3s"></div>' +
            '</div>' +
            '<span style="font-size:.65rem;font-family:var(--font-mono);min-width:70px;text-align:right;color:' + col + '">' +
            '$' + pnl.toFixed(2) + '/' + limit.toFixed(0) + '</span>' +
            '</div>';
    }).join("");
}

// ── B.6: FEE SAVINGS TRACKER ──
function updateFeeSavings(realStatus) {
    const fs = (realStatus && realStatus.fix_stats) || {};
    const fills = fs.fix1_ioc_fill || 0;
    const skips = fs.fix1_ioc_skip || 0;
    const total = fills + skips;
    const fillRate = total > 0 ? (fills / total * 100) : 0;
    const el = id => document.getElementById(id);
    if (el("fs-ioc-fills")) el("fs-ioc-fills").textContent = String(fills);
    if (el("fs-ioc-skips")) el("fs-ioc-skips").textContent = String(skips);
    // Estimate savings: each skipped trade avoided ~30bp * $20 margin * 20x leverage
    const estSave = skips * 0.003 * 20 * 20;
    if (el("fs-est-savings")) el("fs-est-savings").textContent = "$" + estSave.toFixed(2);
    if (el("fs-fill-rate")) {
        const rEl = el("fs-fill-rate");
        rEl.textContent = total > 0 ? fillRate.toFixed(0) + "%" : "--";
        rEl.style.color = fillRate >= 70 ? "var(--green)" : fillRate >= 50 ? "var(--yellow)" : "var(--red)";
    }
    // Avg slippage from closed real trades
    const rt = (realStatus && realStatus.recent_trades) || [];
    if (rt.length > 0 && el("fs-avg-slip")) {
        const slips = rt.filter(t => (t.slippage_bps || 0) > 0).map(t => t.slippage_bps);
        const avg = slips.length > 0 ? slips.reduce((a, b) => a + b, 0) / slips.length : 0;
        el("fs-avg-slip").textContent = avg.toFixed(1) + " bp";
    }
}

// ── B.11: SCANNER WEIGHTS TABLE ──
async function loadScannerWeights() {
    try {
        const d = await fetch("/api/ml/live-calibration").then(r => r.json()).catch(() => null);
        const body = document.getElementById("sw-body");
        if (!body) return;
        if (!d || !d.buckets) {
            body.innerHTML = '<tr><td colspan="3" class="empty">No calibration data</td></tr>';
            return;
        }
        // Aggregate by scanner from buckets
        const scanners = {};
        (d.buckets || []).forEach(b => {
            const sc = b.scanner || "unknown";
            if (!scanners[sc]) scanners[sc] = { total: 0, ok: 0, drift: 0 };
            scanners[sc].total++;
            if (b.status === "OK") scanners[sc].ok++;
            if (b.status === "DRIFTING") scanners[sc].drift++;
        });
        const rows = Object.entries(scanners).sort((a, b) => b[1].ok - a[1].ok);
        body.innerHTML = rows.map(([sc, v]) => {
            const health = v.total > 0 ? (v.ok / v.total * 100) : 0;
            const trendIcon = v.drift > 0 ? '<span class="text-danger">&#x25BC;</span>' :
                              health >= 80 ? '<span class="text-success">&#x25B2;</span>' :
                              '<span class="text-warning">&#x25CF;</span>';
            return '<tr><td style="font-weight:600;color:' + (SCANNER_COLORS[sc] || 'var(--text)') + '">' + esc(sc) + '</td>' +
                '<td class="font-mono">' + health.toFixed(0) + '%</td>' +
                '<td>' + trendIcon + (v.drift > 0 ? ' <span style="font-size:.6rem;color:var(--red)">' + v.drift + ' drift</span>' : '') + '</td></tr>';
        }).join("");
    } catch (e) {}
}

// ── C.1: EDGE VERDICT TREND SPARKLINES ──
async function loadVerdictSparklines() {
    try {
        const d = await fetch("/api/ml/edge-verdict-trend").then(r => r.json()).catch(() => null);
        const wrap = document.getElementById("verdict-sparklines");
        if (!wrap) return;
        if (!d || (!d.trend && !d.data)) {
            wrap.innerHTML = '<div class="empty">No verdict trend data</div>';
            return;
        }
        const trend = d.trend || d.data || d;
        const cats = ["HOLDS", "WEAK", "UNCLEAR", "NO_EDGE"];
        const colors = { "HOLDS": "var(--green)", "WEAK": "var(--yellow)", "UNCLEAR": "var(--text-muted)", "NO_EDGE": "var(--red)" };
        let html = "";
        if (Array.isArray(trend)) {
            // Array of {ts, verdict} — aggregate counts
            const counts = {};
            cats.forEach(c => counts[c] = 0);
            trend.forEach(t => {
                const v = (t.verdict || t.edge_verdict || "").toUpperCase();
                if (counts[v] !== undefined) counts[v]++;
            });
            const total = trend.length || 1;
            html = cats.map(c => {
                const n = counts[c] || 0;
                const pct = (n / total * 100);
                return '<div class="flex items-center gap-2">' +
                    '<span style="font-size:.6rem;min-width:60px;color:' + colors[c] + ';font-weight:600">' + c + '</span>' +
                    '<div style="flex:1;height:10px;background:rgba(255,255,255,.03);border-radius:2px;overflow:hidden">' +
                    '<div style="height:100%;width:' + pct.toFixed(1) + '%;background:' + colors[c] + ';border-radius:2px"></div></div>' +
                    '<span style="font-size:.6rem;font-family:var(--font-mono);min-width:40px;text-align:right">' + n + ' (' + pct.toFixed(0) + '%)</span></div>';
            }).join("");
        } else if (typeof trend === "object") {
            // Dict keyed by verdict
            const total = Object.values(trend).reduce((a, v) => a + (typeof v === "number" ? v : (v.count || 0)), 0) || 1;
            html = cats.map(c => {
                const val = trend[c] || trend[c.toLowerCase()] || 0;
                const n = typeof val === "number" ? val : (val.count || 0);
                const pct = (n / total * 100);
                return '<div class="flex items-center gap-2">' +
                    '<span style="font-size:.6rem;min-width:60px;color:' + colors[c] + ';font-weight:600">' + c + '</span>' +
                    '<div style="flex:1;height:10px;background:rgba(255,255,255,.03);border-radius:2px;overflow:hidden">' +
                    '<div style="height:100%;width:' + pct.toFixed(1) + '%;background:' + colors[c] + ';border-radius:2px"></div></div>' +
                    '<span style="font-size:.6rem;font-family:var(--font-mono);min-width:40px;text-align:right">' + n + ' (' + pct.toFixed(0) + '%)</span></div>';
            }).join("");
        }
        wrap.innerHTML = html || '<div class="empty">Unexpected data format</div>';
    } catch (e) {
        var w = document.getElementById("verdict-sparklines");
        if (w) w.innerHTML = '<div class="empty">Fetch failed</div>';
    }
}

// ── C.6 + C.7: FEATURE IMPORTANCE + BTC CROSS-ASSET ──
async function loadFeatureImportance() {
    try {
        const d = await fetch("/api/ml/health").then(r => r.json()).catch(() => null);
        const fiWrap = document.getElementById("feat-importance");
        const btcWrap = document.getElementById("btc-importance");
        if (!fiWrap && !btcWrap) return;

        // Try live_model_results for top features
        let topFeats = [];
        let btcFeats = [];
        try {
            const lm = await fetch("/api/ml/live-calibration").then(r => r.json()).catch(() => null);
            if (lm && lm.feature_importances) {
                topFeats = Object.entries(lm.feature_importances).sort((a, b) => b[1] - a[1]).slice(0, 12);
            }
        } catch (e) {}

        // Fallback 1: read from scanner models via /api/ml/health
        if (topFeats.length === 0 && d && d.scanners) {
            const firstScanner = Object.values(d.scanners)[0];
            if (firstScanner && firstScanner.top_features) {
                topFeats = firstScanner.top_features.map(f => [f.feature, f.importance]);
            }
        }

        // Fallback 2: read from live_model_results via tracker stats
        if (topFeats.length === 0) {
            try {
                const trkStats = await fetch("/api/tracker/stats").then(r => r.json()).catch(() => null);
                if (trkStats && trkStats.by_setup) {
                    // Build pseudo-importance from scanner WR
                    const setups = Object.entries(trkStats.by_setup || {});
                    topFeats = setups.map(([name, data]) => [
                        "scanner_" + name,
                        parseFloat(data.win_rate || data.wr || 0) / 100
                    ]).sort((a, b) => b[1] - a[1]).slice(0, 10);
                }
            } catch(e) {}
        }

        // Separate BTC features from general features
        const allFeats = topFeats.length > 0 ? topFeats : [];
        btcFeats = allFeats.filter(([name]) => name.startsWith("btc_") || name.includes("_btc_"));
        const nonBtcFeats = allFeats.filter(([name]) => !name.startsWith("btc_") && !name.includes("_btc_")).slice(0, 10);

        // Render general feature importance (C.6)
        if (fiWrap) {
            if (nonBtcFeats.length === 0) {
                fiWrap.innerHTML = '<div class="empty">No feature data yet</div>';
            } else {
                const maxImp = Math.max(...nonBtcFeats.map(f => f[1])) || 1;
                fiWrap.innerHTML = nonBtcFeats.map(([name, imp]) => {
                    const pct = (imp / maxImp * 100);
                    const shortName = name.replace(/_/g, " ").replace(/^(scanner |regime )/, "");
                    return '<div class="flex items-center gap-2">' +
                        '<span style="font-size:.58rem;min-width:85px;color:var(--text-muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + esc(name) + '">' + esc(shortName) + '</span>' +
                        '<div style="flex:1;height:8px;background:rgba(255,255,255,.03);border-radius:2px;overflow:hidden">' +
                        '<div style="height:100%;width:' + pct.toFixed(1) + '%;background:var(--cyan);border-radius:2px"></div></div>' +
                        '<span style="font-size:.55rem;font-family:var(--font-mono);min-width:35px;text-align:right">' + (imp * 100).toFixed(1) + '%</span></div>';
                }).join("");
            }
        }

        // Render BTC cross-asset features (C.7)
        if (btcWrap) {
            if (btcFeats.length === 0) {
                // Show known BTC block features from schema even if no importance yet
                const knownBtc = ["btc_return_1", "btc_return_5", "btc_atr_ratio", "btc_dist_from_vwap",
                                  "btc_ema8_slope", "btc_trend_bias", "btc_range_pos", "symbol_btc_corr_60", "symbol_btc_beta_60"];
                btcWrap.innerHTML = '<div style="font-size:.6rem;color:var(--text-muted);margin-bottom:6px">BTC context features (14) built by _build_btc_block():</div>' +
                    knownBtc.map(f => '<span style="display:inline-block;font-size:.55rem;padding:2px 6px;margin:1px;border-radius:3px;background:rgba(249,115,22,.08);color:#f97316;border:1px solid rgba(249,115,22,.2)">' + f + '</span>').join("");
            } else {
                const maxBtc = Math.max(...btcFeats.map(f => f[1])) || 1;
                btcWrap.innerHTML = btcFeats.map(([name, imp]) => {
                    const pct = (imp / maxBtc * 100);
                    return '<div class="flex items-center gap-2">' +
                        '<span style="font-size:.58rem;min-width:85px;color:#f97316;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(name) + '</span>' +
                        '<div style="flex:1;height:8px;background:rgba(255,255,255,.03);border-radius:2px;overflow:hidden">' +
                        '<div style="height:100%;width:' + pct.toFixed(1) + '%;background:#f97316;border-radius:2px"></div></div>' +
                        '<span style="font-size:.55rem;font-family:var(--font-mono);min-width:35px;text-align:right">' + (imp * 100).toFixed(1) + '%</span></div>';
                }).join("");
            }
        }
    } catch (e) {}
}

// ── A.1: OPPORTUNITY FUNNEL KANBAN ──
function updateKanbanFunnel(funnelData) {
    const wrap = document.getElementById("kanban-funnel");
    if (!wrap) return;
    const f = (funnelData && funnelData.funnel) || funnelData || {};
    if (!f || Object.keys(f).length === 0) {
        wrap.innerHTML = '<div class="empty" style="grid-column:1/-1">No funnel data</div>';
        return;
    }
    // Standard funnel stages
    const stages = [
        { key: "scanned", label: "Scanned", color: "var(--text-muted)", icon: "&#x1F50D;" },
        { key: "passed_filter", label: "Filtered", color: "var(--yellow)", icon: "&#x2705;" },
        { key: "ml_scored", label: "ML Scored", color: "var(--purple)", icon: "&#x1F916;" },
        { key: "tracked", label: "Tracked", color: "var(--cyan)", icon: "&#x1F4CA;" },
        { key: "executed", label: "Executed", color: "var(--green)", icon: "&#x2B50;" },
    ];
    wrap.innerHTML = stages.map(s => {
        const count = f[s.key] || f[s.key + "_count"] || f[s.key + "s"] || 0;
        const nStr = typeof count === "number" ? count : (count.total || count.count || 0);
        return '<div style="background:rgba(255,255,255,.02);border:1px solid var(--border);border-radius:6px;padding:10px 6px;text-align:center">' +
            '<div style="font-size:1.2rem">' + s.icon + '</div>' +
            '<div style="font-size:1.3rem;font-weight:800;font-family:var(--font-mono);color:' + s.color + ';margin:4px 0">' + nStr + '</div>' +
            '<div style="font-size:.55rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:.5px">' + s.label + '</div>' +
            '</div>';
    }).join("");
}

// ── WIRE SECTION 6.5 into existing data flows ──
// Hook into the main refresh cycle to populate these panels
(function wireSection65() {
    // Drawdown + Fee Savings from real status (already fetched every 5s by loadRealOps)
    const origUpdateRealOps = window.updateRealActiveTrades;
    if (origUpdateRealOps) {
        window.updateRealActiveTrades = function(realStatus) {
            origUpdateRealOps(realStatus);
            try { updateDrawdownGauge(realStatus); } catch (e) {}
            try { updateFeeSavings(realStatus); } catch (e) {}
        };
    }
    // Scanner weights + verdict sparklines + feature importance — load every 30s
    async function loadSection65Slow() {
        try { await loadScannerWeights(); } catch (e) {}
        try { await loadVerdictSparklines(); } catch (e) {}
        try { await loadFeatureImportance(); } catch (e) {}
    }
    loadSection65Slow();
    setInterval(loadSection65Slow, 30000);
})();

// ══════════════════════════════════════════════════════════
// ══════════════════════════════════════════════════════════
// VISION TIER 1: Attention Rail + KPIs + Signal Radar
// ══════════════════════════════════════════════════════════

// ── ATTENTION RAIL ──
// Prioritized list of what the bot is focusing on RIGHT NOW.
// Sources: active trades approaching SL, regime changes, scanner triggers,
// ML drift alerts, unusual volume, real trade events.
function updateAttentionRail(status, decision, active, realStatus, funnel) {
    var rail = document.getElementById("attention-rail");
    var countEl = document.getElementById("attn-count");
    if (!rail) return;

    var items = [];
    var now = Date.now() / 1000;

    // 1. Active trades approaching SL (highest priority)
    try {
        var tracker = (active && active.active) || active || [];
        if (Array.isArray(tracker)) {
            tracker.forEach(function(t) {
                var mfe = parseFloat(t.peak_mfe_r || t.mfe_r || 0);
                var currentR = parseFloat(t.current_r || 0);
                if (currentR < 0.05 && mfe > 0.3) {
                    items.push({pri: 1, color: "#ff3b5c", icon: "&#x26A0;",
                        text: (t.symbol || "?") + " retracing from " + mfe.toFixed(2) + "R peak — near SL"});
                }
            });
        }
    } catch(e){}

    // 2. Real trade events
    try {
        if (realStatus) {
            var openR = realStatus.open_count || 0;
            if (openR > 0) {
                items.push({pri: 2, color: "#ff3b5c", icon: "&#x1F534;",
                    text: openR + " real position(s) open — monitoring"});
            }
            var cb = realStatus.circuit_breaker || {};
            if (cb.is_tripped) {
                items.push({pri: 0, color: "#ff3b5c", icon: "&#x1F6A8;",
                    text: "CIRCUIT BREAKER TRIPPED: " + (cb.trip_reason || "unknown")});
            }
            if ((cb.consecutive_losses || 0) >= 2) {
                items.push({pri: 1, color: "#f59e0b", icon: "&#x26A0;",
                    text: cb.consecutive_losses + " consecutive real losses — approaching CB trip"});
            }
        }
    } catch(e){}

    // 3. Regime info from decision
    try {
        if (decision) {
            var regime = (decision.regime || decision.market_regime || "").toLowerCase();
            if (regime && regime !== "unknown") {
                var regimeColor = regime.includes("trend") ? "var(--green)" :
                                  regime.includes("break") ? "var(--cyan)" :
                                  regime.includes("sideways") || regime.includes("rang") ? "var(--yellow)" : "var(--text-muted)";
                items.push({pri: 3, color: regimeColor, icon: "&#x1F30A;",
                    text: "Regime: " + regime.toUpperCase() + " — " +
                          (regime.includes("trend") ? "scanners active, expecting signals" :
                           regime.includes("break") ? "breakout mode, watching for entries" :
                           "sideways/quiet, reduced signal generation")});
            }
        }
    } catch(e){}

    // 4. Scanner triggers from funnel
    try {
        var f = (funnel && funnel.funnel) || funnel || {};
        var nearMisses = funnel ? (funnel.near_misses || {}) : {};
        var nmCount = 0;
        for (var sym in nearMisses) {
            nmCount += (nearMisses[sym] || []).length;
        }
        if (nmCount > 0) {
            items.push({pri: 4, color: "var(--yellow)", icon: "&#x1F50D;",
                text: nmCount + " near-miss signal(s) — close to qualifying but didn't pass filters"});
        }
    } catch(e){}

    // 5. Bot status
    try {
        if (status) {
            var uptime = status.uptime || "--";
            items.push({pri: 5, color: "var(--text-muted)", icon: "&#x2705;",
                text: "Bot running: " + uptime + " | " + (status.symbols || []).length + " symbols scanning"});
        }
    } catch(e){}

    // Sort by priority (0=highest)
    items.sort(function(a, b) { return a.pri - b.pri; });

    if (items.length === 0) {
        rail.innerHTML = '<div class="empty">All quiet — no active signals or alerts</div>';
    } else {
        rail.innerHTML = items.map(function(item) {
            return '<div style="display:flex;align-items:flex-start;gap:6px;padding:4px 6px;border-radius:4px;background:rgba(255,255,255,.02);border-left:2px solid ' + item.color + '">' +
                '<span style="font-size:.7rem;flex-shrink:0">' + item.icon + '</span>' +
                '<span style="font-size:.62rem;color:var(--text);line-height:1.3">' + item.text + '</span></div>';
        }).join("");
    }
    if (countEl) countEl.textContent = items.length + " items";
}

// ── DEPLOYABLE CAPITAL KPI ──
var _deploySparkData = [];
var _deploySparkChart = null;

function updateDeployableCapital(realStatus) {
    if (!realStatus) return;
    var balance = realStatus.balance || 0;
    var openCount = realStatus.open_count || 0;
    var margin = 20; // per-trade margin
    var reserved = openCount * margin;
    var dd = realStatus.rolling_drawdown || {};
    var dd1h = (dd["1h"] || {}).pnl || 0;
    var ddBuffer = Math.max(0, Math.abs(dd1h) * 2); // 2x recent drawdown as buffer
    var deployable = Math.max(0, balance - reserved - ddBuffer);

    var el = function(id) { return document.getElementById(id); };
    if (el("kpi-deployable")) el("kpi-deployable").textContent = "$" + deployable.toFixed(0);
    if (el("kpi-deploy-pct")) el("kpi-deploy-pct").textContent = "of $" + balance.toFixed(0);
    if (el("kpi-reserved")) el("kpi-reserved").textContent = "$" + reserved.toFixed(0);
    if (el("kpi-dd-buffer")) el("kpi-dd-buffer").textContent = "$" + ddBuffer.toFixed(1);

    // Sparkline (rolling 30 data points)
    _deploySparkData.push(deployable);
    if (_deploySparkData.length > 30) _deploySparkData.shift();
    try {
        var canvas = document.getElementById("kpi-deploy-spark");
        if (canvas && _deploySparkData.length > 2) {
            if (_deploySparkChart) _deploySparkChart.destroy();
            _deploySparkChart = new Chart(canvas.getContext("2d"), {
                type: "line",
                data: {
                    labels: _deploySparkData.map(function(_, i) { return ""; }),
                    datasets: [{
                        data: _deploySparkData,
                        borderColor: "rgba(0,255,157,.6)",
                        backgroundColor: "rgba(0,255,157,.05)",
                        fill: true, borderWidth: 1.5, pointRadius: 0, tension: 0.3,
                    }],
                },
                options: {
                    responsive: true, maintainAspectRatio: false,
                    plugins: { legend: { display: false }, tooltip: { enabled: false } },
                    scales: { x: { display: false }, y: { display: false } },
                },
            });
        }
    } catch(e) {}
}

// ── LIVE EDGE ESTIMATE KPI ──
function updateLiveEdge(closed) {
    if (!closed || !Array.isArray(closed) || closed.length < 5) return;
    var recent = closed.slice(-20);
    var wins = 0, totalPnl = 0, winPnl = 0, lossPnl = 0, winCount = 0, lossCount = 0;
    recent.forEach(function(t) {
        var pnl = parseFloat(t.pnl_usd || 0);
        var r = parseFloat(t.exit_r || t.r_multiple || 0);
        totalPnl += pnl;
        if (pnl > 0) { wins++; winPnl += pnl; winCount++; }
        else { lossPnl += Math.abs(pnl); lossCount++; }
    });
    var wr = recent.length > 0 ? (wins / recent.length * 100) : 0;
    var avgR = recent.length > 0 ? (totalPnl / recent.length) : 0;
    var avgWin = winCount > 0 ? (winPnl / winCount) : 0;
    var avgLoss = lossCount > 0 ? (lossPnl / lossCount) : 0;
    var ev = recent.length > 0 ? (totalPnl / recent.length) : 0;

    var el = function(id) { return document.getElementById(id); };
    if (el("kpi-edge")) {
        var edgeR = avgLoss > 0 ? (avgWin / avgLoss) : 0;
        el("kpi-edge").textContent = edgeR.toFixed(2) + "R";
        el("kpi-edge").style.color = edgeR >= 1.0 ? "var(--green)" : edgeR >= 0.5 ? "var(--yellow)" : "var(--red)";
    }
    if (el("kpi-edge-wr")) {
        el("kpi-edge-wr").textContent = wr.toFixed(0) + "%";
        el("kpi-edge-wr").style.color = wr >= 60 ? "var(--green)" : wr >= 45 ? "var(--yellow)" : "var(--red)";
    }
    if (el("kpi-edge-ev")) {
        el("kpi-edge-ev").textContent = "$" + ev.toFixed(2);
        el("kpi-edge-ev").style.color = ev >= 0 ? "var(--green)" : "var(--red)";
    }
    if (el("kpi-avg-win")) el("kpi-avg-win").textContent = "$" + avgWin.toFixed(2);
    if (el("kpi-avg-loss")) el("kpi-avg-loss").textContent = "$" + avgLoss.toFixed(2);
    if (el("kpi-paper-count")) el("kpi-paper-count").textContent = closed.length;
}

// ── REAL EDGE (split panel) ──
function updateRealEdge(realStatus) {
    if (!realStatus) return;
    var el = function(id) { return document.getElementById(id); };
    var closed = realStatus.closed_trades || [];
    var totalPnl = parseFloat(realStatus.total_pnl || 0);

    if (closed.length === 0) {
        if (el("kpi-real-edge")) el("kpi-real-edge").textContent = "--R";
        if (el("kpi-real-wr")) el("kpi-real-wr").textContent = "--%";
        if (el("kpi-real-pnl")) { el("kpi-real-pnl").textContent = "$0"; el("kpi-real-pnl").style.color = "var(--text-muted)"; }
        if (el("kpi-real-count")) el("kpi-real-count").textContent = "0";
        if (el("kpi-real-cb")) {
            var cb = realStatus.circuit_breaker || {};
            var tripped = cb.is_tripped || (cb.consecutive_losses || 0) >= 3;
            el("kpi-real-cb").textContent = tripped ? "TRIPPED" : "OK";
            el("kpi-real-cb").style.color = tripped ? "var(--red)" : "var(--green)";
        }
        return;
    }

    var wins = 0, winPnl = 0, lossPnl = 0, winCount = 0, lossCount = 0;
    closed.forEach(function(t) {
        var pnl = parseFloat(t.pnl_usd || 0);
        if (pnl > 0) { wins++; winPnl += pnl; winCount++; }
        else { lossPnl += Math.abs(pnl); lossCount++; }
    });
    var wr = closed.length > 0 ? (wins / closed.length * 100) : 0;
    var avgWin = winCount > 0 ? (winPnl / winCount) : 0;
    var avgLoss = lossCount > 0 ? (lossPnl / lossCount) : 0;
    var edgeR = avgLoss > 0 ? (avgWin / avgLoss) : 0;

    if (el("kpi-real-edge")) {
        el("kpi-real-edge").textContent = edgeR.toFixed(2) + "R";
        el("kpi-real-edge").style.color = edgeR >= 1.0 ? "var(--green)" : edgeR >= 0.5 ? "var(--yellow)" : "var(--red)";
    }
    if (el("kpi-real-wr")) {
        el("kpi-real-wr").textContent = wr.toFixed(0) + "%";
        el("kpi-real-wr").style.color = wr >= 60 ? "var(--green)" : wr >= 45 ? "var(--yellow)" : "var(--red)";
    }
    if (el("kpi-real-pnl")) {
        el("kpi-real-pnl").textContent = "$" + totalPnl.toFixed(2);
        el("kpi-real-pnl").style.color = totalPnl >= 0 ? "var(--green)" : "var(--red)";
    }
    if (el("kpi-real-avg-win")) el("kpi-real-avg-win").textContent = "$" + avgWin.toFixed(2);
    if (el("kpi-real-avg-loss")) el("kpi-real-avg-loss").textContent = "$" + avgLoss.toFixed(2);
    if (el("kpi-real-count")) el("kpi-real-count").textContent = closed.length;
    if (el("kpi-real-cb")) {
        var cb = realStatus.circuit_breaker || {};
        var tripped = cb.is_tripped || (cb.consecutive_losses || 0) >= 3;
        el("kpi-real-cb").textContent = tripped ? "TRIPPED" : "OK";
        el("kpi-real-cb").style.color = tripped ? "var(--red)" : "var(--green)";
    }
}

// ── SIGNAL RADAR ──
var _radarChart = null;

function updateSignalRadar(signals, status) {
    var canvas = document.getElementById("signal-radar-chart");
    var legend = document.getElementById("radar-legend");
    var lockLabel = document.getElementById("radar-lock-label");
    if (!canvas) return;

    // Collect recent scanner triggers from signals
    var scannerData = {};
    var symbols = (status && status.symbols) || [];
    var prices = (status && status.prices) || {};

    // Build per-symbol scanner data from signals
    try {
        var sigs = (signals && Array.isArray(signals)) ? signals.slice(-20) : [];
        sigs.forEach(function(s) {
            var sym = (s.symbol || "").replace("/USDT", "");
            var scanner = (s.metadata || {}).setup_type || (s.metadata || {}).scanner || "unknown";
            var conf = parseFloat(s.confidence || 0);
            var mlProb = parseFloat((s.metadata || {}).ml_probability || 0.5);
            var side = s.side || "?";
            if (conf > 0) {
                var key = sym + "_" + scanner;
                scannerData[key] = {sym: sym, scanner: scanner, conf: conf, mlProb: mlProb, side: side};
            }
        });
    } catch(e) {}

    var entries = Object.values(scannerData);

    // Radar labels = symbols being scanned
    var radarSymbols = symbols.map(function(s) { return s.replace("/USDT", ""); }).slice(0, 10);
    if (radarSymbols.length === 0) radarSymbols = ["BTC", "ETH", "SOL", "XRP"];

    // Build data: conviction per symbol (max conf × ml_prob from recent signals)
    var convictions = radarSymbols.map(function(sym) {
        var best = 0;
        entries.forEach(function(e) {
            if (e.sym === sym) {
                var conviction = (e.conf / 100) * e.mlProb;
                if (conviction > best) best = conviction;
            }
        });
        return Math.round(best * 100);
    });

    try {
        if (_radarChart) _radarChart.destroy();
        _radarChart = new Chart(canvas.getContext("2d"), {
            type: "radar",
            data: {
                labels: radarSymbols,
                datasets: [{
                    label: "Conviction",
                    data: convictions,
                    backgroundColor: "rgba(6,182,212,.12)",
                    borderColor: "rgba(6,182,212,.7)",
                    borderWidth: 1.5,
                    pointRadius: 3,
                    pointBackgroundColor: convictions.map(function(v) {
                        return v > 50 ? "#00ff9d" : v > 25 ? "#06b6d4" : "rgba(255,255,255,.2)";
                    }),
                }],
            },
            options: {
                responsive: false, maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: {
                    r: {
                        min: 0, max: 100,
                        ticks: { display: false, stepSize: 25 },
                        grid: { color: "rgba(255,255,255,.06)" },
                        angleLines: { color: "rgba(255,255,255,.06)" },
                        pointLabels: { color: "rgba(255,255,255,.5)", font: { size: 9, family: "var(--font-mono)" } },
                    },
                },
            },
        });
    } catch(e) {}

    // Legend: top signals
    if (legend) {
        if (entries.length === 0) {
            legend.innerHTML = '<div class="empty" style="font-size:.6rem">No recent signals — scanners running</div>';
        } else {
            var sorted = entries.sort(function(a, b) { return (b.conf * b.mlProb) - (a.conf * a.mlProb); }).slice(0, 6);
            legend.innerHTML = sorted.map(function(e) {
                var conviction = (e.conf / 100 * e.mlProb * 100).toFixed(0);
                var sideColor = e.side === "long" || e.side === "buy" ? "var(--green)" : "var(--red)";
                return '<div style="display:flex;justify-content:space-between;align-items:center;padding:2px 0">' +
                    '<span style="color:' + sideColor + ';font-weight:700">' + e.sym + ' ' + e.side.toUpperCase().charAt(0) + '</span>' +
                    '<span class="text-muted">' + e.scanner.substring(0, 8) + '</span>' +
                    '<span style="font-family:var(--font-mono);color:var(--cyan)">' + conviction + '%</span></div>';
            }).join("");
        }
    }

    // Lock label: strongest signal
    if (lockLabel) {
        if (entries.length > 0) {
            var best = entries[0];
            lockLabel.textContent = "LOCK: " + best.sym + " " + best.side.toUpperCase().charAt(0) + " " +
                (best.conf * best.mlProb / 100 * 100).toFixed(0) + "%";
            lockLabel.style.color = "var(--cyan)";
        } else {
            lockLabel.textContent = "RADAR LOCK: scanning...";
            lockLabel.style.color = "var(--text-muted)";
        }
    }
}

// ── WIRE VISION TIER 1 into main refresh ──
// Uses a wrapper on the existing refreshLive to inject new data
(function wireVisionTier1() {
    // Attention Rail + Signal Radar need signals + status + decision + realStatus
    // These are all available in the main refreshLive function at line ~3180+
    // We hook into the existing funnel/signals update calls
})();

// ══════════════════════════════════════════════════════════
// VISION TIER 2+3: Thesis + Agent Pipeline + Research + Pipeline Trace
// ══════════════════════════════════════════════════════════

// ── THESIS TRACKER ──
async function loadThesis() {
    try {
        var d = await fetch("/api/thesis").then(function(r) { return r.json(); }).catch(function() { return null; });
        var wrap = document.getElementById("thesis-content");
        var badge = document.getElementById("thesis-regime-badge");
        if (!wrap || !d) return;

        var regime = d.regime || "unknown";
        var regimeColors = {
            trending_up: "var(--green)", trending_down: "var(--red)", breakout: "var(--cyan)",
            mean_reversion: "var(--yellow)", ranging: "var(--yellow)",
            sideways: "var(--text-muted)", quiet: "var(--text-muted)", high_volatility: "#f97316",
        };
        var rc = regimeColors[regime] || "var(--text-muted)";

        if (badge) {
            badge.textContent = regime.toUpperCase().replace("_", " ");
            badge.style.background = rc;
            badge.style.color = "#000";
        }

        var confBar = '<div style="height:4px;background:rgba(255,255,255,.06);border-radius:2px;margin:4px 0">' +
            '<div style="height:100%;width:' + (d.regime_confidence * 100).toFixed(0) + '%;background:' + rc + ';border-radius:2px"></div></div>';

        wrap.innerHTML =
            '<div style="color:' + rc + ';font-weight:700;margin-bottom:4px">📋 ' + (d.thesis || "No thesis") + '</div>' +
            confBar +
            '<div style="color:var(--text-muted);font-size:.62rem;margin-top:6px">🔄 <b>Invalidation:</b> ' + (d.invalidation || "--") + '</div>' +
            '<div style="display:flex;gap:12px;margin-top:8px;font-size:.65rem">' +
            '<span class="text-muted">Bias: <b style="color:' + (d.dominant_side === "LONG" ? "var(--green)" : d.dominant_side === "SHORT" ? "var(--red)" : "var(--text-muted)") + '">' + d.dominant_side + '</b></span>' +
            '<span class="text-muted">WR: <b>' + (d.recent_wr || 0).toFixed(0) + '%</b></span>' +
            '<span class="text-muted">PnL: <b style="color:' + (d.recent_pnl >= 0 ? "var(--green)" : "var(--red)") + '">$' + (d.recent_pnl || 0).toFixed(2) + '</b></span>' +
            '<span class="text-muted">BTC: <b>$' + (d.btc_price || 0).toFixed(0) + '</b></span>' +
            '</div>';
    } catch (e) {}
}

// ── MULTI-AGENT PIPELINE ──
async function loadAgentPipeline() {
    try {
        var d = await fetch("/api/agents/pipeline").then(function(r) { return r.json(); }).catch(function() { return null; });
        var wrap = document.getElementById("agent-pipeline");
        if (!wrap || !d || !d.agents) return;

        var statusIcons = {
            scanning: "◉", active: "◉", tracking: "◉", monitoring: "◉", watching: "◉",
            LIVE: "●", ready: "○", idle: "○", disabled: "◌", off: "◌",
            TRIPPED: "⊘", dry_run: "◎",
        };

        wrap.innerHTML = d.agents.map(function(a, idx) {
            var statusIcon = statusIcons[a.status] || "○";
            var isActive = ["scanning", "active", "tracking", "monitoring", "watching", "LIVE"].indexOf(a.status) >= 0;
            var arrow = idx < d.agents.length - 1 ? '<div style="display:flex;align-items:center;color:rgba(255,255,255,.15);font-size:1.2rem;padding:0 2px">→</div>' : '';

            return '<div style="flex:1;min-width:90px;padding:8px 6px;background:rgba(255,255,255,.02);border:1px solid ' +
                (isActive ? a.color + '40' : 'var(--border)') + ';border-top:2px solid ' + (isActive ? a.color : 'var(--border)') +
                ';border-radius:6px;text-align:center">' +
                '<div style="font-size:1.1rem;margin-bottom:2px">' + a.icon + '</div>' +
                '<div style="font-size:.65rem;font-weight:700;color:' + (isActive ? a.color : 'var(--text-muted)') + '">' + a.name + '</div>' +
                '<div style="font-size:.55rem;color:' + (isActive ? a.color : 'var(--text-muted)') + ';margin-top:2px">' +
                statusIcon + ' ' + a.status + '</div>' +
                '<div style="font-size:.5rem;color:var(--text-muted);margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + esc(a.detail || "") + '">' +
                esc((a.detail || "--").substring(0, 25)) + '</div>' +
                '</div>' + arrow;
        }).join("");
    } catch (e) {}
}

// ── PIPELINE TRACE (per-batch drilldown) ──
async function loadPipelineTrace() {
    try {
        var d = await fetch("/api/pipeline/trace?limit=50").then(function(r) { return r.json(); }).catch(function() { return null; });
        var wrap = document.getElementById("pipeline-trace");
        if (!wrap || !d || !d.batches) return;

        if (d.batches.length === 0) {
            wrap.innerHTML = '<div class="empty">No pipeline trace data yet</div>';
            return;
        }

        wrap.innerHTML = d.batches.slice(0, 12).map(function(batch) {
            var total = batch.passed + batch.failed;
            var passRate = total > 0 ? (batch.passed / total * 100) : 0;
            var barColor = passRate >= 60 ? "var(--green)" : passRate >= 30 ? "var(--yellow)" : "var(--red)";
            var ts = new Date(batch.ts * 1000);
            var timeStr = ts.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});

            // Compact signal icons
            var sigIcons = (batch.signals || []).slice(0, 8).map(function(s) {
                var col = s.final_passed ? "var(--green)" : "var(--red)";
                var sym = (s.symbol || "?").replace("/USDT", "").substring(0, 3);
                return '<span style="font-size:.5rem;padding:1px 3px;border-radius:2px;background:' +
                    (s.final_passed ? 'rgba(0,255,157,.1)' : 'rgba(255,59,92,.1)') +
                    ';color:' + col + ';border:1px solid ' + col + '30" title="' +
                    esc(s.symbol + ' ' + (s.side || '') + ' → ' + s.final_stage) + '">' + sym + '</span>';
            }).join(" ");

            return '<div style="display:flex;align-items:center;gap:6px;padding:3px 0;border-bottom:1px solid rgba(255,255,255,.03)">' +
                '<span style="font-size:.6rem;font-family:var(--font-mono);color:var(--text-muted);min-width:42px">' + timeStr + '</span>' +
                '<div style="flex:1;height:8px;background:rgba(255,255,255,.03);border-radius:2px;overflow:hidden;min-width:60px">' +
                '<div style="height:100%;width:' + passRate.toFixed(0) + '%;background:' + barColor + ';border-radius:2px"></div></div>' +
                '<span style="font-size:.55rem;font-family:var(--font-mono);min-width:35px;text-align:right;color:' + barColor + '">' +
                batch.passed + '/' + total + '</span>' +
                '<div style="display:flex;gap:2px;flex-wrap:wrap;max-width:160px">' + sigIcons + '</div>' +
                '</div>';
        }).join("");
    } catch (e) {}
}

// ── RESEARCH ENGINE (correlations + regime transitions) ──
async function loadResearchEngine() {
    try {
        var d = await fetch("/api/research/correlations").then(function(r) { return r.json(); }).catch(function() { return null; });
        var wrap = document.getElementById("research-content");
        if (!wrap || !d) return;

        var html = "";

        // 1. Regime transitions
        var transitions = d.regime_transitions || [];
        if (transitions.length > 0) {
            html += '<div style="font-size:.6rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:1px;font-weight:600;margin-bottom:4px">Regime Status</div>';
            transitions.forEach(function(t) {
                var col = t.signal === "stable" ? "var(--green)" : "var(--yellow)";
                html += '<div style="display:flex;justify-content:space-between;align-items:center;padding:3px 6px;background:rgba(255,255,255,.02);border-radius:3px;border-left:2px solid ' + col + '">' +
                    '<span style="font-size:.65rem;color:' + col + ';font-weight:700">' + (t.current || "?").toUpperCase() + '</span>' +
                    '<span class="metric-label">' + t.signal + ' · conf ' + ((t.confidence || 0) * 100).toFixed(0) + '%</span></div>';
            });
        }

        // 2. Symbol performance heatmap
        var perf = d.symbol_performance || {};
        var symbols = Object.keys(perf);
        if (symbols.length > 0) {
            html += '<div style="font-size:.6rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:1px;font-weight:600;margin-top:8px;margin-bottom:4px">Symbol Performance</div>';
            html += '<div style="display:flex;flex-wrap:wrap;gap:4px">';
            symbols.sort(function(a, b) { return (perf[b].avg_r || 0) - (perf[a].avg_r || 0); });
            symbols.forEach(function(sym) {
                var p = perf[sym];
                var avgR = p.avg_r || 0;
                var wr = p.wr || 0;
                var trades = p.trades || 0;
                var col = avgR > 0 ? "rgba(0,255,157,.15)" : "rgba(255,59,92,.15)";
                var textCol = avgR > 0 ? "var(--green)" : "var(--red)";
                html += '<div style="padding:3px 6px;border-radius:3px;background:' + col + ';border:1px solid ' + textCol + '30;text-align:center;min-width:55px">' +
                    '<div style="font-size:.6rem;font-weight:700;color:' + textCol + '">' + sym.replace("/USDT", "") + '</div>' +
                    '<div style="font-size:.55rem;font-family:var(--font-mono);color:' + textCol + '">' + avgR.toFixed(2) + 'R</div>' +
                    '<div style="font-size:.5rem;color:var(--text-muted)">' + trades + 't · ' + wr.toFixed(0) + '%</div></div>';
            });
            html += '</div>';
        }

        // 3. Scanner co-firing
        var coFiring = d.scanner_co_firing || {};
        var coKeys = Object.keys(coFiring).filter(function(k) { return coFiring[k] > 0.1; });
        if (coKeys.length > 0) {
            html += '<div style="font-size:.6rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:1px;font-weight:600;margin-top:8px;margin-bottom:4px">Scanner Co-Firing</div>';
            coKeys.sort(function(a, b) { return coFiring[b] - coFiring[a]; });
            html += coKeys.slice(0, 5).map(function(k) {
                var v = coFiring[k];
                return '<div style="display:flex;justify-content:space-between;font-size:.6rem;padding:1px 0">' +
                    '<span class="text-muted">' + k + '</span>' +
                    '<span style="font-family:var(--font-mono);color:var(--cyan)">' + (v * 100).toFixed(0) + '%</span></div>';
            }).join("");
        }

        wrap.innerHTML = html || '<div class="empty">No research data</div>';
    } catch (e) {}
}

// ── WIRE TIER 2+3 into refresh cycles ──
// ══════════════════════════════════════════════════════════
// VISION TIER 3: Resolution Clock + Market Map + Catalyst Calendar
// ══════════════════════════════════════════════════════════

// ── RESOLUTION CLOCK ──
// Forward-looking thesis timeline showing key price levels and regime questions
function updateResolutionClock(thesis, prices) {
    var wrap = document.getElementById("resolution-clock");
    if (!wrap) return;

    var btc = (prices && prices["BTC/USDT"]) || 0;
    var eth = (prices && prices["ETH/USDT"]) || 0;
    var regime = (thesis && thesis.regime) || "unknown";
    var conf = (thesis && thesis.regime_confidence) || 0;

    // Build resolution questions based on current state
    var questions = [];

    // BTC key levels
    if (btc > 0) {
        var btcRound = Math.round(btc / 1000) * 1000;
        var btcDist = ((btc - btcRound) / btc * 100).toFixed(2);
        var direction = btc > btcRound ? "above" : "below";
        questions.push({
            question: "BTC hold $" + btcRound.toLocaleString() + "?",
            pressure: Math.max(0, 100 - Math.abs(btcDist) * 20),
            status: direction + " (" + Math.abs(btcDist) + "%)",
            color: btc > btcRound ? "var(--green)" : "var(--red)",
        });
    }

    // Regime stability
    questions.push({
        question: "Regime stable?",
        pressure: conf * 100,
        status: regime.replace("_", " ") + " (" + (conf * 100).toFixed(0) + "%)",
        color: conf > 0.7 ? "var(--green)" : conf > 0.4 ? "var(--yellow)" : "var(--red)",
    });

    // Trend continuation
    var side = (thesis && thesis.dominant_side) || "NEUTRAL";
    var wr = (thesis && thesis.recent_wr) || 0;
    questions.push({
        question: side + " thesis holds?",
        pressure: wr,
        status: "WR " + wr.toFixed(0) + "% (last 20)",
        color: wr >= 60 ? "var(--green)" : wr >= 45 ? "var(--yellow)" : "var(--red)",
    });

    // Session timing
    var hour = new Date().getUTCHours();
    var isActiveSession = (hour >= 3 && hour < 21);
    questions.push({
        question: "Active session?",
        pressure: isActiveSession ? 80 : 20,
        status: isActiveSession ? "YES (UTC " + hour + ":00)" : "low volume (UTC " + hour + ":00)",
        color: isActiveSession ? "var(--green)" : "var(--text-muted)",
    });

    wrap.innerHTML = questions.map(function(q) {
        return '<div style="padding:4px 0;border-bottom:1px solid rgba(255,255,255,.03)">' +
            '<div style="display:flex;justify-content:space-between;font-size:.62rem;margin-bottom:3px">' +
            '<span style="color:var(--text);font-weight:600">' + q.question + '</span>' +
            '<span style="font-family:var(--font-mono);color:' + q.color + '">' + q.pressure.toFixed(0) + '%</span></div>' +
            '<div style="height:4px;background:rgba(255,255,255,.04);border-radius:2px;overflow:hidden">' +
            '<div style="height:100%;width:' + q.pressure.toFixed(0) + '%;background:' + q.color + ';border-radius:2px;transition:width .5s"></div></div>' +
            '<div style="font-size:.5rem;color:var(--text-muted);margin-top:2px">' + q.status + '</div></div>';
    }).join("");
}

// ── MARKET MAP ──
// Capital deployed by symbol/family with visual nodes
async function loadMarketMap() {
    try {
        var d = await fetch("/api/market-map").then(function(r) { return r.json(); }).catch(function() { return null; });
        var wrap = document.getElementById("market-map");
        if (!wrap || !d || !d.symbols) return;

        var families = d.families || {};
        var familyColors = {
            liquid_majors: "#06b6d4", secondary: "#8b5cf6", high_beta: "#f59e0b",
        };

        var html = '<div style="display:flex;gap:12px;flex-wrap:wrap">';

        for (var famName in families) {
            var syms = families[famName] || [];
            var fColor = familyColors[famName] || "var(--text-muted)";
            var famExposure = 0;
            var famPnl = 0;
            var famTrades = 0;

            var nodes = syms.map(function(sym) {
                var s = d.symbols[sym] || {};
                var exposure = (s.paper_exposure || 0) + (s.real_exposure || 0);
                famExposure += exposure;
                famPnl += (s.pnl || 0);
                famTrades += (s.trades || 0);

                var size = Math.max(36, Math.min(70, 36 + exposure / 50));
                var pnlColor = (s.pnl || 0) >= 0 ? "rgba(0,255,157,.15)" : "rgba(255,59,92,.15)";
                var borderColor = (s.pnl || 0) >= 0 ? "rgba(0,255,157,.3)" : "rgba(255,59,92,.3)";
                var coin = sym.replace("/USDT", "");

                return '<div style="width:' + size + 'px;height:' + size + 'px;border-radius:50%;background:' + pnlColor +
                    ';border:1px solid ' + borderColor + ';display:flex;flex-direction:column;align-items:center;justify-content:center;cursor:default" ' +
                    'title="' + sym + ': $' + (s.price || 0).toFixed(2) + ' | ' + (s.trades || 0) + ' trades | WR ' + (s.wr || 0).toFixed(0) + '% | PnL $' + (s.pnl || 0).toFixed(2) + '">' +
                    '<span style="font-size:.55rem;font-weight:800;color:' + fColor + '">' + coin + '</span>' +
                    '<span style="font-size:.45rem;font-family:var(--font-mono);color:var(--text-muted)">' + (s.wr || 0).toFixed(0) + '%</span></div>';
            }).join("");

            html += '<div class="text-center">' +
                '<div style="font-size:.55rem;font-weight:700;color:' + fColor + ';text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px">' +
                famName.replace("_", " ") + '</div>' +
                '<div style="display:flex;gap:4px;flex-wrap:wrap;justify-content:center">' + nodes + '</div>' +
                '<div style="font-size:.5rem;color:var(--text-muted);margin-top:3px">$' + famExposure.toFixed(0) + ' exp · ' +
                famTrades + 't · <span style="color:' + (famPnl >= 0 ? 'var(--green)' : 'var(--red)') + '">$' + famPnl.toFixed(0) + '</span></div></div>';
        }

        html += '</div>';

        // Totals bar
        html += '<div style="display:flex;justify-content:space-between;margin-top:8px;padding-top:6px;border-top:1px solid var(--border);font-size:.6rem">' +
            '<span class="text-muted">Paper: <b class="text-info">$' + (d.total_paper_exposure || 0).toFixed(0) + '</b></span>' +
            '<span class="text-muted">Real: <b class="text-danger">$' + (d.total_real_exposure || 0).toFixed(0) + '</b></span></div>';

        wrap.innerHTML = html;
    } catch (e) {}
}

// ── CATALYST CALENDAR ──
async function loadCatalystCalendar() {
    try {
        var d = await fetch("/api/catalyst-calendar").then(function(r) { return r.json(); }).catch(function() { return null; });
        var wrap = document.getElementById("catalyst-calendar");
        if (!wrap || !d) return;

        var html = "";

        // 1. Current session indicator
        html += '<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 6px;background:rgba(255,255,255,.02);border-radius:3px;margin-bottom:4px">' +
            '<span style="font-size:.6rem;font-weight:700;color:var(--cyan)">' + (d.current_session || "?") + '</span>' +
            '<span style="font-size:.55rem;font-family:var(--font-mono);color:var(--text-muted)">' + (d.utc_time || "") + '</span></div>';

        // 2. Session timeline (compact horizontal bar)
        var sessions = d.sessions || [];
        html += '<div style="display:flex;height:12px;border-radius:3px;overflow:hidden;margin-bottom:6px">';
        sessions.forEach(function(s) {
            var width = ((s.utc_end - s.utc_start) / 24 * 100);
            var bg = s.active ? "rgba(6,182,212,.3)" : "rgba(255,255,255,.03)";
            var border = s.active ? "rgba(6,182,212,.5)" : "rgba(255,255,255,.05)";
            html += '<div style="width:' + width + '%;background:' + bg + ';border-right:1px solid ' + border +
                ';display:flex;align-items:center;justify-content:center" title="' + s.name + ' (UTC ' + s.utc_start + '-' + s.utc_end + ')">' +
                '<span style="font-size:.4rem;color:' + (s.active ? 'var(--cyan)' : 'rgba(255,255,255,.15)') + '">' +
                (width > 10 ? s.name.split(" ")[0] : "") + '</span></div>';
        });
        html += '</div>';

        // 3. Funding rates
        var funding = d.funding || {};
        var fundingKeys = Object.keys(funding);
        if (fundingKeys.length > 0) {
            html += '<div style="font-size:.5rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:.5px;font-weight:600;margin-bottom:3px">Funding Rates</div>';
            fundingKeys.forEach(function(sym) {
                var f = funding[sym];
                var rate = f.rate_8h || 0;
                var col = rate > 0 ? "var(--green)" : rate < 0 ? "var(--red)" : "var(--text-muted)";
                var bias = f.bias === "longs_pay" ? "L pay" : f.bias === "shorts_pay" ? "S pay" : "neutral";
                html += '<div style="display:flex;justify-content:space-between;font-size:.58rem;padding:1px 0">' +
                    '<span class="text-muted">' + sym.replace("/USDT", "") + '</span>' +
                    '<span style="font-family:var(--font-mono);color:' + col + '">' + rate.toFixed(4) + '% <span style="font-size:.5rem">(' + bias + ')</span></span></div>';
            });
        }

        // 4. Upcoming events
        var events = d.upcoming_events || [];
        if (events.length > 0) {
            html += '<div style="font-size:.5rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:.5px;font-weight:600;margin-top:6px;margin-bottom:3px">Upcoming Events</div>';
            events.forEach(function(e) {
                var impactCol = e.impact === "high" ? "var(--red)" : e.impact === "medium" ? "var(--yellow)" : "var(--text-muted)";
                html += '<div style="display:flex;gap:6px;align-items:flex-start;font-size:.55rem;padding:2px 0;border-bottom:1px solid rgba(255,255,255,.02)">' +
                    '<span style="color:var(--text-muted);min-width:42px;font-family:var(--font-mono)">' + e.date.substring(5) + '</span>' +
                    '<span style="width:4px;height:4px;border-radius:50%;background:' + impactCol + ';margin-top:4px;flex-shrink:0"></span>' +
                    '<span style="color:var(--text)">' + e.event + '</span></div>';
                });
        }

        wrap.innerHTML = html || '<div class="empty">No catalyst data</div>';
    } catch (e) {}
}

// ── Wire Tier 3 into Tier 2+3 refresh cycle ──
(function wireVisionTier3() {
    async function loadTier3() {
        try {
            var thesis = await fetch("/api/thesis").then(function(r) { return r.json(); }).catch(function() { return null; });
            var prices = (thesis && thesis.btc_price) ? {"BTC/USDT": thesis.btc_price} : {};
            // Also get full prices from status
            try {
                var st = await fetch("/api/status").then(function(r) { return r.json(); }).catch(function() { return {}; });
                prices = st.prices || prices;
            } catch(e) {}
            updateResolutionClock(thesis, prices);
        } catch (e) {}
        try { await loadMarketMap(); } catch (e) {}
        try { await loadCatalystCalendar(); } catch (e) {}
    }
    setTimeout(loadTier3, 4000);
    setInterval(loadTier3, 20000);  // every 20s (funding rates don't change faster)
})();

(function wireVisionTier23() {
    // Thesis + agents + pipeline trace + research — load every 15s
    async function loadTier23() {
        try { await loadThesis(); } catch (e) {}
        try { await loadAgentPipeline(); } catch (e) {}
        try { await loadPipelineTrace(); } catch (e) {}
        try { await loadResearchEngine(); } catch (e) {}
    }
    setTimeout(loadTier23, 3000); // initial load after 3s
    setInterval(loadTier23, 15000); // refresh every 15s
})();

// ITEM #10: COMMAND CENTER 2.0 — Quick Actions
// ══════════════════════════════════════════════════════════

async function ccTogglePause() {
    var btn = document.getElementById("cc-pause-btn");
    var isPaused = btn.dataset.paused === "true";
    var endpoint = isPaused ? "/api/control/resume" : "/api/control/pause";
    try {
        await fetch(endpoint, { method: "POST" });
        btn.dataset.paused = isPaused ? "false" : "true";
        btn.innerHTML = isPaused ? "&#x23F8; Pause" : "&#x25B6; Resume";
    } catch (e) { console.warn("Pause toggle error:", e); }
}

async function ccToggleReal() {
    var label = document.getElementById("cc-real-label");
    var isOn = label.textContent !== "OFF";
    try {
        await fetch("/api/real/toggle", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ enabled: !isOn }),
        });
        label.textContent = isOn ? "OFF" : "ON";
        label.parentElement.style.borderColor = isOn ? "rgba(255,59,92,.3)" : "rgba(0,255,157,.3)";
        label.parentElement.style.color = isOn ? "var(--red)" : "var(--green)";
    } catch (e) { console.warn("Real toggle error:", e); }
}

async function ccEmergencyStop() {
    if (!confirm("EMERGENCY STOP: disable ALL trading. Continue?")) return;
    try {
        await fetch("/api/emergency-stop", { method: "POST" });
        var btn = document.getElementById("cc-estop-btn");
        btn.style.opacity = "0.4";
        btn.textContent = "STOPPED";
    } catch (e) { alert("Emergency stop failed: " + e.message); }
}

// Update CC 2.0 real label + live PnL on every refresh
function updateCC20(status, realStatus) {
    try {
        var lbl = document.getElementById("cc-real-label");
        if (lbl && realStatus) {
            lbl.textContent = realStatus.enabled ? "ON" : "OFF";
        }
        var pnlEl = document.getElementById("cc-live-pnl");
        if (pnlEl && status) {
            var pnl = parseFloat(status.paper_pnl_usd || status.daily_pnl || 0);
            pnlEl.textContent = "$" + pnl.toFixed(2);
            pnlEl.style.color = pnl >= 0 ? "var(--green)" : "var(--red)";
        }
    } catch (e) {}
}

// ══════════════════════════════════════════════════════════
// ITEMS #6+8: CONFIG EDITOR + HOT-RELOAD
// ══════════════════════════════════════════════════════════

async function toggleConfigEditor() {
    var editor = document.getElementById("config-editor");
    var snapshot = document.getElementById("config-snapshot");
    var btn = document.getElementById("config-edit-btn");
    if (editor.style.display === "none") {
        try {
            var r = await fetch("/api/config");
            var d = await r.json();
            document.getElementById("config-json").value = JSON.stringify(d, null, 2);
            editor.style.display = "block";
            snapshot.style.display = "none";
            btn.textContent = "Cancel";
        } catch (e) {
            alert("Failed to load config: " + e.message);
        }
    } else {
        cancelConfigEdit();
    }
}

function cancelConfigEdit() {
    document.getElementById("config-editor").style.display = "none";
    document.getElementById("config-snapshot").style.display = "block";
    document.getElementById("config-edit-btn").textContent = "Edit";
    document.getElementById("config-save-msg").style.display = "none";
}

async function saveConfig() {
    var msg = document.getElementById("config-save-msg");
    try {
        var text = document.getElementById("config-json").value;
        var parsed = JSON.parse(text);
        var r = await fetch("/api/config", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(parsed),
        });
        var d = await r.json();
        if (d.ok) {
            msg.textContent = "Saved & reloaded: " + (d.keys_updated || []).join(", ");
            msg.style.color = "var(--green)";
            msg.style.display = "block";
            setTimeout(cancelConfigEdit, 2000);
        } else {
            msg.textContent = "Error: " + (d.error || "unknown");
            msg.style.color = "var(--red)";
            msg.style.display = "block";
        }
    } catch (e) {
        msg.textContent = "Invalid JSON or network error: " + e.message;
        msg.style.color = "var(--red)";
        msg.style.display = "block";
    }
}

// ══════════════════════════════════════════════════════════
// ITEM #11: GRID BOT VISUAL MAP
// ══════════════════════════════════════════════════════════

async function refreshGridBot() {
    try {
        var status = await fetch("/api/grid/status").then(function(r) { return r.json(); }).catch(function() { return { enabled: false }; });
        var badge = document.getElementById("grid-status-badge");
        var disabledMsg = document.getElementById("grid-disabled-msg");
        var activeContent = document.getElementById("grid-active-content");
        if (!badge) return;

        if (!status.enabled) {
            badge.textContent = "PAUSED";
            badge.style.background = "var(--text-muted)";
            if (disabledMsg) disabledMsg.style.display = "block";
            if (activeContent) activeContent.style.display = "none";
            return;
        }

        badge.textContent = "ACTIVE";
        badge.style.background = "var(--green)";
        if (disabledMsg) disabledMsg.style.display = "none";
        if (activeContent) activeContent.style.display = "block";

        var s = status;
        var el = function(id) { return document.getElementById(id); };
        if (el("grid-total-fills")) el("grid-total-fills").textContent = s.total_fills || 0;
        if (el("grid-profit")) el("grid-profit").textContent = "$" + (s.total_profit || 0).toFixed(2);
        if (el("grid-fees")) el("grid-fees").textContent = "$" + (s.total_fees || 0).toFixed(2);
        if (el("grid-fills-hr")) el("grid-fills-hr").textContent = (s.fills_per_hour || 0).toFixed(1);
        if (el("grid-profit-hr")) el("grid-profit-hr").textContent = "$" + (s.profit_per_hour || 0).toFixed(2);

        // Grid ladders visualization
        var ladders = document.getElementById("grid-ladders");
        if (ladders && s.symbols) {
            var html = "";
            for (var sym in s.symbols) {
                var info = s.symbols[sym] || {};
                var center = info.center || info.last_price || 0;
                var openCount = info.open || 0;
                var dec = sym.includes("BTC") ? 2 : 4;
                html += '<div style="padding:8px;border:1px solid var(--border);border-radius:6px">' +
                    '<div style="font-weight:700;font-size:.72rem">' + esc(sym.replace("/USDT","")) + '</div>' +
                    '<div style="font-size:.62rem;color:var(--cyan)">Center: $' + center.toFixed(dec) + '</div>' +
                    '<div style="font-size:.62rem;color:var(--text-muted)">Open: ' + openCount + '</div>' +
                    '</div>';
            }
            ladders.innerHTML = html || '<div class="empty">No grid symbols</div>';
        }
    } catch (e) {}
}

// ── BRAIN TAB ────────────────────────────────────────────
async function refreshBrainTab() {
    // Only fetch if Brain tab is active (avoid unnecessary API calls)
    var brainTab = document.getElementById("tab-brain");
    if (!brainTab || brainTab.style.display === "none") return;

    try {
        // Fetch all brain endpoints in parallel
        var [stateRes, matrixRes, hourlyRes, regimeRes, sessionsRes] = await Promise.all([
            fetch("/api/brain/state").then(function(r){return r.ok?r.json():null}).catch(function(){return null}),
            fetch("/api/brain/matrix").then(function(r){return r.ok?r.json():null}).catch(function(){return null}),
            fetch("/api/brain/hourly-heatmap").then(function(r){return r.ok?r.json():null}).catch(function(){return null}),
            fetch("/api/brain/regime-history").then(function(r){return r.ok?r.json():null}).catch(function(){return null}),
            fetch("/api/brain/sessions").then(function(r){return r.ok?r.json():null}).catch(function(){return null}),
        ]);

        // 1. Brain Status
        if (stateRes) {
            var modeEl = document.getElementById("brain-mode");
            if (modeEl) modeEl.textContent = stateRes.dry_run ? "DRY RUN" : "LIVE";
            if (modeEl) modeEl.style.color = stateRes.dry_run ? "#bf00ff" : "#00ff88";
            var obsEl = document.getElementById("brain-observations");
            if (obsEl) obsEl.textContent = (stateRes.total_observations || 0).toLocaleString();
            var cellsEl = document.getElementById("brain-cells");
            if (cellsEl) cellsEl.textContent = (stateRes.matrix_cells || 0).toLocaleString();
            var dirEl = document.getElementById("brain-directives");
            if (dirEl) dirEl.textContent = stateRes.directives_issued || 0;

            // Active directives
            var adDiv = document.getElementById("brain-active-directives");
            if (adDiv && stateRes.active_directives) {
                var ad = stateRes.active_directives;
                var lines = [];
                if (ad.trading_paused) lines.push('<span style="color:#ff4444">PAUSED: ' + ad.pause_reason + '</span>');
                if (ad.suppressed_scanners && ad.suppressed_scanners.length > 0) lines.push('Suppressed scanners: <span style="color:#ff8800">' + ad.suppressed_scanners.join(", ") + '</span>');
                if (ad.suppressed_hours && ad.suppressed_hours.length > 0) lines.push('Suppressed hours: <span style="color:#ff8800">' + ad.suppressed_hours.join(", ") + 'h UTC</span>');
                if (Object.keys(ad.side_penalties || {}).length > 0) lines.push('Side penalties: ' + JSON.stringify(ad.side_penalties));
                if (stateRes.bad_hours && stateRes.bad_hours.length > 0) lines.push('Bad hours detected: <span style="color:#ff6666">' + stateRes.bad_hours.join(", ") + 'h</span>');
                if (stateRes.best_hours && stateRes.best_hours.length > 0) lines.push('Best hours: <span style="color:#00ff88">' + stateRes.best_hours.join(", ") + 'h</span>');
                adDiv.innerHTML = lines.length > 0 ? lines.join("<br>") : '<span style="color:#666">No active directives (dry_run mode)</span>';
            }

            // Today's session
            if (stateRes.session) {
                var s = stateRes.session;
                var tEl = document.getElementById("brain-today-trades"); if(tEl) tEl.textContent = s.trades || 0;
                var wrEl = document.getElementById("brain-today-wr"); if(wrEl) { wrEl.textContent = s.wr ? s.wr + "%" : "--"; wrEl.style.color = (s.wr||0)>=55?"#00ff88":"#ff4444"; }
                var pnlEl = document.getElementById("brain-today-pnl"); if(pnlEl) { pnlEl.textContent = "$" + (s.pnl_usd||0).toFixed(2); pnlEl.style.color = (s.pnl_usd||0)>=0?"#00ff88":"#ff4444"; }
                var warmEl = document.getElementById("brain-warm"); if(warmEl) { warmEl.textContent = s.is_warm ? "YES" : "COLD"; warmEl.style.color = s.is_warm?"#00ff88":"#ff8800"; }
            }

            // Weekly summary
            if (stateRes.weekly) {
                var w = stateRes.weekly;
                var wDiv = document.getElementById("brain-weekly-summary");
                if (wDiv && w.days > 0) {
                    wDiv.innerHTML = 'Week: ' + w.days + 'd | ' + w.total_trades + ' trades | WR ' + (w.wr||0) + '% | PnL <span style="color:' + ((w.total_pnl||0)>=0?"#00ff88":"#ff4444") + '">$' + (w.total_pnl||0).toFixed(2) + '</span>';
                }
            }

            // Optimizer
            if (stateRes.optimizer) {
                var opt = stateRes.optimizer;
                var optDiv = document.getElementById("brain-optimizer-state");
                if (optDiv) {
                    if (!opt.enabled) {
                        optDiv.innerHTML = '<div style="text-align:center;color:#666;padding:10px">Disabled (Phase 7)</div>';
                    } else {
                        var phtml = '';
                        for (var pn in (opt.params||{})) {
                            var p = opt.params[pn];
                            phtml += '<div style="margin-bottom:6px"><strong>' + pn + '</strong>: ' + p.current + ' <span style="color:#666">(default: ' + p.default + ', range: ' + p.min + '-' + p.max + ')</span><br>Trades at current: ' + p.trades_at_current + ' | Adjustments: ' + p.total_adjustments + '</div>';
                        }
                        optDiv.innerHTML = phtml || 'No params active';
                    }
                }
            }
        }

        // 2. Setup x Regime Matrix
        if (matrixRes && Object.keys(matrixRes).length > 0) {
            document.getElementById("brain-matrix-empty").style.display = "none";
            var regimes = new Set(); var setups = new Set();
            for (var k in matrixRes) { var parts = k.split(":"); setups.add(parts[0]); regimes.add(parts[1]); }
            var regArr = Array.from(regimes).sort();
            var setArr = Array.from(setups).sort();
            var hdr = '<tr><th style="text-align:left;padding:4px 8px;border-bottom:1px solid #333;color:#aaa">Setup</th>';
            regArr.forEach(function(r){ hdr += '<th style="padding:4px 8px;border-bottom:1px solid #333;color:#aaa;font-size:11px">' + r.replace("_"," ") + '</th>'; });
            hdr += '</tr>';
            document.querySelector("#brain-matrix-table thead").innerHTML = hdr;
            var bdy = '';
            setArr.forEach(function(setup){
                bdy += '<tr><td style="padding:4px 8px;border-bottom:1px solid #222;font-weight:bold;color:#00d4ff">' + setup.replace("_"," ") + '</td>';
                regArr.forEach(function(regime){
                    var cell = matrixRes[setup + ":" + regime];
                    if (cell && cell.sample_count > 0) {
                        var wr = cell.win_rate;
                        var bg = wr >= 60 ? "rgba(0,255,136,0.15)" : wr >= 45 ? "rgba(255,200,0,0.12)" : "rgba(255,68,68,0.15)";
                        var clr = wr >= 60 ? "#00ff88" : wr >= 45 ? "#ffcc00" : "#ff4444";
                        bdy += '<td style="padding:4px 8px;border-bottom:1px solid #222;text-align:center;background:' + bg + ';color:' + clr + ';font-size:12px">' + wr.toFixed(0) + '% <span style="color:#666;font-size:10px">(' + cell.sample_count + ')</span></td>';
                    } else {
                        bdy += '<td style="padding:4px 8px;border-bottom:1px solid #222;text-align:center;color:#333">-</td>';
                    }
                });
                bdy += '</tr>';
            });
            document.getElementById("brain-matrix-body").innerHTML = bdy;
        }

        // 3. Hourly Heatmap
        if (hourlyRes) {
            var grid = document.getElementById("brain-hourly-grid");
            if (grid) {
                var hhtml = '';
                for (var h = 0; h < 24; h++) {
                    var hc = hourlyRes[h] || hourlyRes[String(h)];
                    if (hc && hc.sample_count > 0) {
                        var wr = hc.win_rate;
                        var bg = wr >= 60 ? "rgba(0,255,136,0.25)" : wr >= 45 ? "rgba(255,200,0,0.2)" : "rgba(255,68,68,0.25)";
                        var clr = wr >= 60 ? "#00ff88" : wr >= 45 ? "#ffcc00" : "#ff4444";
                        hhtml += '<div style="background:' + bg + ';border:1px solid #333;border-radius:4px;padding:4px;text-align:center;font-size:10px"><div style="color:#888">' + h + 'h</div><div style="color:' + clr + ';font-weight:bold">' + wr.toFixed(0) + '%</div><div style="color:#555">' + hc.sample_count + '</div></div>';
                    } else {
                        hhtml += '<div style="background:#111;border:1px solid #222;border-radius:4px;padding:4px;text-align:center;font-size:10px"><div style="color:#555">' + h + 'h</div><div style="color:#333">-</div></div>';
                    }
                }
                grid.innerHTML = hhtml;
            }
        }

        // 4. Regime History
        if (regimeRes && regimeRes.symbols) {
            var rDiv = document.getElementById("brain-regime-container");
            if (rDiv) {
                var rhtml = '<table style="width:100%;border-collapse:collapse;font-size:12px"><thead><tr><th style="text-align:left;padding:4px 8px;color:#aaa">Symbol</th><th class="px-2 py-1 text-muted">Current</th><th class="px-2 py-1 text-muted">Confidence</th><th class="px-2 py-1 text-muted">Predicted Next</th><th class="px-2 py-1 text-muted">Probability</th></tr></thead><tbody>';
                for (var sym in regimeRes.symbols) {
                    var si = regimeRes.symbols[sym];
                    var pred = (regimeRes.predictions||{})[sym] || {};
                    var regColor = {"trending_up":"#00ff88","trending_down":"#ff4444","breakout":"#00d4ff","ranging":"#ffcc00","sideways":"#ffcc00","volatile":"#ff8800","mean_reversion":"#bf00ff","quiet":"#666"}[si.current] || "#aaa";
                    rhtml += '<tr><td style="padding:4px 8px;border-bottom:1px solid #222;font-weight:bold">' + sym + '</td>';
                    rhtml += '<td style="padding:4px 8px;border-bottom:1px solid #222;color:' + regColor + '">' + (si.current||"--").replace("_"," ") + '</td>';
                    rhtml += '<td class="px-2 py-1 border-b">' + (si.current_confidence ? (si.current_confidence*100).toFixed(0)+"%" : "--") + '</td>';
                    rhtml += '<td style="padding:4px 8px;border-bottom:1px solid #222;color:#888">' + (pred.predicted||"--").replace("_"," ") + '</td>';
                    rhtml += '<td class="px-2 py-1 border-b">' + (pred.probability ? (pred.probability*100).toFixed(0)+"%" : "--") + '</td></tr>';
                }
                rhtml += '</tbody></table>';
                rDiv.innerHTML = rhtml;
            }
        }

        // 5. Daily Sessions
        if (sessionsRes && sessionsRes.daily_summaries && sessionsRes.daily_summaries.length > 0) {
            var sDiv = document.getElementById("brain-sessions-container");
            if (sDiv) {
                var shtml = '<table style="width:100%;border-collapse:collapse;font-size:12px"><thead><tr><th style="text-align:left;padding:4px 8px;color:#aaa">Date</th><th class="px-2 py-1 text-muted">Trades</th><th class="px-2 py-1 text-muted">WR%</th><th class="px-2 py-1 text-muted">PnL</th><th class="px-2 py-1 text-muted">Best Scanner</th><th class="px-2 py-1 text-muted">Regime</th></tr></thead><tbody>';
                sessionsRes.daily_summaries.forEach(function(ds){
                    var pclr = (ds.pnl||0) >= 0 ? "#00ff88" : "#ff4444";
                    shtml += '<tr><td class="px-2 py-1 border-b">' + ds.date + '</td>';
                    shtml += '<td style="padding:4px 8px;border-bottom:1px solid #222;text-align:center">' + ds.trades + '</td>';
                    shtml += '<td style="padding:4px 8px;border-bottom:1px solid #222;text-align:center;color:' + ((ds.wr||0)>=55?"#00ff88":"#ff4444") + '">' + (ds.wr||0) + '%</td>';
                    shtml += '<td style="padding:4px 8px;border-bottom:1px solid #222;text-align:center;color:' + pclr + '">$' + (ds.pnl||0).toFixed(2) + '</td>';
                    shtml += '<td class="px-2 py-1 border-b">' + (ds.best_scanner||"-").replace("_"," ") + '</td>';
                    shtml += '<td class="px-2 py-1 border-b">' + (ds.dominant_regime||"-").replace("_"," ") + '</td></tr>';
                });
                shtml += '</tbody></table>';
                sDiv.innerHTML = shtml;
            }
        }
    } catch(e) { console.error("refreshBrainTab:", e); }
}

// ── INFRA HEALTH: Proxy CB + Cron Sync + OB Cache ────────
async function refreshInfraHealth() {
    try {
        var d = await fetch("/api/infra/health").then(function(r) { return r.json(); }).catch(function() { return null; });
        if (!d) return;

        // Helper: inline KV rendering (avoids scope issues with makeKV in different tab context)
        function kv(label, value, color) {
            var c = color || "var(--text)";
            return '<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px solid rgba(255,255,255,.03)">' +
                '<span class="text-sm text-muted">' + label + '</span>' +
                '<span style="font-size:.75rem;font-weight:600;color:' + c + ';font-family:var(--font-mono)">' + (value || "--") + '</span></div>';
        }

        // 1. ML Proxy Health
        var proxyWrap = document.getElementById("proxy-health");
        if (proxyWrap && d.ml_proxy) {
            var p = d.ml_proxy;
            var cbColor = p.cb_open ? "var(--red)" : "var(--green)";
            var cbText = p.cb_open ? "OPEN (" + (p.cb_remaining_sec || 0) + "s)" : "CLOSED";
            var cacheHtml = "";
            try {
                var entries = p.cache_entries || {};
                Object.keys(entries).forEach(function(path) {
                    var e = entries[path] || {};
                    var age = parseFloat(e.age_sec || 0);
                    var shortPath = path.split("/").pop() || path;
                    cacheHtml += '<div style="display:flex;justify-content:space-between;font-size:.6rem;padding:1px 0"><span class="text-muted">' +
                        shortPath + '</span><span style="font-family:var(--font-mono);color:' +
                        (age < 30 ? 'var(--green)' : 'var(--yellow)') + '">' + age.toFixed(0) + 's ago</span></div>';
                });
            } catch(e) {}
            proxyWrap.innerHTML =
                kv("Circuit Breaker", cbText, cbColor) +
                kv("Failures", String(p.cb_failures || 0), (p.cb_failures || 0) > 0 ? "var(--yellow)" : "var(--green)") +
                kv("Cache Entries", String(p.cache_size || 0)) + cacheHtml;
        }

        // 2. Cron Sync Monitor
        var syncWrap = document.getElementById("sync-monitor");
        if (syncWrap && d.cron_sync) {
            var s = d.cron_sync;
            var syncAge = parseFloat(s.age_sec || 999999);
            var syncColor = syncAge < 900 ? "var(--green)" : syncAge < 1800 ? "var(--yellow)" : "var(--red)";
            var syncText = s.last_sync ? (Math.floor(syncAge / 60) + "m ago") : "never";
            syncWrap.innerHTML =
                kv("Last Sync", syncText, syncColor) +
                kv("Sync Time", s.last_sync || "--") +
                kv("Feedback Records", String(s.feedback_lines || 0)) +
                kv("Training Records", String(s.trades_lines || 0)) +
                kv("Log Exists", s.log_exists ? "Yes" : "No", s.log_exists ? "var(--green)" : "var(--red)");
        }

        // 3. Orderbook Cache
        var obWrap = document.getElementById("ob-cache-health");
        if (obWrap && d.orderbook_cache) {
            var ob = d.orderbook_cache;
            var running = ob.running;
            obWrap.innerHTML =
                kv("Status", running ? "RUNNING" : "OFF", running ? "var(--green)" : "var(--red)") +
                kv("Symbols", String(ob.symbols || 0)) +
                kv("Cached", String(ob.cached || 0)) +
                kv("Fetches", String(ob.fetch_count || 0)) +
                kv("Errors", String(ob.error_count || 0), (ob.error_count || 0) > 10 ? "var(--red)" : "var(--green)");
        }
    } catch (e) { console.error("refreshInfraHealth error:", e); }
}
// Self-init: also load infra health independently every 10s
// (in case refreshSystem fails or System tab isn't clicked)
(function() {
    setTimeout(function() { refreshInfraHealth().catch(function(){}); }, 5000);
    setInterval(function() { refreshInfraHealth().catch(function(){}); }, 10000);
})();

// ── #13 DAILY PNL BAR CHART ──────────────────────────────
let dailyPnlChart = null;
function updateDailyPnlChart(closed) {
    if (!closed || !Array.isArray(closed) || closed.length === 0) return;
    const daily = {};
    closed.forEach(t => {
        const d = (t.closed_at || t.exit_time || "").substring(0, 10);
        if (!d) return;
        if (!daily[d]) daily[d] = 0;
        daily[d] += (t.pnl_usd || 0);
    });
    const days = Object.keys(daily).sort();
    const values = days.map(d => daily[d]);
    const colors = values.map(v => v >= 0 ? "rgba(0,255,157,.7)" : "rgba(255,59,92,.7)");
    const borderColors = values.map(v => v >= 0 ? "#00ff9d" : "#ff3b5c");
    const ctx = document.getElementById("daily-pnl-chart");
    if (!ctx) return;
    if (dailyPnlChart) dailyPnlChart.destroy();
    dailyPnlChart = new Chart(ctx.getContext("2d"), {
        type: "bar",
        data: {
            labels: days.map(d => d.substring(5)),
            datasets: [{label:"Daily PnL ($)", data:values, backgroundColor:colors, borderColor:borderColors, borderWidth:1, borderRadius:2}]
        },
        options: {
            responsive:true, maintainAspectRatio:false,
            plugins:{legend:{display:false},tooltip:{backgroundColor:"rgba(22,26,37,.95)",borderColor:"rgba(99,148,255,.15)",borderWidth:1}},
            scales:{x:{grid:{display:false},ticks:{color:"#5f6a7d",font:{size:9}}},y:{grid:{color:"rgba(255,255,255,.03)"},ticks:{color:"#5f6a7d",font:{size:9},callback:v=>"$"+v.toFixed(0)}}}
        }
    });
}

// ── #8 FEE IMPACT UPDATE ─────────────────────────────────
function updateFeeImpact(closed) {
    if (!closed || !Array.isArray(closed) || closed.length === 0) return;
    let totalFees = 0, grossPnl = 0;
    closed.forEach(t => {
        totalFees += (t.total_fees_usd || 0);
        grossPnl += Math.abs(t.pnl_usd || 0);
    });
    const feePct = grossPnl > 0 ? (totalFees / grossPnl * 100) : 0;
    const avgFee = closed.length > 0 ? (totalFees / closed.length) : 0;
    const el = id => document.getElementById(id);
    if (el("fi-total-fees")) el("fi-total-fees").textContent = "$" + totalFees.toFixed(2);
    if (el("fi-fee-pct")) el("fi-fee-pct").textContent = feePct.toFixed(1) + "%";
    if (el("fi-avg-fee")) el("fi-avg-fee").textContent = "$" + avgFee.toFixed(3);
}

// ── #5 ML ACCURACY TRACKER ──────────────────────────────
function updateMLAccuracy(closed) {
    if (!closed || !Array.isArray(closed) || closed.length === 0) return;
    let takeWins = 0, takeTrades = 0, weakWins = 0, weakTrades = 0;
    closed.forEach(t => {
        const mlVerdict = (t.metadata && t.metadata.ml_verdict) || "";
        const mlProb = (t.metadata && t.metadata.ml_probability) || 0;
        const isWin = (t.pnl_usd || 0) > 0;
        if (mlVerdict.toLowerCase().includes("take") || mlProb >= 0.6) {
            takeTrades++;
            if (isWin) takeWins++;
        } else if (mlVerdict.toLowerCase().includes("weak") || (mlProb > 0 && mlProb < 0.45)) {
            weakTrades++;
            if (isWin) weakWins++;
        }
    });
    const takeWR = takeTrades > 0 ? (takeWins/takeTrades*100) : 0;
    const weakWR = weakTrades > 0 ? (weakWins/weakTrades*100) : 0;
    const el = id => document.getElementById(id);
    if (el("ml-take-wr")) el("ml-take-wr").textContent = takeTrades > 0 ? takeWR.toFixed(1) + "%" : "--";
    if (el("ml-take-count")) el("ml-take-count").textContent = takeTrades + " trades";
    if (el("ml-weak-wr")) el("ml-weak-wr").textContent = weakTrades > 0 ? weakWR.toFixed(1) + "%" : "--";
    if (el("ml-weak-count")) el("ml-weak-count").textContent = weakTrades + " trades";
    // Color coding
    if (el("ml-take-wr")) el("ml-take-wr").style.color = takeWR >= 55 ? "var(--green)" : takeWR >= 45 ? "var(--yellow)" : "var(--red)";
    if (el("ml-weak-wr")) el("ml-weak-wr").style.color = weakWR < 40 ? "var(--green)" : weakWR < 50 ? "var(--yellow)" : "var(--red)";
}

// ── #12 TRADE CORRELATION (Paper vs Real) ────────────────
function updateTradeCorrelation(closed, realStatus) {
    // Paper stats
    if (closed && Array.isArray(closed) && closed.length > 0) {
        let pWins = 0, pTotal = 0, pRs = [];
        closed.forEach(t => {
            pTotal++;
            if ((t.pnl_usd||0) > 0) pWins++;
            pRs.push(t.exit_r || t.r_multiple || 0);
        });
        const pWR = pTotal > 0 ? (pWins/pTotal*100) : 0;
        const pAvgR = pRs.length > 0 ? pRs.reduce((a,b)=>a+b,0)/pRs.length : 0;
        const el = id => document.getElementById(id);
        if (el("corr-paper-wr")) el("corr-paper-wr").textContent = pWR.toFixed(1) + "%";
        if (el("corr-paper-r")) el("corr-paper-r").textContent = pAvgR.toFixed(3) + "R";
    }
    // Real stats from closed real trades
    const realTrades = (realStatus && (realStatus.live_trades || realStatus.recent_trades)) || [];
    if (realTrades.length > 0) {
        let rWins = 0, rTotal = 0, rPnls = [];
        realTrades.forEach(t => {
            rTotal++;
            if ((t.pnl_usd||0) > 0) rWins++;
            rPnls.push(t.pnl_pct || 0);
        });
        const rWR = rTotal > 0 ? (rWins/rTotal*100) : 0;
        const rAvgR = rPnls.length > 0 ? rPnls.reduce((a,b)=>a+b,0)/rPnls.length : 0;
        const el = id => document.getElementById(id);
        if (el("corr-real-wr")) el("corr-real-wr").textContent = rWR.toFixed(1) + "%";
        if (el("corr-real-r")) el("corr-real-r").textContent = rAvgR.toFixed(2) + "%";
    }
    // B.4/B.5: feed real trades into the Scanner Performance panel
    try { updateRealScannerPerf(realTrades); } catch (e) {}
}

// Load config when tab is switched
const _origSwitchTab = switchTab;
switchTab = function(tab) {
    _origSwitchTab(tab);
    if (tab === "config") {
        populateConfigTab();
        loadTradeTypePerf();
    }
    if (tab === "profile") {
        refreshProfile();
    }
};

// ── INIT ─────────────────────────────────────────────────
switchTab("live");

// ═══ BLOCK 2 (original lines 7327-7431) ═══
function openTradeDetail(trade) {
    if (!trade) return;
    const m = document.getElementById("trade-modal");
    const title = document.getElementById("tm-title");
    const body = document.getElementById("tm-body");
    const sym = trade.symbol || trade.trade_id || "?";
    const side = trade.side || "?";
    const sideColor = side === "long" ? "var(--green)" : "var(--red)";
    title.innerHTML = sym + " <span style='color:" + sideColor + "'>" + side.toUpperCase() + "</span>";

    const entry = trade.entry_price || 0;
    const exit = trade.exit_price || 0;
    const pnl = trade.pnl_usd || trade.pnl || 0;
    const pnlPct = trade.pnl_pct || 0;
    const r = trade.exit_r || 0;
    const mfe = trade.mfe_r || 0;
    const mae = trade.mae_r || 0;
    const sl = trade.stop_loss || 0;
    const tp1 = trade.tp1 || 0;
    const conf = trade.confidence || 0;
    const scanner = trade.setup_type || trade.scanner || "?";
    const exitReason = trade.exit_reason || "?";
    const ml = trade.ml_probability || 0;
    const regime = trade.regime || "?";
    const dur = trade.duration_sec || 0;
    const durMin = (dur / 60).toFixed(1);
    const tradeType = trade.trade_type || "?";
    const ts = trade.timestamp || "";

    const pnlColor = pnl >= 0 ? "var(--green)" : "var(--red)";
    const efficiency = mfe > 0 ? ((r / mfe) * 100).toFixed(0) : "—";

    body.innerHTML = "" +
        "<div style='display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px'>" +
        "<div class='metric'><span class='label'>Entry</span><span class='value'>" + entry.toFixed(4) + "</span></div>" +
        "<div class='metric'><span class='label'>Exit</span><span class='value'>" + exit.toFixed(4) + "</span></div>" +
        "<div class='metric'><span class='label'>Stop Loss</span><span class='value' style='color:var(--red)'>" + sl.toFixed(4) + "</span></div>" +
        "<div class='metric'><span class='label'>TP1</span><span class='value' style='color:var(--green)'>" + (tp1 > 0 ? tp1.toFixed(4) : "—") + "</span></div>" +
        "</div>" +
        "<div style='display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px'>" +
        "<div style='text-align:center;padding:8px;background:rgba(255,255,255,.03);border-radius:6px'><div style='font-size:1.1rem;font-weight:700;color:" + pnlColor + "'>$" + pnl.toFixed(2) + "</div><div style='font-size:.6rem;color:var(--text-muted)'>PNL</div></div>" +
        "<div style='text-align:center;padding:8px;background:rgba(255,255,255,.03);border-radius:6px'><div style='font-size:1.1rem;font-weight:700;color:" + pnlColor + "'>" + r.toFixed(2) + "R</div><div style='font-size:.6rem;color:var(--text-muted)'>R-MULTIPLE</div></div>" +
        "<div style='text-align:center;padding:8px;background:rgba(255,255,255,.03);border-radius:6px'><div style='font-size:1.1rem;font-weight:700'>" + efficiency + "%</div><div style='font-size:.6rem;color:var(--text-muted)'>EXIT EFF</div></div>" +
        "</div>" +
        "<div style='display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px'>" +
        "<div class='metric'><span class='label'>MFE (Peak)</span><span class='value' style='color:var(--green)'>" + mfe.toFixed(3) + "R</span></div>" +
        "<div class='metric'><span class='label'>MAE (Worst)</span><span class='value' style='color:var(--red)'>" + mae.toFixed(3) + "R</span></div>" +
        "<div class='metric'><span class='label'>Scanner</span><span class='value'>" + scanner + "</span></div>" +
        "<div class='metric'><span class='label'>Exit Reason</span><span class='value'>" + exitReason + "</span></div>" +
        "<div class='metric'><span class='label'>Confidence</span><span class='value'>" + conf + "/100</span></div>" +
        "<div class='metric'><span class='label'>ML Score</span><span class='value'>" + (ml * 100).toFixed(0) + "%</span></div>" +
        "<div class='metric'><span class='label'>Regime</span><span class='value'>" + regime + "</span></div>" +
        "<div class='metric'><span class='label'>Type</span><span class='value'>" + tradeType + "</span></div>" +
        "<div class='metric'><span class='label'>Duration</span><span class='value'>" + durMin + " min</span></div>" +
        "<div class='metric'><span class='label'>Time</span><span class='value' style='font-size:.65rem'>" + ts.substring(0,19) + "</span></div>" +
        "</div>" +
        "<div style='margin-top:8px;padding:8px;background:rgba(255,255,255,.02);border-radius:4px;font-size:.7rem;color:var(--text-dim)'>" +
        "<strong>Lifecycle:</strong> Entry " + entry.toFixed(2) + " → Peak +" + mfe.toFixed(2) + "R → Worst -" + mae.toFixed(2) + "R → Exit " + r.toFixed(2) + "R (" + exitReason + ")" +
        "</div>";

    m.style.display = "flex";
}

// --- Dashboard Header Update ---
function updateDashboardHeader(trkStats, closed, decision) {
    if (trkStats) {
        var daily = trkStats.daily_pnl || {};
        var today = new Date().toISOString().slice(0, 10);
        var td = daily[today] || {};
        var pnl = td.net_pnl || 0;
        var pnlEl = document.getElementById("cmd-today-pnl");
        if (pnlEl) {
            pnlEl.textContent = (pnl < 0 ? "-" : "") + "$" + Math.abs(pnl).toFixed(2);
            pnlEl.style.color = pnl >= 0 ? "var(--green)" : "var(--red)";
        }
        var wrEl = document.getElementById("cmd-today-wr");
        if (wrEl) wrEl.textContent = (td.wr || 0).toFixed(0) + "%";
        var trEl = document.getElementById("cmd-today-trades");
        if (trEl) trEl.textContent = td.trades || 0;
        var feEl = document.getElementById("cmd-today-fees");
        if (feEl) feEl.textContent = "$" + (td.fees || 0).toFixed(0);
    }
    if (decision) {
        var macroEl = document.getElementById("cmd-macro-bias");
        if (macroEl) {
            var bias = decision.macro_bias_str || "NEUTRAL";
            macroEl.textContent = String(bias).toUpperCase();
            macroEl.style.color = bias === "bullish" ? "var(--green)" : bias === "bearish" ? "var(--red)" : "var(--text-muted)";
        }
    }
    if (closed && closed.length > 0) {
        var t = closed[closed.length - 1];
        var lpnl = t.pnl_usd || 0;
        var sym = (t.symbol || "?").split("/")[0];
        var side = t.side || "?";
        var reason = t.exit_reason || "?";
        var el = document.getElementById("cmd-last-trade");
        if (el) {
            var color = lpnl >= 0 ? "var(--green)" : "var(--red)";
            el.innerHTML = '<span style="font-weight:700;color:' + color + '">$' + (lpnl >= 0 ? "+" : "") + lpnl.toFixed(2) + '</span> <span class="text-muted">' + sym + ' ' + side + '</span> <span style="font-size:.65rem;color:var(--text-muted)">' + reason + '</span>';
        }
    }
}


// ═══ BLOCK 3 (original lines 7449-7768) ═══
// Dashboard header auto-update (clean rewrite)

function switchRecentClosed(tab) {
    document.getElementById("rc-paper-wrap").style.display = tab === "paper" ? "" : "none";
    document.getElementById("rc-real-wrap").style.display = tab === "real" ? "" : "none";
    document.getElementById("rc-tab-paper").style.background = tab === "paper" ? "rgba(0,212,255,.15)" : "transparent";
    document.getElementById("rc-tab-paper").style.color = tab === "paper" ? "var(--cyan)" : "var(--text-muted)";
    document.getElementById("rc-tab-paper").style.borderColor = tab === "paper" ? "rgba(0,212,255,.3)" : "var(--border)";
    document.getElementById("rc-tab-real").style.background = tab === "real" ? "rgba(255,59,92,.15)" : "transparent";
    document.getElementById("rc-tab-real").style.color = tab === "real" ? "var(--red)" : "var(--text-muted)";
    document.getElementById("rc-tab-real").style.borderColor = tab === "real" ? "rgba(255,59,92,.3)" : "var(--border)";
}


function updatePnlCalendar(trkStats) {
    var cal = document.getElementById("pnl-calendar");
    if (!cal || !trkStats) return;
    var daily = trkStats.daily_pnl || {};
    var days = Object.keys(daily).sort();
    if (days.length === 0) { cal.innerHTML = '<div class="empty" style="grid-column:1/-1;font-size:.65rem">No data</div>'; return; }

    // Get last 35 days (5 weeks)
    var today = new Date();
    var cells = [];
    for (var i = 34; i >= 0; i--) {
        var d = new Date(today);
        d.setDate(d.getDate() - i);
        var key = d.toISOString().slice(0, 10);
        var dow = d.getDay(); // 0=Sun
        var td = daily[key] || null;
        cells.push({date: key, dow: dow, data: td, day: d.getDate()});
    }

    // Pad start to align with Monday (dow=1)
    var firstDow = cells[0].dow;
    var padStart = firstDow === 0 ? 6 : firstDow - 1; // Monday-based

    var html = "";
    for (var p = 0; p < padStart; p++) {
        html += '<div style="aspect-ratio:1;border-radius:3px"></div>';
    }

    for (var ci = 0; ci < cells.length; ci++) {
        var c = cells[ci];
        var bg, color, title;
        if (!c.data) {
            bg = "rgba(255,255,255,.03)";
            color = "var(--text-muted)";
            title = c.date + ": No trades";
        } else {
            var pnl = c.data.net_pnl || 0;
            var wr = c.data.wr || 0;
            var trades = c.data.trades || 0;
            if (pnl < -10) {
                bg = "rgba(255,59,92,.7)"; color = "#fff";
            } else if (pnl < 0) {
                bg = "rgba(255,59,92,.35)"; color = "var(--red)";
            } else if (pnl < 50) {
                bg = "rgba(0,255,157,.2)"; color = "var(--green)";
            } else if (pnl < 200) {
                bg = "rgba(0,255,157,.45)"; color = "var(--green)";
            } else {
                bg = "rgba(0,255,157,.75)"; color = "#fff";
            }
            title = c.date + ": $" + (pnl>=0?"+":"") + pnl.toFixed(0) + " | " + trades + " trades | " + wr.toFixed(0) + "% WR";
        }

        html += '<div style="aspect-ratio:1;background:' + bg + ';border-radius:3px;display:flex;align-items:center;justify-content:center;color:' + color + ';font-weight:600;font-family:var(--font-mono);cursor:default;font-size:.55rem" title="' + title + '">' + c.day + '</div>';
    }

    cal.innerHTML = html;
}


function updateSessionSummary(closed) {
    if (!closed || closed.length === 0) return;
    // IST = UTC + 5:30
    var sessions = {
        "asia_early": {h:[0,1,2,3], w:0, n:0},     // 5:30-9:30 IST = 0-4 UTC
        "india":      {h:[4,5,6,7,8], w:0, n:0},    // 9:30-14:00 IST = 4-8:30 UTC
        "europe":     {h:[9,10,11,12,13], w:0, n:0}, // 14:00-19:30 IST = 8:30-14 UTC
        "us":         {h:[14,15,16,17,18,19,20,21,22,23], w:0, n:0}  // 19:30-5:30 IST
    };
    for (var i = 0; i < closed.length; i++) {
        var t = closed[i];
        var ts = t.exit_time || t.timestamp || "";
        if (!ts || ts.length < 13) continue;
        var h = parseInt(ts.substring(11, 13));
        var pnl = t.pnl_usd || 0;
        for (var sk in sessions) {
            if (sessions[sk].h.indexOf(h) >= 0) {
                sessions[sk].n++;
                if (pnl > 0) sessions[sk].w++;
                break;
            }
        }
    }
    var ids = {"asia_early":"sess-asia-early","india":"sess-india","europe":"sess-europe","us":"sess-us"};
    for (var sk2 in ids) {
        var s = sessions[sk2];
        var wr = s.n > 0 ? Math.round(s.w / s.n * 100) : 0;
        var el = document.getElementById(ids[sk2]);
        if (el) {
            el.textContent = s.n > 0 ? wr + "%" : "--";
            el.style.color = wr >= 70 ? "var(--green)" : wr >= 50 ? "var(--yellow)" : s.n > 0 ? "var(--red)" : "var(--text-muted)";
        }
        var nel = document.getElementById(ids[sk2] + "-n");
        if (nel) nel.textContent = s.n + " trades";
    }
}


// REMOVED: switchAnalyticsAccount() — dead code, buttons hidden (display:none).
// Analytics paper/real toggle handled by the Scanner Performance panel (B.4 setScannerView).
window._analyticsMode = "paper";

async function dashUpdate() {
  try {
    // Fetch all data in parallel
    var results = await Promise.all([
      fetch("/api/tracker/stats").then(function(r){return r.json()}).catch(function(){return {}}),
      fetch("/api/tracker/closed").then(function(r){return r.json()}).catch(function(){return []}),
      fetch("/api/real/status").then(function(r){return r.json()}).catch(function(){return {}}),
      fetch("/api/decision").then(function(r){return r.json()}).catch(function(){return {}})
    ]);
    var stats = results[0];
    var closed = results[1];
    var real = results[2];
    var decision = results[3];

    // 1. Today paper performance
    var daily = stats.daily_pnl || {};
    var today = new Date().toISOString().slice(0,10);
    var td = daily[today] || {};
    var pnl = td.net_pnl || 0;
    var e1 = document.getElementById("cmd-today-pnl");
    if(e1){e1.textContent=(pnl<0?"-":"")+"$"+Math.abs(pnl).toFixed(2);e1.style.color=pnl>=0?"var(--green)":"var(--red)";}
    var e2=document.getElementById("cmd-today-wr");if(e2)e2.textContent=(td.wr||0).toFixed(0)+"%";
    var e3=document.getElementById("cmd-today-trades");if(e3)e3.textContent=td.trades||0;
    var e4=document.getElementById("cmd-today-fees");if(e4)e4.textContent="$"+(td.fees||0).toFixed(0);

    // 2. Real trading performance
    var cb = real.circuit_breaker || {};
    var rpnl = cb.daily_pnl || 0;
    var rpe = document.getElementById("cmd-real-pnl");
    if(rpe){rpe.textContent=(rpnl<0?"-":rpnl>0?"+":"")+"$"+Math.abs(rpnl).toFixed(2);rpe.style.color=rpnl>=0?"var(--green)":"var(--red)";}
    var rbe=document.getElementById("cmd-real-bal");if(rbe)rbe.textContent="$"+(real.balance||0).toFixed(2);
    var rte=document.getElementById("cmd-real-trades");if(rte)rte.textContent=cb.trade_count_today||0;
    var rle=document.getElementById("cmd-real-total");
    if(rle){var tp=cb.total_pnl||0;rle.textContent=(tp<0?"-":"+")+"$"+Math.abs(tp).toFixed(2);rle.style.color=tp>=0?"var(--green)":"var(--red)";}

    // 3. Header bar updates
    var ppMini = document.getElementById("paper-bal-mini");
    if(ppMini && stats.paper_balance) ppMini.textContent = "$" + stats.paper_balance.toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2});
    var sigMini = document.getElementById("signals-mini-count");
    if(sigMini) sigMini.textContent = stats.total_signals || 0;
    // 3. Exchange balance
    var eb=document.getElementById("exchange-bal");if(eb&&stats.exchange_balance)eb.textContent="$"+stats.exchange_balance.toFixed(2);

    // 4. Per-symbol regime from closed trades metadata
    var rc={"trending_up":"#00ff9d","trending_down":"#ff3b5c","sideways":"#ffd700","ranging":"#ffd700",
      "breakout":"#00d4ff","volatile":"#f97316","high_volatility":"#f97316","quiet":"#5a7090",
      "mean_reversion":"#a78bfa","low_liquidity":"#ff3b5c"};
    if(closed&&closed.length>0){
      var sr={};
      for(var i=closed.length-1;i>=0;i--){
        var ct=closed[i];var s=ct.symbol||"";var m=ct.metadata||{};var rg=m.regime||"";
        if(s&&rg&&!sr[s])sr[s]=rg;
      }
      ["btc","eth","sol","xrp"].forEach(function(sym){
        var reg=sr[sym.toUpperCase()+"/USDT"]||"--";
        var el=document.getElementById("cmd-regime-"+sym);
        if(el){el.textContent=reg;el.style.color=rc[reg]||"#9ba3b5";}
      });
      // Also update scanner status cards with regime
      window._symRegimes = sr;
    }

    // 5. Session + macro bias from decision
    var se=document.getElementById("cmd-session");if(se&&decision.session)se.textContent=decision.session;
    var me=document.getElementById("cmd-macro-bias");
    if(me){var b=decision.macro_bias_str||"NEUTRAL";me.textContent=String(b).toUpperCase();
      me.style.color=b==="bullish"?"var(--green)":b==="bearish"?"var(--red)":"var(--text-muted)";}

    // 6. Last trade with duration + time ago
    if(closed&&closed.length>0){
      var lt=closed[closed.length-1];var lp=lt.pnl_usd||0;
      var le=document.getElementById("cmd-last-trade");
      if(le){
        var c=lp>=0?"var(--green)":"var(--red)";
        var sym=(lt.symbol||"?").split("/")[0];
        var ago="";
        if(lt.exit_time){var diff=Math.round((Date.now()-new Date(lt.exit_time).getTime())/1000);
          if(diff<60)ago=diff+"s ago";else if(diff<3600)ago=Math.floor(diff/60)+"m ago";
          else if(diff<86400)ago=Math.floor(diff/3600)+"h ago";else ago=Math.floor(diff/86400)+"d ago";}
        var dur=Math.round(lt.trade_duration_sec||lt.duration_sec||0);
        var ds=dur>0?(dur<60?dur+"s":Math.floor(dur/60)+"m"):"";
        le.innerHTML='<span style="font-weight:700;color:'+c+'">$'+(lp>=0?"+":"")+lp.toFixed(2)+'</span> '+sym+' '+(lt.side||"?")+'<br><span style="font-size:.62rem;color:var(--text-muted)">'+(lt.exit_reason||"?")+(ds?' \u00B7 held '+ds:'')+(ago?' \u00B7 '+ago:'')+'</span>';
      }
    }
    // 7. Last real trade
    if(real&&real.recent_trades&&real.recent_trades.length>0){
      var rt=real.recent_trades[real.recent_trades.length-1];
      var rp=rt.pnl_usd||0;
      var rle2=document.getElementById("cmd-last-real-trade");
      if(rle2){
        var rc2=rp>=0?"var(--green)":"var(--red)";
        var rsym=(rt.symbol||"?").split("/")[0];
        var rago2="";
                if(rt.timestamp){var rd2=Math.round((Date.now()-new Date(rt.timestamp).getTime())/1000);
                if(rd2<60)rago2=rd2+"s ago";else if(rd2<3600)rago2=Math.floor(rd2/60)+"m ago";
                else if(rd2<86400)rago2=Math.floor(rd2/3600)+"h ago";else rago2=Math.floor(rd2/86400)+"d ago";}
                rle2.innerHTML='<span style="font-weight:700;color:'+rc2+'">$'+(rp>=0?"+":"")+rp.toFixed(2)+'</span> '+rsym+' '+(rt.side||"?")+'<br><span style="font-size:.62rem;color:var(--text-muted)">'+(rt.exit_reason||rt.reason||"--")+(rago2?' \u00B7 '+rago2:'')+'</span>';
      }
    } else {
      var rle3=document.getElementById("cmd-last-real-trade");
      if(rle3&&real&&real.total_closed>0)rle3.innerHTML='<span class="text-muted">No trades today</span>';
    }

    // 8. Market highlight bar
    var mhBal=document.getElementById("mh-paper-bal");
    if(mhBal&&stats.paper_balance)mhBal.textContent="$"+stats.paper_balance.toFixed(2);
    // Peak: calculate from paper balance + gross pnl history
    var mhPeak=document.getElementById("mh-peak");
    if(mhPeak){
        var peakVal=stats.paper_balance||1000;
        var daily=stats.daily_pnl||{};
        var runBal=stats.paper_start_balance||1000;
        var maxBal=runBal;
        var days=Object.keys(daily).sort();
        for(var di=0;di<days.length;di++){runBal+=(daily[days[di]].net_pnl||0);if(runBal>maxBal)maxBal=runBal;}
        mhPeak.textContent="$"+maxBal.toFixed(2);
        // Drawdown
        var mhDD=document.getElementById("mh-dd");
        if(mhDD){var ddp=maxBal>0?((maxBal-(stats.paper_balance||0))/maxBal*100):0;mhDD.textContent=ddp.toFixed(1)+"%";mhDD.style.color=ddp>2?"var(--red)":"var(--green)";}
    }
    var mhPF=document.getElementById("mh-pf");
    if(mhPF&&stats.profit_factor)mhPF.textContent=stats.profit_factor.toFixed(2);
    // Avg R from r_metrics
    var mhAR=document.getElementById("mh-avgr");
    var rm=stats.r_metrics||{};
    if(mhAR&&rm.avg_r!=null){var ar=rm.avg_r;mhAR.textContent=(ar>=0?"+":"")+ar.toFixed(3)+"R";mhAR.style.color=ar>=0?"var(--green)":"var(--red)";}


    // 9. Recent real closed trades
    try {
      var rrb = document.getElementById("recent-real-closed-body");
      if (rrb && real && real.recent_trades) {
        var rt = real.recent_trades || [];
        if (rt.length === 0) {
          rrb.innerHTML = '<tr><td colspan="9" class="empty">No real trades</td></tr>';
        } else {
          rrb.innerHTML = rt.slice(-10).reverse().map(function(t) {
            var p = t.pnl_usd || 0;
            var pc = p >= 0 ? "var(--green)" : "var(--red)";
            var dt = (t.timestamp || t.closed_at || t.opened_at || "").replace("T"," ").slice(5,16);
            var reason = t.reason || t.exit_reason || "--";
            var margin = t.margin || 0;
            var slip = t.slippage_bps || 0;
            var scanner = t.scanner || t.trade_type || "--";
            return '<tr>' +
              '<td class="text-sm">' + dt + '</td>' +
              '<td class="font-semibold">' + (t.symbol||"?") + '</td>' +
              '<td><span style="color:' + (t.side==="long"?"var(--green)":"var(--red)") + '">' + (t.side||"?").toUpperCase() + '</span></td>' +
              '<td style="font-family:var(--font-mono);font-size:.7rem">' + (t.entry_price||0).toFixed(4) + '</td>' +
              '<td style="font-family:var(--font-mono);font-size:.7rem">' + (t.exit_price||0).toFixed(4) + '</td>' +
              '<td>' + slip.toFixed(0) + 'bp</td>' +
              '<td style="color:' + pc + ';font-weight:600">$' + (p>=0?"+":"") + p.toFixed(2) + '</td>' +
              '<td>$' + margin.toFixed(0) + '</td>' +
              '<td class="text-sm">' + reason + '</td></tr>';
          }).join("");
        }
      }
    } catch(e) {}

  
    // 10. Trade Diagnostic — why no trades?
    try {
      var diag = document.getElementById("trade-diagnostic");
      if (diag) {
        var fr = await fetch("/api/opportunity-funnel").then(function(r){return r.json()}).catch(function(){return {}});
        var dec = await fetch("/api/decision").then(function(r){return r.json()}).catch(function(){return {}});
        var f = fr.funnel || {};
        var vs = fr.veto_stats || {};
        var reasons = [];

        // Check paper trading status
        var hasActive = document.querySelectorAll("#active-trades-cards .trade-card").length > 0;
        if (hasActive) {
            diag.innerHTML = '<span class="text-success">\u2705 Paper trade active</span>';
        } else {
            // Build diagnostic from funnel + decision data
            if (f.scanned === 0) {
                reasons.push('<span class="text-danger">No candles processed yet</span>');
            } else {
                if (f.rejected > 0) reasons.push('<span class="text-warning">' + f.rejected + ' signals below threshold</span>');
                if (f.blocked_regime > 0) reasons.push('<span class="text-danger">' + f.blocked_regime + ' blocked by regime</span>');
                if (f.blocked_ml > 0) reasons.push('<span class="text-orange">' + f.blocked_ml + ' blocked by ML</span>');
            }
            // Veto breakdown
            for (var vk in vs) {
                if (vs[vk] > 0) reasons.push('<span class="text-muted">' + vk + ': ' + vs[vk] + '</span>');
            }
            // Real trade specific
            var cb2 = (real && real.circuit_breaker) || {};
            if (cb2.is_tripped) reasons.push('<span class="text-danger">\u26A0 Circuit breaker TRIPPED</span>');

            if (reasons.length === 0 && f.scanned > 0) {
                reasons.push('<span class="text-info">Scanning... ' + f.scanned + ' checked, waiting for setup</span>');
            }

            diag.innerHTML = reasons.join(' <span class="text-dim">|</span> ');
        }
      }
    } catch(e) {}

    } catch(err) { console.error("dashUpdate error:", err); }
}
document.addEventListener("DOMContentLoaded", function() { dashUpdate(); setInterval(() => { if(!window._refreshLiveActive) dashUpdate(); }, 3000); });

// ═══ BLOCK 4 (original lines 7827-9126) ═══
async function showJourney(tradeId) {
  if (!tradeId) return;
  const modal = document.getElementById('journey-modal');
  const body = document.getElementById('journey-body');
  const idEl = document.getElementById('journey-trade-id');
  modal.style.display = 'flex';
  idEl.textContent = 'Trade ID: ' + tradeId;
  body.innerHTML = '<div style="color:var(--text-muted);font-size:.75rem">Loading...</div>';
  try {
    const r = await fetch('/api/pipeline/journey/' + encodeURIComponent(tradeId));
    const d = await r.json();
    if (!d.found) {
      body.innerHTML = '<div style="color:var(--text-muted);font-size:.75rem">No journey recorded yet (trade may still be active)</div>';
      return;
    }
    const jny = d.journey;
    const stages = jny.stages || [];
    const stageColors = {strategy:'#6366f1',hard_block:'#ef4444',risk_check:'#f59e0b',signal_tracker:'#0ea5e9',paper_exec:'#10b981',real_qualify:'#f59e0b',real_exec:'#00ff9d',exit:'#00d4ff'};
    body.innerHTML = [
      `<div style="font-size:.72rem;color:var(--text-muted);margin-bottom:4px">${jny.symbol||''} ${jny.side||''} · ${jny.grade||''} · ${stages.length} stages · ${(jny.total_ms||0).toFixed(0)}ms total</div>`,
      ...stages.map((s,i) => {
        const color = stageColors[s.stage] || 'var(--text-dim)';
        const icon = s.passed ? '✓' : '✗';
        const bg = s.passed ? 'rgba(16,185,129,.07)' : 'rgba(239,68,68,.07)';
        return `<div style="display:flex;align-items:flex-start;gap:10px;padding:7px 10px;border-radius:6px;background:${bg};border:1px solid ${s.passed ? 'rgba(16,185,129,.15)' : 'rgba(239,68,68,.15)'}">
          <span style="font-weight:700;font-size:.9rem;color:${s.passed?'var(--green)':'var(--red)'}">${icon}</span>
          <div style="flex:1">
            <div class="flex justify-between items-center">
              <span style="font-weight:700;font-size:.75rem;color:${color}">${s.stage}</span>
              <span style="font-size:.6rem;color:var(--text-muted);font-family:var(--font-mono)">+${s.latency_ms.toFixed(0)}ms</span>
            </div>
            <div style="font-size:.68rem;color:var(--text-dim);margin-top:2px">${s.reason||''}</div>
          </div>
        </div>`;
      })
    ].join('');
  } catch(e) {
    body.innerHTML = '<div style="color:var(--red);font-size:.75rem">Error: ' + e.message + '</div>';
  }
}
document.addEventListener('DOMContentLoaded', function() {
  var jm = document.getElementById('journey-modal');
  if (jm) jm.addEventListener('click', function(e) { if (e.target === this) this.style.display = 'none'; });
});

// ══════════════════════════════════════════════════════════
// Pipeline Observability panel (Live tab) — restored yesterday layout
// ══════════════════════════════════════════════════════════
async function renderPipelineObs() {
  try {
    const ltHours = window._ltWindowHours != null ? window._ltWindowHours : 24;
    const [pm, sup, rstat, hotfix, taxonomy] = await Promise.all([
      fetch('/api/pipeline/overview').then(r => r.json()).catch(() => ({})),
      fetch('/api/supervisor/status').then(r => r.json()).catch(() => ({})),
      fetch('/api/real/status').then(r => r.json()).catch(() => ({})),
      fetch('/api/pipeline/hotfix_stats').then(r => r.json()).catch(() => ({})),
      fetch(`/api/pipeline/loss_taxonomy?hours=${ltHours}`).then(r => r.json()).catch(() => ({})),
    ]);

    // Phase 3.3: Loss Taxonomy panel
    try { renderLossTaxonomy(taxonomy); } catch(_){}

    if (!pm || !pm.funnel) return;

    // ── Phase 3.2: Hotfix Effectiveness rows ──
    try {
      const hfEl = document.getElementById('po-hotfix-rows');
      if (hfEl) {
        const fixes = (hotfix && hotfix.ok && hotfix.fixes) || null;
        if (!fixes) {
          hfEl.innerHTML = '<div style="color:var(--text-muted);font-size:.68rem">No data</div>';
        } else {
          const formatAge = (s) => {
            if (s == null) return 'never';
            if (s < 60) return s.toFixed(0) + 's';
            if (s < 3600) return (s/60).toFixed(1) + 'm';
            if (s < 86400) return (s/3600).toFixed(1) + 'h';
            return (s/86400).toFixed(1) + 'd';
          };
          const mapping = [
            {key: 'p0_lowconf_bear_htf', label: 'P0 SB bear-HTF', color: '#ef4444'},
            {key: 'p0_8_momentum_counter_htf', label: 'P0.8 mom-HTF', color: '#dc2626'},
            {key: 'p1_duplicate_exit_match', label: 'P1 dup-exit', color: '#f59e0b'},
            {key: 'p2_counter_htf_boost_cap', label: 'P2 cap-boost', color: '#8b5cf6'},
            {key: 'p3_orphan_prevention', label: 'P3 orphan', color: '#10b981'},
            {key: 'p3_6_ml_weak_block', label: 'P3.6 ML-weak', color: '#a78bfa'},
            {key: 'p3_7_sideways_sb_long', label: 'P3.7 sideways-SB', color: '#e11d48'},
            {key: 'p3_9_limit_no_fill', label: 'P3.9 no-fill save', color: '#14b8a6'},
            {key: 'p3_11_chop_long_block', label: 'P3.11 chop-long', color: '#f43f5e'},
            {key: 'p4_fee_drag_chop', label: 'P4 fee-chop', color: '#06b6d4'},
          ];
          hfEl.innerHTML = mapping.map(m => {
            const d = fixes[m.key] || {};
            const blocked = d.blocked || 0;
            const age = d.age_sec;
            const status = d.status || 'dormant';
            const countColor = blocked > 0 ? m.color : 'var(--text-muted)';
            const ageText = blocked > 0 ? formatAge(age) : '—';
            return `<div class="flex justify-between items-center"><span class="text-muted"><span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:${countColor};margin-right:5px;box-shadow:${blocked>0?'0 0 4px '+m.color:'none'}"></span>${m.label}</span><span><span style="color:${countColor};font-weight:800">${blocked}</span><span style="color:var(--text-muted);font-size:.6rem;margin-left:6px">${ageText}</span></span></div>`;
          }).join('');
          // Summary line
          const sum = fixes._summary || {};
          const totalBlocked = sum.total_blocked || 0;
          hfEl.innerHTML += `<div style="border-top:1px dotted rgba(255,255,255,.05);margin-top:4px;padding-top:3px;display:flex;justify-content:space-between;font-size:.6rem"><span class="text-muted">total blocked</span><span style="color:${totalBlocked>0?'#818cf8':'var(--text-muted)'};font-weight:700">${totalBlocked}</span></div>`;
        }
      }
    } catch(_){}

    // Timestamp
    const tsEl = document.getElementById('po-timestamp');
    if (tsEl) {
      const d = new Date();
      tsEl.textContent = d.toTimeString().slice(0,8);
    }

    // ── Col 1: Stage-Loss Map rows ──
    const f = pm.funnel || {};
    const rowsEl = document.getElementById('po-stage-rows');
    if (rowsEl) {
      const r = (label, val, color) =>
        `<div class="flex justify-between"><span class="text-muted">${label}</span><span style="color:${color||'var(--text-dim)'};font-weight:700">${(val||0).toLocaleString()}</span></div>`;
      rowsEl.innerHTML = [
        r('Scanned', f.scanned, 'var(--cyan)'),
        r('Tier Valid', f.tier_valid, 'var(--green)'),
        r('Near Miss', f.tier_near_miss || f.near_miss, 'var(--yellow)'),
        r('Blocked (regime)', f.blocked_regime, 'var(--red)'),
        '<div style="border-top:1px dashed rgba(255,255,255,.08);margin:4px 0"></div>',
        r('Paper Emitted', f.paper_emitted, 'var(--green)'),
        r('Real Qualified', f.real_qualified, 'var(--yellow)'),
        r('Real Rejected', f.real_rejected, 'var(--red)'),
        '<div style="border-top:1px dashed rgba(255,255,255,.08);margin:4px 0"></div>',
        r('Real Executed', f.real_executed, 'var(--accent)'),
        r('Anti-slip Rej', f.real_anti_slip_rejected, 'var(--red)'),
      ].join('');
    }

    // ── Col 2: Rejection Leaderboard ──
    const rejEl = document.getElementById('po-reject-rows');
    if (rejEl) {
      const rejs = (pm.rejections && pm.rejections.top) || [];
      if (rejs.length) {
        rejEl.innerHTML = rejs.slice(0,5).map(x =>
          `<div style="display:flex;justify-content:space-between;padding:1px 0;border-bottom:1px solid rgba(255,255,255,.04)"><span style="color:var(--text-dim);font-size:.68rem;overflow:hidden;text-overflow:ellipsis;max-width:140px;white-space:nowrap">${x.reason||'?'}</span><span class="text-danger font-bold">${x.count||0}</span></div>`
        ).join('');
      } else {
        rejEl.innerHTML = '<div style="color:var(--text-muted);font-size:.7rem;padding:8px 0">No rejections today</div>';
      }
    }

    // ── Col 3: Agents & CB ──
    const cbEl = document.getElementById('po-cb-rows');
    if (cbEl) {
      const cb = pm.circuit_breaker || {};
      const agents = pm.agents || {};
      // P0: show real-trading enabled state FIRST (most important)
      const realEnabled = rstat && rstat.enabled;
      const realMode = (rstat && rstat.mode) || 'UNKNOWN';
      const modeColor = !realEnabled ? 'var(--red)' : (rstat && rstat.dry_run ? 'var(--cyan)' : 'var(--green)');
      // CB state
      const cbColor = cb.is_tripped ? 'var(--red)' : (realEnabled ? 'var(--green)' : 'var(--text-muted)');
      const cbLabel = cb.is_tripped ? 'TRIPPED' : (realEnabled ? 'OK' : 'idle');
      const pnlColor = (cb.daily_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)';
      const totalPnl = rstat && rstat.total_pnl != null ? rstat.total_pnl : (cb.total_pnl||0);
      const totalColor = totalPnl >= 0 ? 'var(--green)' : 'var(--red)';
      // Phase 3.5: probation badge
      const prob = sup && sup.probation;
      let probBadge = '';
      if (prob && prob.active) {
        const remT = prob.remaining_trades || 0;
        const remH = ((prob.remaining_age_sec || 0) / 3600).toFixed(1);
        probBadge = `<span style="font-size:.55rem;padding:1px 5px;border-radius:3px;background:rgba(255,215,0,.12);color:var(--yellow);font-weight:800;margin-left:6px">PROB ${(prob.size_mult*100).toFixed(0)}% · ${remT}t/${remH}h</span>`;
      }
      const rows = [
        `<div style="display:flex;justify-content:space-between;align-items:center;padding:2px 4px;background:${!realEnabled?'rgba(255,59,92,.08)':'transparent'};border-radius:3px"><span style="color:var(--text-muted);font-weight:700">Real Trading</span><span style="display:flex;align-items:center"><span style="color:${modeColor};font-weight:800">${realMode}</span>${probBadge}</span></div>`,
        `<div class="flex justify-between"><span class="text-muted">Circuit Breaker</span><span style="color:${cbColor};font-weight:700">${cbLabel}</span></div>`,
        `<div class="flex justify-between"><span class="text-muted">Consec losses</span><span style="color:var(--text-dim);font-weight:700">${cb.consecutive_losses||0} / 3</span></div>`,
        `<div class="flex justify-between"><span class="text-muted">Daily PnL</span><span style="color:${pnlColor};font-weight:700">$${(cb.daily_pnl||0).toFixed(2)}</span></div>`,
        `<div class="flex justify-between"><span class="text-muted">Total PnL</span><span style="color:${totalColor};font-weight:700">$${totalPnl.toFixed(2)}</span></div>`,
        `<div class="flex justify-between"><span class="text-muted">Daily limit</span><span style="color:var(--text-dim);font-weight:700">$${(cb.daily_loss_limit||15).toFixed(0)}</span></div>`,
        '<div style="border-top:1px dashed rgba(255,255,255,.08);margin:4px 0"></div>',
      ];
      const keys = Object.keys(agents);
      if (keys.length) {
        // Phase 2.5 B1: Upgrade from plain rows to role-state labels
        keys.forEach(k => {
          const a = agents[k] || {};
          const age = a.last_seen_sec || 0;
          const st = (a.status || 'unknown').toLowerCase();
          // Derive WORKING/IDLE/STALE from heartbeat age
          let roleLabel, dotColor;
          if (st !== 'ok' && st !== 'stale') {
            roleLabel = 'OFFLINE';
            dotColor = 'var(--red)';
          } else if (age < 5) {
            roleLabel = 'WORKING';
            dotColor = 'var(--green)';
          } else if (age < 30) {
            roleLabel = 'IDLE';
            dotColor = 'var(--cyan)';
          } else if (age < 120) {
            roleLabel = 'STALE';
            dotColor = 'var(--yellow)';
          } else {
            roleLabel = 'DEAD';
            dotColor = 'var(--red)';
          }
          rows.push(`<div class="flex justify-between items-center"><span style="color:var(--text-muted);display:flex;align-items:center;gap:5px"><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${dotColor};box-shadow:0 0 5px ${dotColor}"></span>${k}</span><span class="flex items-center gap-2"><span style="color:var(--text-muted);font-size:.62rem">${age.toFixed(0)}s</span><span style="color:${dotColor};font-weight:800;font-size:.62rem;padding:1px 5px;border-radius:3px;background:${dotColor === 'var(--green)' ? 'rgba(0,255,157,.08)' : dotColor === 'var(--cyan)' ? 'rgba(0,212,255,.08)' : dotColor === 'var(--yellow)' ? 'rgba(255,215,0,.08)' : 'rgba(255,59,92,.08)'}">${roleLabel}</span></span></div>`);
        });
      }
      // Supervisor last-known
      if (sup && sup.running !== undefined) {
        const anomalies = sup.anomaly_count || 0;
        const supColor = sup.running ? (anomalies > 0 ? 'var(--yellow)' : 'var(--green)') : 'var(--red)';
        const supText = sup.running ? `✓ ${anomalies} alerts` : '✗ OFF';
        rows.push(`<div class="flex justify-between"><span class="text-muted"><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${supColor};margin-right:6px;box-shadow:0 0 5px ${supColor}"></span>supervisor</span><span style="color:${supColor};font-weight:700">${supText}</span></div>`);
      }
      cbEl.innerHTML = rows.join('');
    }
  } catch(e) {
    // silent — keep last known state
  }
}
// Poll independently of tab switches (every 3s while Live is active)
try {
  renderPipelineObs();
  setInterval(() => {
    if (document.getElementById('tab-live') && document.getElementById('tab-live').classList.contains('active')) {
      renderPipelineObs();
    }
  }, 3000);
} catch(e) {}

// ══════════════════════════════════════════════════════════
// Phase 3.3: Loss Taxonomy window selector + renderer
// ══════════════════════════════════════════════════════════
window._ltWindowHours = 24;
function setLtWindow(hours) {
  window._ltWindowHours = hours;
  document.querySelectorAll('.lt-win-btn').forEach(b => {
    const h = parseFloat(b.dataset.h);
    if (h === hours) {
      b.classList.add('lt-win-active');
      b.style.background = 'rgba(255,59,92,.08)';
      b.style.color = 'var(--red)';
      b.style.borderColor = 'rgba(255,59,92,.3)';
      b.style.fontWeight = '700';
    } else {
      b.classList.remove('lt-win-active');
      b.style.background = 'transparent';
      b.style.color = 'var(--text-muted)';
      b.style.borderColor = 'var(--border)';
      b.style.fontWeight = '600';
    }
  });
  const label = document.getElementById('lt-window-label');
  if (label) {
    label.textContent = hours < 24 ? `· last ${hours}h` : hours === 24 ? '· last 24h' : `· last ${hours/24}d`;
  }
  if (typeof renderPipelineObs === 'function') renderPipelineObs();
}

function renderLossTaxonomy(data) {
  const rowsEl = document.getElementById('lt-rows');
  const summaryEl = document.getElementById('lt-summary');
  if (!rowsEl) return;
  if (!data || !data.ok) {
    rowsEl.innerHTML = '<div style="font-size:.72rem;color:var(--text-muted);text-align:center;padding:14px">No taxonomy data yet</div>';
    if (summaryEl) summaryEl.textContent = '0 losses';
    return;
  }
  const totalCount = data.total_losses_analyzed || 0;
  const totalLossUsd = data.total_loss_usd || 0;
  const totalLossPct = data.total_loss_pct || 0;
  if (summaryEl) {
    summaryEl.textContent = totalCount === 0 ? '✓ no losses' : `${totalCount} losses · $${totalLossUsd.toFixed(2)}`;
    summaryEl.style.color = totalCount === 0 ? 'var(--green)' : 'var(--red)';
  }

  if (totalCount === 0) {
    rowsEl.innerHTML = '<div style="font-size:.72rem;color:var(--green);text-align:center;padding:14px">✓ No losses in window — bot is profitable</div>';
    return;
  }

  const buckets = data.buckets || {};
  // Build sorted list by total_loss_usd (most damaging first)
  const labels = {
    counter_htf_long:  {label: 'Counter-HTF Long',  color: '#ef4444', icon: '🔻'},
    counter_htf_short: {label: 'Counter-HTF Short', color: '#f87171', icon: '🔺'},
    early_kill:        {label: 'Early Kill <5m',    color: '#f59e0b', icon: '⚡'},
    time_decay:        {label: 'Time Decay',         color: '#fb923c', icon: '⏱'},
    fee_drag_be:       {label: 'Fee-Drag BE',       color: '#06b6d4', icon: '💸'},
    chop_regime:       {label: 'Chop Regime',       color: '#8b5cf6', icon: '🌪'},
    ml_weak:           {label: 'ML WEAK',            color: '#a78bfa', icon: '🤖'},
    slippage:          {label: 'Slippage >30bps',   color: '#ec4899', icon: '📉'},
    other:             {label: 'Other / Unknown',   color: '#6b7280', icon: '?'},
  };

  // Find the max count for bar scaling
  const maxCount = Math.max(1, ...Object.values(buckets).map(b => b.count || 0));

  const sorted = Object.entries(buckets)
    .filter(([_, b]) => (b.count || 0) > 0)
    .sort((a, b) => Math.abs(b[1].total_loss_usd || 0) - Math.abs(a[1].total_loss_usd || 0));

  if (sorted.length === 0) {
    rowsEl.innerHTML = '<div style="font-size:.72rem;color:var(--text-muted);text-align:center;padding:14px">No losses in window</div>';
    return;
  }

  rowsEl.innerHTML = sorted.map(([key, b]) => {
    const meta = labels[key] || {label: key, color: 'var(--text-dim)', icon: '?'};
    const count = b.count || 0;
    const lossUsd = Math.abs(b.total_loss_usd || 0);
    const lossPct = Math.abs(b.total_loss_pct || 0);
    const widthPct = maxCount > 0 ? (count / maxCount) * 100 : 0;
    const samples = (b.sample_symbols || []).join(', ') || '—';
    return `
      <div style="display:flex;flex-direction:column;gap:3px;padding:6px 8px;border-radius:5px;background:rgba(255,255,255,.01);border:1px solid var(--border)">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:10px">
          <div style="display:flex;align-items:center;gap:8px;min-width:200px">
            <span style="font-size:.85rem">${meta.icon}</span>
            <span style="font-size:.72rem;font-weight:700;color:${meta.color}">${meta.label}</span>
          </div>
          <div style="display:flex;align-items:center;gap:12px;font-family:var(--font-mono);font-size:.7rem">
            <span class="text-muted">count <span style="color:${meta.color};font-weight:700">${count}</span></span>
            <span class="text-muted">loss <span class="text-danger font-bold">−$${lossUsd.toFixed(2)}</span></span>
            <span class="text-muted">${lossPct.toFixed(2)}%</span>
            <span style="color:var(--text-dim);font-size:.62rem;max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${samples}">${samples}</span>
          </div>
        </div>
        <div style="height:5px;background:rgba(255,255,255,.03);border-radius:3px;overflow:hidden">
          <div style="height:100%;width:${widthPct}%;background:linear-gradient(90deg,${meta.color}33,${meta.color});border-radius:3px;transition:width .3s"></div>
        </div>
      </div>
    `;
  }).join('');
}

// ══════════════════════════════════════════════════════════
// Circuit Breaker reset buttons — three modes
// ══════════════════════════════════════════════════════════
async function cbResetAction(mode) {
  const msgEl = document.getElementById('po-cb-msg');
  const confirmMsgs = {
    reset: 'Reset circuit breaker?\n\nThis clears: is_tripped, consecutive_losses, trip_reason.\nKeeps: daily_pnl, total_pnl, enabled state.\n\nContinue?',
    full: 'FULL reset?\n\nThis clears: is_tripped, consecutive_losses, daily_pnl, total_pnl.\nKeeps: enabled state.\n\nUse this to clear drawdown-kill pnl accumulator.\n\nContinue?',
    reenable: 'RE-ENABLE real trading (FULL SIZE)?\n\nThis clears ALL CB state AND sets enabled=true with full position sizes.\n\n⚠ WARNING: Real trading will resume on next qualifying signal at 100% margin.\n\nRECOMMENDED: Use "+ PROB" instead for probation-mode (50% size).\n\nContinue?',
    probation: 'RE-ENABLE WITH PROBATION (Phase 3.5)?\n\nThis re-enables real trading with reduced risk:\n  • 50% position size for first 3 trades\n  • Auto-exits to full size after 3 trades or 4 hours\n  • Safer recovery from drawdown-kill\n\n✓ Recommended over plain RE-ENABLE.\n\nContinue?',
  };
  if (!confirm(confirmMsgs[mode] || 'Reset?')) return;

  let url = '/api/real/cb-reset';
  if (mode === 'full') url += '?full=true';
  else if (mode === 'reenable') url += '?full=true&reenable=true';
  else if (mode === 'probation') url += '?full=true&reenable=true&probation=true';

  try {
    const r = await fetch(url, {method: 'POST'});
    const d = await r.json();
    if (msgEl) {
      msgEl.style.display = 'block';
      if (d.ok) {
        const wasEnabled = d.was ? d.was.enabled : null;
        const nowEnabled = d.now ? d.now.enabled : null;
        msgEl.style.background = 'rgba(0,255,157,.08)';
        msgEl.style.border = '1px solid rgba(0,255,157,.2)';
        msgEl.style.color = 'var(--green)';
        msgEl.textContent = `✓ ${mode.toUpperCase()} OK · was tripped=${d.was.is_tripped} losses=${d.was.consecutive_losses} enabled=${wasEnabled} · now losses=0 enabled=${nowEnabled}`;
        // Trigger immediate refresh
        setTimeout(() => renderPipelineObs(), 300);
      } else {
        msgEl.style.background = 'rgba(255,59,92,.08)';
        msgEl.style.border = '1px solid rgba(255,59,92,.2)';
        msgEl.style.color = 'var(--red)';
        msgEl.textContent = '✗ ERROR: ' + (d.error || 'unknown');
      }
      setTimeout(() => { msgEl.style.display = 'none'; }, 8000);
    }
  } catch(e) {
    if (msgEl) {
      msgEl.style.display = 'block';
      msgEl.style.background = 'rgba(255,59,92,.08)';
      msgEl.style.color = 'var(--red)';
      msgEl.textContent = '✗ Network error: ' + e.message;
    }
  }
}

// ══════════════════════════════════════════════════════════
// Phase 3.1: Stage Loss Map window selector
// ══════════════════════════════════════════════════════════
window._slWindowHours = 4;
function setSlWindow(hours) {
  window._slWindowHours = hours;
  // Update button states
  document.querySelectorAll('.sl-win-btn').forEach(b => {
    const h = parseFloat(b.dataset.h);
    if (h === hours) {
      b.classList.add('sl-win-active');
      b.style.background = 'rgba(255,215,0,.08)';
      b.style.color = 'var(--yellow)';
      b.style.borderColor = 'rgba(255,215,0,.3)';
      b.style.fontWeight = '700';
    } else {
      b.classList.remove('sl-win-active');
      b.style.background = 'transparent';
      b.style.color = 'var(--text-muted)';
      b.style.borderColor = 'var(--border)';
      b.style.fontWeight = '600';
    }
  });
  // Update label
  const label = document.getElementById('sl-window-label');
  if (label) {
    const txt = hours === 0 ? '· all time' : hours < 24 ? `· last ${hours}h` : hours < 168 ? `· last ${hours/24}d` : '· last 7d';
    label.textContent = txt;
  }
  // Immediate refresh
  if (typeof refreshAgents === 'function') refreshAgents();
}

// ══════════════════════════════════════════════════════════
// Phase 2A: Stage Loss Map renderer
// ══════════════════════════════════════════════════════════
function renderStageLossMap(data) {
  const container = document.getElementById('sl-funnel');
  const analyzed = document.getElementById('sl-analyzed');
  if (!container) return;
  if (!data || !data.ok || !Array.isArray(data.funnel)) {
    container.innerHTML = '<div style="font-size:.72rem;color:var(--text-muted);text-align:center;padding:20px">No journey data yet — waiting for closed trades</div>';
    if (analyzed) analyzed.textContent = '0 journeys';
    return;
  }
  // Show filtered vs total from backend
  if (analyzed) {
    const f = data.filter || {};
    const shown = data.journeys_analyzed || 0;
    const total = f.total_in_file || shown;
    if (shown < total) {
      analyzed.textContent = `${shown}/${total} journeys`;
    } else {
      analyzed.textContent = `${shown} journeys`;
    }
  }

  // Max "reached" count for bar scale
  const maxReached = Math.max(1, ...data.funnel.map(s => s.reached || 0));
  const stageIcons = {
    strategy: '🎯', hard_block: '🛑', risk_check: '⚖️',
    signal_tracker: '📡', paper_exec: '📝', real_qualify: '🔍',
    real_exec: '💰', exit: '🏁'
  };
  const stageColors = {
    strategy: '#6366f1', hard_block: '#ef4444', risk_check: '#f59e0b',
    signal_tracker: '#0ea5e9', paper_exec: '#10b981', real_qualify: '#f59e0b',
    real_exec: '#00ff9d', exit: '#00d4ff'
  };

  const rows = data.funnel.map((s, i) => {
    const reached = s.reached || 0;
    const passed = s.passed || 0;
    const failed = s.failed || 0;
    const dropPct = s.drop_pct || 0;
    const widthPct = maxReached > 0 ? (reached / maxReached) * 100 : 0;
    const col = stageColors[s.stage] || 'var(--text-dim)';
    const icon = stageIcons[s.stage] || '•';
    const dropBadge = i > 0 && s.drop_from_prev > 0
      ? `<span style="font-size:.6rem;color:var(--red);font-weight:700;margin-left:8px">−${s.drop_from_prev} (${dropPct}% drop)</span>`
      : '';
    const reasonChips = (s.top_reasons || []).slice(0, 3).map(r =>
      `<span style="font-size:.58rem;padding:1px 6px;border-radius:3px;background:rgba(239,68,68,.08);color:#f87171;border:1px solid rgba(239,68,68,.15);font-family:var(--font-mono)" title="${r.reason}">${r.reason.length > 20 ? r.reason.substring(0,20)+'…' : r.reason} ×${r.count}</span>`
    ).join(' ');
    return `
      <div style="display:flex;flex-direction:column;gap:3px;padding:6px 8px;border-radius:5px;background:rgba(255,255,255,.01);border:1px solid var(--border)">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:10px">
          <div style="display:flex;align-items:center;gap:8px;min-width:200px">
            <span style="font-size:.85rem">${icon}</span>
            <span style="font-size:.72rem;font-weight:700;color:${col}">${s.stage}</span>
            ${dropBadge}
          </div>
          <div style="display:flex;align-items:center;gap:10px;font-family:var(--font-mono);font-size:.68rem">
            <span class="text-muted">reached <span style="color:var(--text-dim);font-weight:700">${reached}</span></span>
            <span class="text-success">✓ ${passed}</span>
            <span class="text-danger">✗ ${failed}</span>
            <span class="text-muted">${s.avg_latency_ms}ms</span>
          </div>
        </div>
        <div style="height:6px;background:rgba(255,255,255,.03);border-radius:3px;overflow:hidden">
          <div style="height:100%;width:${widthPct}%;background:linear-gradient(90deg,${col}33,${col});border-radius:3px;transition:width .3s"></div>
        </div>
        ${reasonChips ? `<div style="display:flex;gap:4px;flex-wrap:wrap;margin-top:2px">${reasonChips}</div>` : ''}
      </div>
    `;
  }).join('');
  container.innerHTML = rows;
}

// ══════════════════════════════════════════════════════════
// Phase 2D: R-Drift / WR alert strip (global, all tabs)
// ══════════════════════════════════════════════════════════
// Phase 3.14: R-Drift rolling window selector
window._rdWindowHours = 24;  // default to 24h (sane default)
function setRdWindow(hours) {
  window._rdWindowHours = hours;
  document.querySelectorAll('.rd-win-btn').forEach(b => {
    const h = parseFloat(b.dataset.h);
    if (h === hours) {
      b.classList.add('rd-win-active');
      b.style.background = 'rgba(0,255,157,.08)';
      b.style.color = 'var(--green)';
      b.style.borderColor = 'rgba(0,255,157,.3)';
      b.style.fontWeight = '700';
    } else {
      b.classList.remove('rd-win-active');
      b.style.background = 'transparent';
      b.style.color = 'var(--text-muted)';
      b.style.borderColor = 'var(--border)';
      b.style.fontWeight = '600';
    }
  });
  if (typeof loadRDrift === 'function') loadRDrift();
}

async function loadRDrift() {
  try {
    const rdHours = window._rdWindowHours != null ? window._rdWindowHours : 24;
    const rdUrl = rdHours > 0
      ? `/api/pipeline/rdrift?limit=50&hours=${rdHours}`
      : `/api/pipeline/rdrift?limit=20`;
    const [d, sup] = await Promise.all([
      fetch(rdUrl).then(r => r.json()).catch(() => ({})),
      fetch('/api/supervisor/status').then(r => r.json()).catch(() => ({})),
    ]);

    // ── Phase 2.5 B2: Current Action Banner ──
    try {
      const ca = sup && sup.current_action;
      if (ca) {
        const actionEl = document.getElementById('ca-action');
        const symEl = document.getElementById('ca-symbol');
        const detEl = document.getElementById('ca-detail');
        const ageEl = document.getElementById('ca-age');
        const pulseEl = document.getElementById('ca-pulse');
        const act = (ca.action || 'idle').toUpperCase().replace(/_/g, ' ');
        if (actionEl) actionEl.textContent = act;
        if (symEl) symEl.textContent = ca.symbol ? `${ca.symbol}${ca.timeframe ? ' · ' + ca.timeframe : ''}` : '';
        if (detEl) detEl.textContent = ca.detail || '';
        const age = ca.age_sec || 0;
        const ageText = age < 60 ? `${age.toFixed(0)}s ago` : `${(age/60).toFixed(1)}m ago`;
        if (ageEl) {
          ageEl.textContent = ageText;
          ageEl.style.color = age > 60 ? 'var(--red)' : age > 10 ? 'var(--yellow)' : 'var(--text-muted)';
        }
        // Pulse dim if stale
        if (pulseEl) {
          if (age > 60) {
            pulseEl.style.background = 'var(--red)';
            pulseEl.style.boxShadow = '0 0 6px var(--red)';
            pulseEl.style.animation = 'none';
          } else if (age > 10) {
            pulseEl.style.background = 'var(--yellow)';
            pulseEl.style.boxShadow = '0 0 6px var(--yellow)';
            pulseEl.style.animation = 'pulse 2.5s ease-in-out infinite';
          } else {
            pulseEl.style.background = '#818cf8';
            pulseEl.style.boxShadow = '0 0 6px #818cf8';
            pulseEl.style.animation = 'pulse 1.5s ease-in-out infinite';
          }
        }
      } else {
        const actionEl = document.getElementById('ca-action');
        if (actionEl) actionEl.textContent = 'IDLE';
      }
    } catch(_){}

    if (!d || !d.ok) return;
    const paper = d.paper || {};
    const real = d.real || {};
    const drift = d.drift || {};
    const alerts = d.alerts || [];

    const elWR = id => document.getElementById(id);
    const setText = (id, val) => { const e = elWR(id); if (e) e.textContent = val; };
    const setColor = (id, col) => { const e = elWR(id); if (e) e.style.color = col; };

    setText('rd-paper-wr', paper.count ? paper.wr + '% (' + paper.count + ')' : '--');
    setText('rd-real-wr', real.count ? real.wr + '% (' + real.count + ')' : '--');
    setText('rd-paper-r', paper.count ? (paper.avg_r >= 0 ? '+' : '') + paper.avg_r.toFixed(2) + 'R' : '--');
    setText('rd-real-r', real.count ? (real.avg_r >= 0 ? '+' : '') + real.avg_r.toFixed(2) + 'R' : '--');

    // WR drift color
    const wrD = drift.wr_drift_pct || 0;
    const wrDText = (wrD >= 0 ? '+' : '') + wrD.toFixed(1) + '%';
    setText('rd-wr-drift', wrDText);
    setColor('rd-wr-drift', Math.abs(wrD) > 10 ? 'var(--red)' : Math.abs(wrD) > 5 ? 'var(--yellow)' : 'var(--green)');

    // R drift color
    const rD = drift.r_drift || 0;
    const rDText = (rD >= 0 ? '+' : '') + rD.toFixed(2) + 'R';
    setText('rd-r-drift', rDText);
    setColor('rd-r-drift', Math.abs(rD) > 0.5 ? 'var(--red)' : Math.abs(rD) > 0.25 ? 'var(--yellow)' : 'var(--green)');

    // Supervisor indicator
    const supEl = elWR('rd-sup-indicator');
    if (supEl) {
      if (sup && sup.running) {
        const anomalies = sup.anomaly_count || 0;
        supEl.textContent = anomalies > 0 ? '⚠ ' + anomalies : '✓ OK';
        supEl.style.color = anomalies > 0 ? 'var(--red)' : 'var(--green)';
      } else {
        supEl.textContent = '✗ OFF';
        supEl.style.color = 'var(--red)';
      }
    }

    // Inline alerts strip
    const aEl = elWR('rd-alerts');
    if (aEl) {
      if (alerts.length) {
        aEl.textContent = '⚠ ' + alerts.map(a => a.message).join(' · ');
        aEl.style.color = 'var(--red)';
      } else {
        aEl.textContent = '';
      }
    }

    // Strip border color reflects worst alert
    const strip = document.getElementById('rdrift-strip');
    if (strip) {
      if (alerts.length) {
        strip.style.border = '1px solid rgba(255,59,92,.35)';
        strip.style.background = 'rgba(255,59,92,.04)';
      } else {
        strip.style.border = '1px solid rgba(0,255,157,.15)';
        strip.style.background = 'rgba(0,255,157,.03)';
      }
    }
  } catch(e) {
    // Silent — strip stays in last known state
  }
}
// Fire on load + every 10s, independent of tab switches
try {
  loadRDrift();
  setInterval(loadRDrift, 10000);
} catch(e) {}

// ═══ Track D: REAL OPS STRIP (Fix #1-5 governance) ═════════
async function loadRealOps() {
  try {
    const r = await fetch('/api/real/status', {cache: 'no-store'});
    if (!r.ok) return;
    const d = await r.json();
    if (!d) return;

    const set = (id, text, color) => {
      const e = document.getElementById(id);
      if (!e) return;
      if (text != null) e.textContent = text;
      if (color) e.style.color = color;
    };
    const bg = (id, color) => {
      const e = document.getElementById(id);
      if (e && color) e.style.background = color;
    };

    // 1. Status badge
    const mode = d.mode || 'UNKNOWN';
    const modeColor = mode === 'LIVE' ? 'var(--red)'
                    : mode === 'DRY RUN' ? 'var(--yellow)'
                    : 'var(--text-muted)';
    const modeBg = mode === 'LIVE' ? 'rgba(255,59,92,.18)'
                 : mode === 'DRY RUN' ? 'rgba(255,215,0,.15)'
                 : 'rgba(128,128,128,.15)';
    set('rops-status', mode, modeColor);
    bg('rops-status', modeBg);

    // 2. Balance
    const bal = d.balance || 0;
    set('rops-balance', '$' + bal.toFixed(2));

    // 3. Rolling drawdown (1h / 24h / 7d)
    const rd = d.rolling_drawdown || {};
    const fmtCb = (w) => {
      const r = rd[w];
      if (!r) return '--';
      const pnl = r.pnl || 0;
      const pct = r.pct || 0;
      const col = pct > 80 ? 'var(--red)' : pct > 50 ? 'var(--yellow)' : (pnl >= 0 ? 'var(--green)' : 'var(--text-muted)');
      return { text: (pnl >= 0 ? '+' : '') + pnl.toFixed(2) + ' (' + pct.toFixed(0) + '%)', color: col };
    };
    const cb1h = fmtCb('1h');
    const cb24h = fmtCb('24h');
    const cb7d = fmtCb('7d');
    if (typeof cb1h === 'object') set('rops-cb-1h', cb1h.text, cb1h.color);
    if (typeof cb24h === 'object') set('rops-cb-24h', cb24h.text, cb24h.color);
    if (typeof cb7d === 'object') set('rops-cb-7d', cb7d.text, cb7d.color);

    // 4. Fix counters
    const fs = d.fix_stats || {};
    const f1Fill = fs.fix1_ioc_fill || 0;
    const f1Skip = fs.fix1_ioc_skip || 0;
    const f2 = fs.fix2_trail_prop || 0;
    const f3 = fs.fix3_ml_floor_block || 0;
    const f4 = fs.fix4_regime_block || 0;
    const f5 = fs.fix5_tp_override || 0;

    set('rops-fix1', f1Fill + '/' + f1Skip, (f1Fill + f1Skip) > 0 ? 'var(--green)' : 'var(--text-muted)');
    set('rops-fix2', String(f2), f2 > 0 ? 'var(--green)' : 'var(--text-muted)');
    set('rops-fix3', String(f3), f3 > 0 ? 'var(--cyan)' : 'var(--text-muted)');
    set('rops-fix4', String(f4), f4 > 0 ? 'var(--cyan)' : 'var(--text-muted)');
    set('rops-fix5', String(f5), f5 > 0 ? 'var(--green)' : 'var(--text-muted)');

    // 5. Probation mode
    const p = d.probation || {};
    if (p.active) {
      const tradesLeft = p.remaining_trades || 0;
      const secsLeft = Math.max(0, p.remaining_sec || 0);
      const minsLeft = (secsLeft / 60).toFixed(0);
      const mult = (p.size_mult * 100).toFixed(0);
      set('rops-probation', `${mult}% × ${tradesLeft}t / ${minsLeft}m`, 'var(--yellow)');
    } else {
      set('rops-probation', 'off', 'var(--text-muted)');
    }

    // 6. CB tripped border warning
    const cb = d.circuit_breaker || {};
    const strip = document.getElementById('real-ops-strip');
    if (strip) {
      if (cb.is_tripped) {
        strip.style.border = '2px solid var(--red)';
        strip.style.background = 'rgba(255,59,92,.08)';
      } else if (!d.enabled) {
        strip.style.border = '1px solid rgba(128,128,128,.3)';
        strip.style.background = 'rgba(128,128,128,.03)';
      } else {
        strip.style.border = '1px solid rgba(255,59,92,.15)';
        strip.style.background = 'rgba(255,59,92,.03)';
      }
    }
  } catch(e) {
    // Silent — keeps last state on transient failures
  }
}

async function toggleRealTrading() {
  // Read current state to flip it
  let currentEnabled = true;
  try {
    const s = await fetch('/api/real/status').then(r => r.json());
    currentEnabled = !!s.enabled;
  } catch(e) {}
  const newEnabled = !currentEnabled;
  if (!confirm(`Flip real trading: ${currentEnabled ? 'ENABLED' : 'DISABLED'} → ${newEnabled ? 'ENABLED' : 'DISABLED'}?`)) return;
  try {
    const r = await fetch('/api/real/toggle', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled: newEnabled}),
    });
    const d = await r.json().catch(() => ({}));
    if (r.ok) {
      alert('Real trading now: ' + (d.enabled ? 'ENABLED' : 'DISABLED'));
      loadRealOps();
    } else {
      alert('Toggle failed: ' + JSON.stringify(d));
    }
  } catch(e) {
    alert('Toggle error: ' + e.message);
  }
}

async function killAllTrading() {
  if (!confirm('⚠ EMERGENCY STOP — this halts ALL trading activity. Continue?')) return;
  if (!confirm('Are you ABSOLUTELY sure? Bot will need manual restart.')) return;
  try {
    const r = await fetch('/api/emergency-stop', {method: 'POST'});
    const d = await r.json().catch(() => ({}));
    alert('Emergency stop: ' + JSON.stringify(d));
    loadRealOps();
  } catch(e) {
    alert('Kill error: ' + e.message);
  }
}

// Fire on load + every 5s
try {
  loadRealOps();
  setInterval(loadRealOps, 5000);
} catch(e) {}

// ═══ Track C: ML OPS strip + Verdict Matrix + Live Calibration ═══
async function loadMlOps() {
  try {
    const [matrix, calib] = await Promise.all([
      fetch('/api/ml/family-verdict-matrix', {cache: 'no-store'}).then(r => r.ok ? r.json() : null).catch(() => null),
      fetch('/api/ml/live-calibration?last_n=500&min_n=15', {cache: 'no-store'}).then(r => r.ok ? r.json() : null).catch(() => null),
    ]);

    const set = (id, text, color) => {
      const e = document.getElementById(id);
      if (!e) return;
      if (text != null) e.textContent = text;
      if (color) e.style.color = color;
    };

    // ── ML OPS strip ──
    if (matrix && matrix.global_tally) {
      const t = matrix.global_tally;
      const holds = t.HOLDS || 0;
      const weak = t.WEAK || 0;
      const unclear = t.UNCLEAR || 0;
      const noedge = t.NO_EDGE || 0;
      const total = holds + weak + unclear + noedge;
      set('mlops-holds', String(holds));
      set('mlops-weak', String(weak));
      set('mlops-unclear', String(unclear));
      set('mlops-noedge', String(noedge));

      // Health classification: HOLDS ≥ 5 = OK, 3-4 = DEGRADED, <3 = CRITICAL
      let health = '--';
      let healthColor = 'var(--text-muted)';
      if (total > 0) {
        if (holds >= 5) { health = 'OK'; healthColor = 'var(--green)'; }
        else if (holds >= 3) { health = 'DEGRADED'; healthColor = 'var(--yellow)'; }
        else { health = 'CRITICAL'; healthColor = 'var(--red)'; }
      }
      set('mlops-health', health, healthColor);
    }

    if (calib && calib.overall) {
      const o = calib.overall;
      const n = o.n || 0;
      const pred = o.avg_ml_probability || 0;
      const wr = o.realized_mfe_wr || 0;
      const err = o.calibration_error || 0;
      set('mlops-live-n', String(n));
      set('mlops-live-pred', pred.toFixed(3));
      set('mlops-live-wr', (wr * 100).toFixed(1) + '%');
      // Cal err color: green < 0.08, yellow < 0.15, red > 0.15
      const errColor = err < 0.08 ? 'var(--green)' : err < 0.15 ? 'var(--yellow)' : 'var(--red)';
      set('mlops-live-err', err.toFixed(3), errColor);
    }

    // ── ML VERDICT MATRIX table ──
    if (matrix && matrix.grid) {
      const tbody = document.getElementById('ml-verdict-matrix-body');
      if (tbody) {
        const scanners = matrix.scanners || [];
        const families = ['liquid_majors', 'secondary', 'high_beta', '__scanner__'];
        const rows = [];
        for (const sc of scanners) {
          let rowHtml = `<tr><td style="text-align:left;padding:4px 8px;color:var(--text);font-weight:700">${sc}</td>`;
          for (const fam of families) {
            const cell = (matrix.grid[sc] || {})[fam];
            if (!cell) {
              rowHtml += `<td style="text-align:center;padding:4px 8px;color:var(--text-muted)">—</td>`;
              continue;
            }
            const v = cell.edge_verdict || '?';
            const oos = cell.oos_mean;
            const gap = cell.overfit_gap;
            let bg = 'rgba(128,128,128,.08)';
            let fg = 'var(--text-muted)';
            let border = '1px solid var(--border)';
            if (v === 'HOLDS') { bg = 'rgba(0,255,157,.18)'; fg = 'var(--green)'; border = '1px solid var(--green)'; }
            else if (v === 'WEAK') { bg = 'rgba(255,215,0,.14)'; fg = 'var(--yellow)'; border = '1px solid rgba(255,215,0,.4)'; }
            else if (v === 'UNCLEAR') { bg = 'rgba(249,115,22,.14)'; fg = 'var(--orange)'; border = '1px solid rgba(249,115,22,.4)'; }
            else if (v === 'NO_EDGE') { bg = 'rgba(255,59,92,.14)'; fg = 'var(--red)'; border = '1px solid var(--red)'; }
            const tooltip = `${v} · OOS=${oos != null ? oos.toFixed(3) : '--'} · gap=${gap != null ? gap.toFixed(3) : '--'} · n_feat=${cell.n_features || '--'}`;
            const oosDisp = oos != null ? oos.toFixed(2) : '--';
            rowHtml += `<td style="text-align:center;padding:2px 6px" title="${tooltip}"><span style="display:inline-block;padding:2px 8px;background:${bg};color:${fg};border:${border};border-radius:4px;font-weight:700;font-size:.65rem">${v}<span style="font-weight:400;margin-left:4px;opacity:.7">${oosDisp}</span></span></td>`;
          }
          rowHtml += '</tr>';
          rows.push(rowHtml);
        }
        tbody.innerHTML = rows.length ? rows.join('') : '<tr><td colspan="5" class="empty">No models</td></tr>';
        const ts = new Date().toLocaleTimeString();
        set('mlvm-last-update', 'updated ' + ts);
      }
    }

    // ── ML LIVE CALIBRATION table (by_verdict) ──
    if (calib && calib.by_verdict) {
      const tbody = document.getElementById('ml-calibration-body');
      if (tbody) {
        const rows = [];
        for (const v of calib.by_verdict) {
          const n = v.n || 0;
          const pred = v.avg_ml_probability || 0;
          const wr = v.realized_mfe_wr || 0;
          const err = v.calibration_error || 0;
          const xr = v.avg_exit_r || 0;
          let status = 'HEALTHY', statusColor = 'var(--green)';
          if (err > 0.15) { status = 'DRIFTING'; statusColor = 'var(--red)'; }
          else if (err > 0.08) { status = 'SOFT DRIFT'; statusColor = 'var(--yellow)'; }
          rows.push(
            `<tr>` +
            `<td style="text-align:left;padding:4px 8px;font-weight:700">${v.edge_verdict || '?'}</td>` +
            `<td class="text-right px-2 py-1">${n}</td>` +
            `<td class="text-right px-2 py-1">${pred.toFixed(3)}</td>` +
            `<td class="text-right px-2 py-1">${(wr * 100).toFixed(1)}%</td>` +
            `<td style="text-align:right;padding:4px 8px;color:${statusColor}">${err.toFixed(3)}</td>` +
            `<td class="text-right px-2 py-1">${xr >= 0 ? '+' : ''}${xr.toFixed(3)}R</td>` +
            `<td style="text-align:center;padding:4px 8px;color:${statusColor};font-weight:700">${status}</td>` +
            `</tr>`
          );
        }
        tbody.innerHTML = rows.length ? rows.join('') : '<tr><td colspan="7" class="empty">No calibration data</td></tr>';
      }
      // Overall summary line
      const o = calib.overall || {};
      const overallEl = document.getElementById('mlcal-overall');
      if (overallEl) {
        overallEl.textContent = `n=${o.n || 0} pred=${(o.avg_ml_probability || 0).toFixed(3)} wr=${((o.realized_mfe_wr || 0) * 100).toFixed(1)}% err=${(o.calibration_error || 0).toFixed(3)}`;
      }
    }

    // ── Alerts strip (any DRIFTING bucket or critical health) ──
    const alertsEl = document.getElementById('mlops-alerts');
    if (alertsEl) {
      const alerts = [];
      if (calib && calib.by_verdict) {
        for (const v of calib.by_verdict) {
          if ((v.calibration_error || 0) > 0.15 && (v.n || 0) >= 20) {
            alerts.push(`${v.edge_verdict} drift ${v.calibration_error.toFixed(2)}`);
          }
        }
      }
      alertsEl.textContent = alerts.length ? '⚠ ' + alerts.join(' · ') : '';
    }
  } catch(e) {
    // Silent — retain last state on errors
  }
}

// Fire on load + every 30s (ML data moves slowly)
try {
  loadMlOps();
  setInterval(loadMlOps, 30000);
} catch(e) {}

// ═══ Track B: Analytics charts (equity curve, R-hist, divergence, loss) ═══
window._eqRange = 300;  // default
window._eqChart = null;
window._rhChart = null;
window._dvChart = null;

function setEqRange(n) {
  window._eqRange = n;
  document.querySelectorAll('.eq-btn').forEach(b => {
    const v = parseInt(b.getAttribute('data-eq') || '0', 10);
    if (v === n) {
      b.classList.add('eq-active');
      b.style.background = 'rgba(0,212,255,.08)';
      b.style.color = 'var(--cyan)';
      b.style.borderColor = 'rgba(0,212,255,.3)';
      b.style.fontWeight = '700';
    } else {
      b.classList.remove('eq-active');
      b.style.background = 'transparent';
      b.style.color = 'var(--text-muted)';
      b.style.borderColor = 'var(--border)';
      b.style.fontWeight = '400';
    }
  });
  if (typeof loadAnalytics === 'function') loadAnalytics();
}

async function loadAnalytics() {
  try {
    const [paperResp, realResp, lossResp] = await Promise.all([
      fetch('/api/tracker/closed?limit=1000', {cache: 'no-store'}).then(r => r.ok ? r.json() : []).catch(() => []),
      fetch('/api/real/status', {cache: 'no-store'}).then(r => r.ok ? r.json() : {}).catch(() => ({})),
      fetch('/api/pipeline/loss_taxonomy?hours=24', {cache: 'no-store'}).then(r => r.ok ? r.json() : {}).catch(() => ({})),
    ]);

    // Normalize paper trade list
    let paperSigs = Array.isArray(paperResp) ? paperResp : (paperResp.closed || paperResp.signals || []);
    paperSigs = paperSigs.filter(s => s && typeof s === 'object' && s.exit_time);
    // Sort ascending by exit_time
    paperSigs.sort((a, b) => String(a.exit_time).localeCompare(String(b.exit_time)));

    // Normalize real trade list from /api/real/status
    const realTrades = (realResp && (realResp.live_trades || realResp.recent_trades)) || [];
    const realSigs = realTrades.filter(t => t && typeof t === 'object' && t.timestamp);
    realSigs.sort((a, b) => String(a.timestamp).localeCompare(String(b.timestamp)));

    // ── B.1: EQUITY CURVE ──
    const eqRange = window._eqRange || 300;
    const paperSlice = eqRange > 0 ? paperSigs.slice(-eqRange) : paperSigs;
    const realSlice = eqRange > 0 ? realSigs.slice(-eqRange) : realSigs;

    // Compute cumulative PnL arrays (indexed by trade #)
    let paperCum = 0, realCum = 0;
    const paperCumArr = [], realCumArr = [];
    const paperLabels = [], realLabels = [];
    let paperWins = 0, paperLosses = 0, paperPeakDD = 0, paperPeak = 0;
    let realWins = 0, realLosses = 0, realPeakDD = 0, realPeak = 0;

    paperSlice.forEach((s, i) => {
      const pnl = parseFloat(s.pnl_usd || 0) || 0;
      paperCum += pnl;
      paperCumArr.push(paperCum);
      paperLabels.push(String(s.exit_time || '').slice(5, 16));
      if (pnl > 0.05) paperWins++;
      else if (pnl < -0.05) paperLosses++;
      if (paperCum > paperPeak) paperPeak = paperCum;
      const dd = paperPeak - paperCum;
      if (dd > paperPeakDD) paperPeakDD = dd;
    });

    realSlice.forEach((s, i) => {
      const pnl = parseFloat(s.pnl_usd || 0) || 0;
      realCum += pnl;
      realCumArr.push(realCum);
      realLabels.push(String(s.timestamp || '').slice(5, 16));
      if (pnl > 0.05) realWins++;
      else if (pnl < -0.05) realLosses++;
      if (realCum > realPeak) realPeak = realCum;
      const dd = realPeak - realCum;
      if (dd > realPeakDD) realPeakDD = dd;
    });

    // Overlay both on same x-axis — use trade index as x (paper+real rarely align on timestamps)
    const maxLen = Math.max(paperCumArr.length, realCumArr.length);
    const xLabels = Array.from({length: maxLen}, (_, i) => '#' + (i + 1));

    // Pad shorter series with null so Chart.js handles them cleanly
    while (paperCumArr.length < maxLen) paperCumArr.push(null);
    while (realCumArr.length < maxLen) realCumArr.push(null);

    const eqCanvas = document.getElementById('equity-curve-canvas');
    if (eqCanvas && typeof Chart !== 'undefined') {
      if (window._eqChart) { window._eqChart.destroy(); }
      window._eqChart = new Chart(eqCanvas, {
        type: 'line',
        data: {
          labels: xLabels,
          datasets: [
            {
              label: 'Paper',
              data: paperCumArr,
              borderColor: 'rgba(0,212,255,.9)',
              backgroundColor: 'rgba(0,212,255,.08)',
              borderWidth: 2,
              tension: 0.2,
              pointRadius: 0,
              fill: true,
              spanGaps: true,
            },
            {
              label: 'Real',
              data: realCumArr,
              borderColor: 'rgba(255,59,92,.95)',
              backgroundColor: 'rgba(255,59,92,.06)',
              borderWidth: 2,
              tension: 0.2,
              pointRadius: 0,
              fill: true,
              spanGaps: true,
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: false },
            tooltip: {
              mode: 'index',
              intersect: false,
              callbacks: {
                label: (ctx) => ctx.dataset.label + ': $' + (ctx.parsed.y != null ? ctx.parsed.y.toFixed(2) : '--'),
              },
            },
          },
          scales: {
            x: { ticks: { color: 'rgba(255,255,255,.3)', font: { size: 9 } }, grid: { color: 'rgba(255,255,255,.04)' } },
            y: { ticks: { color: 'rgba(255,255,255,.4)', font: { size: 10 }, callback: (v) => '$' + v.toFixed(0) }, grid: { color: 'rgba(255,255,255,.04)' } },
          },
        },
      });
    }

    // Summary strips
    const setT = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
    const pWr = (paperWins + paperLosses) > 0 ? (paperWins / (paperWins + paperLosses) * 100).toFixed(1) + '%' : '--';
    const rWr = (realWins + realLosses) > 0 ? (realWins / (realWins + realLosses) * 100).toFixed(1) + '%' : '--';
    setT('eq-paper-n', paperSlice.length);
    setT('eq-paper-wr', pWr);
    setT('eq-paper-net', (paperCum >= 0 ? '+$' : '-$') + Math.abs(paperCum).toFixed(2));
    setT('eq-paper-dd', '-$' + paperPeakDD.toFixed(2));
    setT('eq-real-n', realSlice.length);
    setT('eq-real-wr', rWr);
    setT('eq-real-net', (realCum >= 0 ? '+$' : '-$') + Math.abs(realCum).toFixed(2));
    setT('eq-real-dd', '-$' + realPeakDD.toFixed(2));

    // Color net numbers green/red
    const setColor = (id, cond) => { const e = document.getElementById(id); if (e) e.style.color = cond ? 'var(--green)' : 'var(--red)'; };
    setColor('eq-paper-net', paperCum >= 0);
    setColor('eq-real-net', realCum >= 0);

    // ── B.3: R-MULTIPLE HISTOGRAM ──
    // Buckets: <-2, -2..-1.5, -1.5..-1, -1..-0.5, -0.5..0, 0..0.5, 0.5..1, 1..1.5, 1.5..2, 2+
    const buckets = [-2, -1.5, -1, -0.5, 0, 0.5, 1, 1.5, 2];
    const bucketLabels = ['<-2R', '-2 to -1.5', '-1.5 to -1', '-1 to -0.5', '-0.5 to 0', '0 to 0.5', '0.5 to 1', '1 to 1.5', '1.5 to 2', '>2R'];
    const paperHist = new Array(10).fill(0);
    const realHist = new Array(10).fill(0);
    let paperSum = 0, realSum = 0, paperRs = [], realRs = [];

    paperSlice.forEach(s => {
      const r = parseFloat(s.exit_r || 0) || 0;
      paperRs.push(r);
      paperSum += r;
      let idx = 0;
      for (let i = 0; i < buckets.length; i++) {
        if (r < buckets[i]) { idx = i; break; }
        idx = i + 1;
      }
      if (idx >= 0 && idx < 10) paperHist[idx]++;
    });

    realSlice.forEach(s => {
      // Real trades: compute exit_r from pnl_pct / initial_risk if not present
      let r = parseFloat(s.exit_r || 0);
      if (!r || isNaN(r)) {
        const pnlPct = parseFloat(s.pnl_pct || 0) || 0;
        const slDist = parseFloat(s.initial_risk || 0) || 0;
        const entry = parseFloat(s.entry_price || 0) || 0;
        if (entry > 0 && slDist > 0) {
          r = (pnlPct / 100) * entry / slDist;
        }
      }
      realRs.push(r);
      realSum += r;
      let idx = 0;
      for (let i = 0; i < buckets.length; i++) {
        if (r < buckets[i]) { idx = i; break; }
        idx = i + 1;
      }
      if (idx >= 0 && idx < 10) realHist[idx]++;
    });

    const median = arr => { if (!arr.length) return 0; const s = [...arr].sort((a,b) => a-b); const m = Math.floor(s.length / 2); return s.length % 2 ? s[m] : (s[m-1] + s[m]) / 2; };
    setT('rh-paper-avg', paperSlice.length ? (paperSum / paperSlice.length).toFixed(2) + 'R' : '--');
    setT('rh-paper-med', paperSlice.length ? median(paperRs).toFixed(2) + 'R' : '--');
    setT('rh-real-avg', realSlice.length ? (realSum / realSlice.length).toFixed(2) + 'R' : '--');
    setT('rh-real-med', realSlice.length ? median(realRs).toFixed(2) + 'R' : '--');

    const rhCanvas = document.getElementById('r-hist-canvas');
    if (rhCanvas && typeof Chart !== 'undefined') {
      if (window._rhChart) { window._rhChart.destroy(); }
      window._rhChart = new Chart(rhCanvas, {
        type: 'bar',
        data: {
          labels: bucketLabels,
          datasets: [
            {
              label: 'Paper',
              data: paperHist,
              backgroundColor: 'rgba(0,212,255,.6)',
              borderColor: 'rgba(0,212,255,1)',
              borderWidth: 1,
            },
            {
              label: 'Real',
              data: realHist,
              backgroundColor: 'rgba(255,59,92,.6)',
              borderColor: 'rgba(255,59,92,1)',
              borderWidth: 1,
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { ticks: { color: 'rgba(255,255,255,.4)', font: { size: 9 } }, grid: { display: false } },
            y: { ticks: { color: 'rgba(255,255,255,.4)', font: { size: 9 } }, grid: { color: 'rgba(255,255,255,.04)' }, beginAtZero: true },
          },
        },
      });
    }

    // ── D.3: PAPER↔REAL DIVERGENCE CHART ──
    // Group by symbol: for each, compute avg paper R - avg real R
    const perSymbol = {};
    paperSlice.forEach(s => {
      const sym = s.symbol || '?';
      if (!perSymbol[sym]) perSymbol[sym] = { paperR: [], realR: [] };
      perSymbol[sym].paperR.push(parseFloat(s.exit_r || 0) || 0);
    });
    realSlice.forEach(s => {
      const sym = s.symbol || '?';
      if (!perSymbol[sym]) perSymbol[sym] = { paperR: [], realR: [] };
      let r = parseFloat(s.exit_r || 0);
      if (!r || isNaN(r)) {
        const pnlPct = parseFloat(s.pnl_pct || 0) || 0;
        const slDist = parseFloat(s.initial_risk || 0) || 0;
        const entry = parseFloat(s.entry_price || 0) || 0;
        if (entry > 0 && slDist > 0) r = (pnlPct / 100) * entry / slDist;
      }
      perSymbol[sym].realR.push(r);
    });

    const symLabels = [];
    const divVals = [];
    const divColors = [];
    for (const sym of Object.keys(perSymbol).sort()) {
      const p = perSymbol[sym];
      if (p.paperR.length === 0 || p.realR.length === 0) continue;
      const avgP = p.paperR.reduce((a, b) => a + b, 0) / p.paperR.length;
      const avgR = p.realR.reduce((a, b) => a + b, 0) / p.realR.length;
      const diff = avgP - avgR;
      symLabels.push(sym.replace('/USDT', ''));
      divVals.push(diff);
      divColors.push(Math.abs(diff) > 0.5 ? 'rgba(255,59,92,.75)' : Math.abs(diff) > 0.25 ? 'rgba(255,215,0,.75)' : 'rgba(0,255,157,.75)');
    }

    const dvCanvas = document.getElementById('divergence-canvas');
    if (dvCanvas && typeof Chart !== 'undefined') {
      if (window._dvChart) { window._dvChart.destroy(); }
      if (symLabels.length > 0) {
        window._dvChart = new Chart(dvCanvas, {
          type: 'bar',
          data: {
            labels: symLabels,
            datasets: [{
              label: 'Paper R − Real R',
              data: divVals,
              backgroundColor: divColors,
              borderWidth: 0,
            }],
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: {
              x: { ticks: { color: 'rgba(255,255,255,.5)', font: { size: 10 } }, grid: { display: false } },
              y: { ticks: { color: 'rgba(255,255,255,.4)', font: { size: 9 }, callback: v => v.toFixed(2) + 'R' }, grid: { color: 'rgba(255,255,255,.04)' } },
            },
          },
        });
      } else {
        // No divergence data yet
        const ctx = dvCanvas.getContext('2d');
        ctx.clearRect(0, 0, dvCanvas.width, dvCanvas.height);
        ctx.fillStyle = 'rgba(255,255,255,.3)';
        ctx.font = '11px SF Mono';
        ctx.textAlign = 'center';
        ctx.fillText('No matched paper/real pairs yet', dvCanvas.width / 2, dvCanvas.height / 2);
      }
    }

    // ── B.7: LOSS ANALYSIS RECOMMENDATIONS ──
    if (lossResp && lossResp.buckets) {
      const buckets = lossResp.buckets;
      const total = lossResp.total_loss_usd || 0;
      const totalN = lossResp.total_losses_analyzed || 0;
      const rows = [];
      // Sort buckets by abs loss desc
      const sorted = Object.entries(buckets)
        .filter(([k, v]) => (v.count || 0) > 0)
        .sort((a, b) => Math.abs(b[1].total_loss_usd || 0) - Math.abs(a[1].total_loss_usd || 0));
      for (const [name, b] of sorted.slice(0, 8)) {
        const loss = b.total_loss_usd || 0;
        const pct = total !== 0 ? (Math.abs(loss) / Math.abs(total) * 100) : 0;
        const barWidth = Math.min(100, pct);
        const symbols = (b.sample_symbols || []).slice(0, 3).join(', ');
        const labelName = name.replace(/_/g, ' ').toUpperCase();
        rows.push(
          `<div style="margin-bottom:6px;padding:4px 8px;border-left:2px solid var(--red);background:rgba(255,59,92,.03)">` +
          `<div style="display:flex;justify-content:space-between;font-weight:700"><span>${labelName}</span><span class="text-danger">${loss.toFixed(2)} (${b.count}t)</span></div>` +
          `<div style="height:3px;background:rgba(255,59,92,.15);border-radius:2px;margin:2px 0"><div style="height:100%;width:${barWidth}%;background:var(--red);border-radius:2px"></div></div>` +
          `<div style="color:var(--text-muted);font-size:.6rem">${symbols || '—'}</div>` +
          `</div>`
        );
      }
      const body = document.getElementById('loss-analysis-body');
      if (body) {
        body.innerHTML = rows.length ? rows.join('') : '<div class="empty">No losses in window</div>';
      }
      const summary = document.getElementById('loss-analysis-summary');
      if (summary) {
        summary.textContent = `${totalN} losses in 24h, total -$${Math.abs(total).toFixed(2)}. Sorted by $ loss impact.`;
      }
    }
  } catch(e) {
    console.warn('loadAnalytics error:', e);
  }
}

// Fire on load + every 30s
try {
  loadAnalytics();
  setInterval(loadAnalytics, 30000);
} catch(e) {}

// ═══ ADMIN TAB ═══════════════════════════════════════════
// Show admin tab button only for admin users
async function checkAdminAccess() {
    try {
        const r = await fetch("/api/session", {credentials:"same-origin"});
        if (r.ok) {
            const d = await r.json();
            console.log("SESSION DATA:", d);
            // Check both flat and nested role field
            var role = d.role || (d.user && d.user.role) || "";
            if (role === "admin") {
                var btn = document.getElementById("admin-tab-btn");
                if (btn) {
                    btn.style.display = "inline-block";
                    console.log("ADMIN TAB: visible");
                }
            }
        }
    } catch(e) { console.error("checkAdminAccess:", e); }
}
// Check on session verify — run immediately + delayed
checkAdminAccess();
setTimeout(checkAdminAccess, 3000);

var adminTimer = null;

async function refreshAdmin() {
    var tab = document.getElementById("tab-admin");
    if (!tab || !tab.classList.contains("active")) return;

    try {
        var [users, sessions, audit] = await Promise.all([
            fetch("/api/admin/users", {credentials:"same-origin"}).then(function(r){return r.ok?r.json():null}).catch(function(){return null}),
            fetch("/api/admin/sessions", {credentials:"same-origin"}).then(function(r){return r.ok?r.json():null}).catch(function(){return null}),
            fetch("/api/admin/audit?limit=50", {credentials:"same-origin"}).then(function(r){return r.ok?r.json():null}).catch(function(){return null}),
        ]);

        // Users table — API returns {users: [...]} or direct array
        console.log("ADMIN RAW users:", JSON.stringify(users).substring(0,200));
        if (users && users.users) users = users.users;
        console.log("ADMIN unwrapped users:", Array.isArray(users), users ? users.length : 0);
        if (users && Array.isArray(users)) {
            var countEl = document.getElementById("admin-user-count");
            if (countEl) countEl.textContent = users.length + " users";
            var statEl = document.getElementById("admin-stat-users");
            if (statEl) statEl.textContent = users.length;

            // Build user cards (not table — cards show full config)
            var container = document.getElementById("admin-users-table").parentElement;
            if (container) {
                var cardsHtml = "";
                for (var i = 0; i < users.length; i++) {
                    var u = users[i];
                    var roleColor = u.role === "admin" ? "#ff3b5c" : u.role === "trader" ? "#00ff9d" : "#5a7090";
                    var modeColor = u.bot_mode === "live" ? "#ff3b5c" : u.bot_mode === "demo" ? "#00d4ff" : "#5a7090";
                    var activeColor = u.is_active ? "#00ff9d" : "#ff3b5c";
                    var lastLogin = u.last_login ? formatTime(u.last_login) : "Never";
                    var pairs = u.trading_pairs;
                    if (typeof pairs === "string") try { pairs = JSON.parse(pairs); } catch(e) { pairs = []; }
                    if (!Array.isArray(pairs)) pairs = [];

                    cardsHtml += '<div style="background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06);border-radius:10px;padding:16px;margin-bottom:12px">' +
                        // Header row
                        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">' +
                            '<div style="display:flex;align-items:center;gap:10px">' +
                                '<span style="font-weight:700;font-size:14px;color:#e8ecf4">' + (u.email||"--") + '</span>' +
                                '<span style="padding:2px 8px;border-radius:12px;font-size:10px;font-weight:700;color:' + roleColor + ';border:1px solid ' + roleColor + '30;background:' + roleColor + '15">' + (u.role||"--").toUpperCase() + '</span>' +
                                '<span style="padding:2px 8px;border-radius:12px;font-size:10px;font-weight:700;color:' + modeColor + ';border:1px solid ' + modeColor + '30;background:' + modeColor + '15">' + (u.bot_mode||"paper").toUpperCase() + '</span>' +
                                '<span style="padding:2px 8px;border-radius:12px;font-size:10px;font-weight:600;color:' + activeColor + '">' + (u.is_active ? "Active" : "Disabled") + '</span>' +
                                '<span style="color:#5a7090;font-size:11px">Tier: <b style="color:#a78bfa">' + (u.tier||"free").toUpperCase() + '</b></span>' +
                            '</div>' +
                            '<div style="display:flex;gap:6px">' +
                                '<button onclick="adminToggleUser(\'' + u.id + '\',' + !u.is_active + ')" style="padding:4px 12px;font-size:11px;border:1px solid #333;background:rgba(255,255,255,.05);color:#aaa;border-radius:4px;cursor:pointer">' + (u.is_active?"Disable":"Enable") + '</button>' +
                                '<button onclick="adminResetPassword(\'' + u.id + '\',\'' + (u.email||"") + '\')" style="padding:4px 12px;font-size:11px;border:1px solid #f97316;background:rgba(249,115,22,.1);color:#f97316;border-radius:4px;cursor:pointer">Reset PW</button>' +
                                '<button onclick="adminEditUser(\'' + u.id + '\')" style="padding:4px 12px;font-size:11px;border:1px solid #00d4ff;background:rgba(0,212,255,.1);color:#00d4ff;border-radius:4px;cursor:pointer">Edit</button>' +
                            '</div>' +
                        '</div>' +
                        // Config grid
                        '<div style="display:grid;grid-template-columns:repeat(6,1fr);gap:8px;font-size:12px">' +
                            '<div style="background:rgba(255,255,255,.02);padding:8px;border-radius:6px"><div style="color:#5a7090;font-size:10px;text-transform:uppercase;margin-bottom:2px">Max Leverage</div><div style="font-weight:700;font-family:monospace;color:#ffd700">' + (u.max_leverage||20) + 'x</div></div>' +
                            '<div style="background:rgba(255,255,255,.02);padding:8px;border-radius:6px"><div style="color:#5a7090;font-size:10px;text-transform:uppercase;margin-bottom:2px">Daily Loss Limit</div><div style="font-weight:700;font-family:monospace;color:#ff3b5c">' + (u.max_daily_loss_pct||3) + '%</div></div>' +
                            '<div style="background:rgba(255,255,255,.02);padding:8px;border-radius:6px"><div style="color:#5a7090;font-size:10px;text-transform:uppercase;margin-bottom:2px">Max Positions</div><div style="font-weight:700;font-family:monospace;color:#00d4ff">' + (u.max_open_positions||3) + '</div></div>' +
                            '<div style="background:rgba(255,255,255,.02);padding:8px;border-radius:6px"><div style="color:#5a7090;font-size:10px;text-transform:uppercase;margin-bottom:2px">Pref Leverage</div><div style="font-weight:700;font-family:monospace;color:#ffd700">' + (u.preferred_leverage||5) + 'x</div></div>' +
                            '<div style="background:rgba(255,255,255,.02);padding:8px;border-radius:6px"><div style="color:#5a7090;font-size:10px;text-transform:uppercase;margin-bottom:2px">Risk/Trade</div><div style="font-weight:700;font-family:monospace;color:#f97316">' + (u.risk_per_trade_pct||1) + '%</div></div>' +
                            '<div style="background:rgba(255,255,255,.02);padding:8px;border-radius:6px"><div style="color:#5a7090;font-size:10px;text-transform:uppercase;margin-bottom:2px">Timezone</div><div style="font-weight:600;color:#9ba3b5;font-size:11px">' + (u.timezone||"UTC") + '</div></div>' +
                        '</div>' +
                        // API Keys section
                        '<div style="margin-top:10px;padding-top:8px;border-top:1px solid rgba(255,255,255,.06);display:flex;align-items:center;gap:8px">' +
                            '<span style="color:#5a7090;font-size:11px;font-weight:600">API KEYS:</span>' +
                            '<span id="admin-keys-' + u.id + '" style="font-size:11px;color:#888">loading...</span>' +
                            '<button onclick="adminAddApiKey(\'' + u.id + '\',\'' + (u.email||"") + '\')" style="padding:2px 8px;font-size:10px;border:1px solid #00ff9d;background:rgba(0,255,157,.08);color:#00ff9d;border-radius:3px;cursor:pointer;margin-left:auto">+ Add Key</button>' +
                        '</div>' +
                        // Trading pairs + extra info
                        '<div style="display:flex;justify-content:space-between;align-items:center;margin-top:8px;font-size:11px">' +
                            '<div style="color:#5a7090">Pairs: ' + pairs.map(function(p){return '<span style="color:#00d4ff;font-weight:600;margin-right:4px">'+p+'</span>';}).join("") + '</div>' +
                            '<div style="color:#5a7090">Last login: <span style="color:#9ba3b5">' + lastLogin + '</span>' +
                                (u.telegram_chat_id ? ' | Telegram: <span style="color:#00d4ff">' + u.telegram_chat_id + '</span>' : '') +
                                (u.full_name ? ' | ' + u.full_name : '') +
                            '</div>' +
                        '</div>' +
                    '</div>';
                }
                container.innerHTML = cardsHtml;
                // Load API keys for each user
                for (var ki = 0; ki < users.length; ki++) {
                    (function(uid) {
                        fetch("/api/admin/users/" + uid + "/api-keys", {credentials:"same-origin"})
                            .then(function(r){return r.ok?r.json():null})
                            .then(function(d) {
                                var el = document.getElementById("admin-keys-" + uid);
                                if (!el || !d) return;
                                var keys = d.keys || [];
                                if (keys.length === 0) {
                                    el.innerHTML = '<span style="color:#ff8800">No keys configured</span>';
                                } else {
                                    el.innerHTML = keys.map(function(k) {
                                        var lc = k.label === "live" ? "#ff3b5c" : "#00d4ff";
                                        return '<span style="display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:4px;background:rgba(255,255,255,.04);border:1px solid ' + lc + '30;margin-right:4px">' +
                                            '<span style="color:' + lc + ';font-weight:700;font-size:10px">' + k.label.toUpperCase() + '</span>' +
                                            '<span style="color:#888;font-family:monospace;font-size:10px">' + (k.api_key_masked||"****") + '</span>' +
                                            (k.is_active ? '<span style="color:#00ff9d;font-size:9px">active</span>' : '<span style="color:#ff3b5c;font-size:9px">disabled</span>') +
                                            '<button onclick="adminDeleteApiKey(\'' + k.id + '\')" style="background:none;border:none;color:#ff3b5c;cursor:pointer;font-size:12px;padding:0 2px" title="Delete">&times;</button>' +
                                        '</span>';
                                    }).join("");
                                }
                            }).catch(function(){});
                    })(users[ki].id);
                }
            }
        }

        // Sessions table — API returns {sessions: [...]}
        if (sessions && sessions.sessions) sessions = sessions.sessions;
        if (sessions && Array.isArray(sessions)) {
            var sessStatEl = document.getElementById("admin-stat-sessions");
            if (sessStatEl) sessStatEl.textContent = sessions.length;

            var sessBody = document.getElementById("admin-sessions-body");
            if (sessBody) {
                if (sessions.length === 0) {
                    sessBody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:#666;padding:20px">No active sessions</td></tr>';
                } else {
                    var shtml = "";
                    for (var si = 0; si < sessions.length; si++) {
                        var s = sessions[si];
                        shtml += "<tr>" +
                            "<td style='padding:8px;font-weight:600'>" + (s.email||s.user_id||"--") + "</td>" +
                            "<td style='padding:8px;color:#5a7090;font-family:monospace;font-size:11px'>" + (s.ip_address||"--") + "</td>" +
                            "<td style='padding:8px;font-size:12px'>" + (s.last_activity ? formatTime(s.last_activity) : "--") + "</td>" +
                            "<td style='padding:8px;font-family:monospace'>" + (s.request_count||0) + "</td>" +
                            "<td style='padding:8px'><button onclick=\"adminKillSession('" + (s.token||"").substring(0,8) + "')\" style='padding:3px 8px;font-size:11px;border:1px solid #ff3b5c;background:rgba(255,59,92,.1);color:#ff3b5c;border-radius:4px;cursor:pointer'>Kill</button></td>" +
                        "</tr>";
                    }
                    sessBody.innerHTML = shtml;
                }
            }
        }

        // Audit log — API returns {entries: [...]}
        if (audit && audit.entries) audit = audit.entries;
        if (audit && Array.isArray(audit)) {
            var auditBody = document.getElementById("admin-audit-body");
            if (auditBody) {
                if (audit.length === 0) {
                    auditBody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:#666;padding:20px">No login history</td></tr>';
                } else {
                    var ahtml = "";
                    for (var ai = 0; ai < audit.length; ai++) {
                        var a = audit[ai];
                        var resultColor = a.success ? "#00ff9d" : "#ff3b5c";
                        ahtml += "<tr>" +
                            "<td style='padding:8px;font-size:12px;color:#5a7090'>" + formatTime(a.created_at) + "</td>" +
                            "<td style='padding:8px;font-weight:600'>" + (a.email||"--") + "</td>" +
                            "<td style='padding:8px;font-family:monospace;font-size:11px;color:#5a7090'>" + (a.ip_address||"--") + "</td>" +
                            "<td style='padding:8px;color:" + resultColor + ";font-weight:700'>" + (a.success ? "OK" : "FAIL") + "</td>" +
                            "<td style='padding:8px;font-size:12px;color:#5a7090'>" + (a.failure_reason||"--") + "</td>" +
                        "</tr>";
                    }
                    auditBody.innerHTML = ahtml;
                }
            }
        }

    } catch(e) { console.error("refreshAdmin:", e); }
}

async function refreshAdminUsers() { await refreshAdmin(); }

async function adminToggleUser(userId, newState) {
    if (!confirm((newState ? "Activate" : "Deactivate") + " this user?")) return;
    try {
        await fetch("/api/admin/users/" + userId, {
            method: "PUT",
            headers: {"Content-Type":"application/json"},
            credentials: "same-origin",
            body: JSON.stringify({is_active: newState})
        });
        refreshAdmin();
    } catch(e) { alert("Failed: " + e); }
}

async function adminKillSession(tokenPrefix) {
    if (!confirm("Force logout this session?")) return;
    try {
        await fetch("/api/admin/sessions/" + tokenPrefix, {
            method: "DELETE",
            credentials: "same-origin"
        });
        refreshAdmin();
    } catch(e) { alert("Failed: " + e); }
}

async function adminAddApiKey(userId, email) {
    var html = '<div style="background:rgba(15,25,45,.95);border:1px solid rgba(0,255,157,.2);border-radius:12px;padding:24px;max-width:450px;margin:20px auto">' +
        '<h3 style="color:#00ff9d;margin:0 0 16px">Add API Key: ' + email + '</h3>' +
        '<div style="display:grid;gap:10px;font-size:13px">' +
            '<label style="color:#9ba3b5">Label<select id="admin-ak-label" style="width:100%;padding:6px;background:#0a1429;color:#e8ecf4;border:1px solid #333;border-radius:4px;margin-top:4px"><option value="demo">Demo (Testnet)</option><option value="live">Live (Real Money)</option></select></label>' +
            '<label style="color:#9ba3b5">API Key<input id="admin-ak-key" type="text" placeholder="Enter Delta API Key" style="width:100%;padding:6px;background:#0a1429;color:#e8ecf4;border:1px solid #333;border-radius:4px;margin-top:4px;font-family:monospace"></label>' +
            '<label style="color:#9ba3b5">API Secret<input id="admin-ak-secret" type="password" placeholder="Enter Delta API Secret" style="width:100%;padding:6px;background:#0a1429;color:#e8ecf4;border:1px solid #333;border-radius:4px;margin-top:4px;font-family:monospace"></label>' +
            '<label style="color:#9ba3b5">Base URL (optional)<input id="admin-ak-url" type="text" placeholder="https://cdn-ind.testnet.deltaex.org (leave empty for default)" style="width:100%;padding:6px;background:#0a1429;color:#e8ecf4;border:1px solid #333;border-radius:4px;margin-top:4px;font-size:11px"></label>' +
        '</div>' +
        '<div style="background:rgba(255,215,0,.08);border:1px solid rgba(255,215,0,.2);border-radius:6px;padding:8px;margin-top:12px;font-size:11px;color:#ffd700">' +
            'Keys are encrypted with Fernet (AES-128-CBC) before storage. Only the last 4 characters are visible after saving.' +
        '</div>' +
        '<div style="display:flex;gap:10px;margin-top:16px;justify-content:flex-end">' +
            '<button onclick="document.getElementById(\'admin-edit-modal\').style.display=\'none\'" style="padding:8px 20px;border:1px solid #333;background:transparent;color:#aaa;border-radius:6px;cursor:pointer">Cancel</button>' +
            '<button onclick="adminSaveApiKey(\'' + userId + '\')" style="padding:8px 20px;border:1px solid #00ff9d;background:rgba(0,255,157,.15);color:#00ff9d;border-radius:6px;cursor:pointer;font-weight:700">Save Key</button>' +
        '</div>' +
    '</div>';

    var modal = document.getElementById("admin-edit-modal");
    if (!modal) {
        modal = document.createElement("div");
        modal.id = "admin-edit-modal";
        modal.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:9999;display:flex;align-items:center;justify-content:center";
        modal.onclick = function(e) { if (e.target === modal) modal.style.display = "none"; };
        document.body.appendChild(modal);
    }
    modal.innerHTML = html;
    modal.style.display = "flex";
}

async function adminSaveApiKey(userId) {
    var label = document.getElementById("admin-ak-label").value;
    var apiKey = document.getElementById("admin-ak-key").value.trim();
    var apiSecret = document.getElementById("admin-ak-secret").value.trim();
    var baseUrl = document.getElementById("admin-ak-url").value.trim();

    if (!apiKey || !apiSecret) { alert("API Key and Secret are required"); return; }

    if (label === "live" && !confirm("You are adding a LIVE (real money) API key. Are you sure?")) return;

    try {
        var r = await fetch("/api/admin/users/" + userId + "/api-keys", {
            method: "POST",
            headers: {"Content-Type":"application/json"},
            credentials: "same-origin",
            body: JSON.stringify({api_key: apiKey, api_secret: apiSecret, label: label, base_url: baseUrl})
        });
        var d = await r.json();
        if (d.ok) {
            document.getElementById("admin-edit-modal").style.display = "none";
            alert(d.message || "API key saved");
            refreshAdmin();
        } else {
            alert("Error: " + (d.error || "unknown"));
        }
    } catch(e) { alert("Failed: " + e); }
}

async function adminDeleteApiKey(keyId) {
    if (!confirm("Delete this API key? The user will lose exchange access.")) return;
    try {
        var r = await fetch("/api/admin/api-keys/" + keyId, {
            method: "DELETE",
            credentials: "same-origin"
        });
        var d = await r.json();
        if (d.ok) {
            alert("API key deleted");
            refreshAdmin();
        } else {
            alert("Error: " + (d.error || "unknown"));
        }
    } catch(e) { alert("Failed: " + e); }
}

async function adminEditUser(userId) {
    // Find user in cached data
    try {
        var r = await fetch("/api/admin/users", {credentials:"same-origin"});
        var d = await r.json();
        var users = d.users || d;
        var u = null;
        for (var i = 0; i < users.length; i++) { if (users[i].id === userId) { u = users[i]; break; } }
        if (!u) { alert("User not found"); return; }

        var pairs = u.trading_pairs;
        if (typeof pairs === "string") try { pairs = JSON.parse(pairs); } catch(e) { pairs = []; }

        var html = '<div style="background:rgba(15,25,45,.95);border:1px solid rgba(0,212,255,.2);border-radius:12px;padding:24px;max-width:500px;margin:20px auto">' +
            '<h3 style="color:#00d4ff;margin:0 0 16px">Edit User: ' + u.email + '</h3>' +
            '<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;font-size:13px">' +
                '<label style="color:#9ba3b5">Role<select id="eu-role" style="width:100%;padding:6px;background:#0a1429;color:#e8ecf4;border:1px solid #333;border-radius:4px;margin-top:4px"><option value="admin"'+(u.role==="admin"?" selected":"")+'>Admin</option><option value="trader"'+(u.role==="trader"?" selected":"")+'>Trader</option><option value="viewer"'+(u.role==="viewer"?" selected":"")+'>Viewer</option></select></label>' +
                '<label style="color:#9ba3b5">Tier<select id="eu-tier" style="width:100%;padding:6px;background:#0a1429;color:#e8ecf4;border:1px solid #333;border-radius:4px;margin-top:4px"><option value="free"'+(u.tier==="free"?" selected":"")+'>Free</option><option value="pro"'+(u.tier==="pro"?" selected":"")+'>Pro</option><option value="enterprise"'+(u.tier==="enterprise"?" selected":"")+'>Enterprise</option></select></label>' +
                '<label style="color:#9ba3b5">Bot Mode<select id="eu-mode" style="width:100%;padding:6px;background:#0a1429;color:#e8ecf4;border:1px solid #333;border-radius:4px;margin-top:4px"><option value="paper"'+(u.bot_mode==="paper"?" selected":"")+'>Paper</option><option value="demo"'+(u.bot_mode==="demo"?" selected":"")+'>Demo</option><option value="live"'+(u.bot_mode==="live"?" selected":"")+'>Live</option></select></label>' +
                '<label style="color:#9ba3b5">Max Leverage<input id="eu-lev" type="number" value="'+(u.max_leverage||20)+'" min="1" max="50" style="width:100%;padding:6px;background:#0a1429;color:#e8ecf4;border:1px solid #333;border-radius:4px;margin-top:4px"></label>' +
                '<label style="color:#9ba3b5">Daily Loss %<input id="eu-loss" type="number" value="'+(u.max_daily_loss_pct||3)+'" min="0.5" max="20" step="0.5" style="width:100%;padding:6px;background:#0a1429;color:#e8ecf4;border:1px solid #333;border-radius:4px;margin-top:4px"></label>' +
                '<label style="color:#9ba3b5">Max Positions<input id="eu-pos" type="number" value="'+(u.max_open_positions||3)+'" min="1" max="10" style="width:100%;padding:6px;background:#0a1429;color:#e8ecf4;border:1px solid #333;border-radius:4px;margin-top:4px"></label>' +
                '<label style="color:#9ba3b5">Risk/Trade %<input id="eu-risk" type="number" value="'+(u.risk_per_trade_pct||1)+'" min="0.1" max="10" step="0.1" style="width:100%;padding:6px;background:#0a1429;color:#e8ecf4;border:1px solid #333;border-radius:4px;margin-top:4px"></label>' +
                '<label style="color:#9ba3b5">Pref Leverage<input id="eu-plev" type="number" value="'+(u.preferred_leverage||5)+'" min="1" max="50" style="width:100%;padding:6px;background:#0a1429;color:#e8ecf4;border:1px solid #333;border-radius:4px;margin-top:4px"></label>' +
            '</div>' +
            '<label style="color:#9ba3b5;display:block;margin-top:10px">Trading Pairs (comma separated)<input id="eu-pairs" type="text" value="'+(Array.isArray(pairs)?pairs.join(", "):"")+'" style="width:100%;padding:6px;background:#0a1429;color:#e8ecf4;border:1px solid #333;border-radius:4px;margin-top:4px"></label>' +
            '<label style="color:#9ba3b5;display:block;margin-top:10px">Telegram Chat ID<input id="eu-tg" type="text" value="'+(u.telegram_chat_id||"")+'" style="width:100%;padding:6px;background:#0a1429;color:#e8ecf4;border:1px solid #333;border-radius:4px;margin-top:4px"></label>' +
            '<div style="display:flex;gap:10px;margin-top:16px;justify-content:flex-end">' +
                '<button onclick="document.getElementById(\'admin-edit-modal\').style.display=\'none\'" style="padding:8px 20px;border:1px solid #333;background:transparent;color:#aaa;border-radius:6px;cursor:pointer">Cancel</button>' +
                '<button onclick="adminSaveUser(\''+userId+'\')" style="padding:8px 20px;border:1px solid #00d4ff;background:rgba(0,212,255,.15);color:#00d4ff;border-radius:6px;cursor:pointer;font-weight:700">Save</button>' +
            '</div>' +
        '</div>';

        var modal = document.getElementById("admin-edit-modal");
        if (!modal) {
            modal = document.createElement("div");
            modal.id = "admin-edit-modal";
            modal.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:9999;display:flex;align-items:center;justify-content:center";
            modal.onclick = function(e) { if (e.target === modal) modal.style.display = "none"; };
            document.body.appendChild(modal);
        }
        modal.innerHTML = html;
        modal.style.display = "flex";
    } catch(e) { alert("Error: " + e); }
}

async function adminSaveUser(userId) {
    var body = {
        role: document.getElementById("eu-role").value,
        tier: document.getElementById("eu-tier").value,
        bot_mode: document.getElementById("eu-mode").value,
        max_leverage: parseInt(document.getElementById("eu-lev").value),
        max_daily_loss_pct: parseFloat(document.getElementById("eu-loss").value),
        max_open_positions: parseInt(document.getElementById("eu-pos").value),
        risk_per_trade_pct: parseFloat(document.getElementById("eu-risk").value),
        preferred_leverage: parseInt(document.getElementById("eu-plev").value),
        telegram_chat_id: document.getElementById("eu-tg").value,
    };
    var pairsStr = document.getElementById("eu-pairs").value;
    if (pairsStr) body.trading_pairs = pairsStr.split(",").map(function(s){return s.trim();}).filter(Boolean);

    try {
        var r = await fetch("/api/admin/users/" + userId, {
            method: "PUT",
            headers: {"Content-Type":"application/json"},
            credentials: "same-origin",
            body: JSON.stringify(body)
        });
        var d = await r.json();
        if (d.ok) {
            document.getElementById("admin-edit-modal").style.display = "none";
            alert("User updated");
            refreshAdmin();
        } else {
            alert("Error: " + (d.error || "unknown"));
        }
    } catch(e) { alert("Failed: " + e); }
}

async function adminResetPassword(userId, email) {
    var newPass = prompt("Enter new password for " + email + "\n(minimum 8 characters):");
    if (!newPass) return;
    if (newPass.length < 8) { alert("Password must be at least 8 characters"); return; }
    if (!confirm("Reset password for " + email + "?\nThey will be logged out of all sessions.")) return;
    try {
        var r = await fetch("/api/admin/users/" + userId + "/reset-password", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            credentials: "same-origin",
            body: JSON.stringify({new_password: newPass})
        });
        var d = await r.json();
        if (d.ok) {
            alert("Password reset for " + email + ". They need to login again.");
            refreshAdmin();
        } else {
            alert("Error: " + (d.error || "unknown"));
        }
    } catch(e) { alert("Failed: " + e); }
}

