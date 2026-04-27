/* ═══════════════════════════════════════════════════════════
   VN Edge Admin Module
   Extracted from app.js — user management, sessions, audit, keys
   ═══════════════════════════════════════════════════════════ */

'use strict';

// ═══ ADMIN TAB ═══════════════════════════════════════════
// Show admin tab button only for admin users
async function checkAdminAccess() {
    try {
        const r = await fetch("/api/session", {credentials:"same-origin"});
        if (r.ok) {
            const d = await r.json();
            console.log("SESSION DATA:", d);
            // Check both flat and nested role field
            let role = d.role || (d.user && d.user.role) || "";
            if (role === "admin") {
                let btn = document.getElementById("admin-tab-btn");
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

// adminTimer declared globally with other timers (line 348)

async function refreshAdmin() {
    let tab = document.getElementById("tab-admin");
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
            let countEl = document.getElementById("admin-user-count");
            if (countEl) countEl.textContent = users.length + " users";
            let statEl = document.getElementById("admin-stat-users");
            if (statEl) statEl.textContent = users.length;

            // Build user cards (not table — cards show full config)
            let container = document.getElementById("admin-users-table").parentElement;
            if (container) {
                let cardsHtml = "";
                for (var i = 0; i < users.length; i++) {
                    let u = users[i];
                    let roleColor = u.role === "admin" ? "#ff3b5c" : u.role === "trader" ? "#00ff9d" : "#5a7090";
                    let modeColor = u.bot_mode === "live" ? "#ff3b5c" : u.bot_mode === "demo" ? "#00d4ff" : "#5a7090";
                    let activeColor = u.is_active ? "#00ff9d" : "#ff3b5c";
                    let lastLogin = u.last_login ? formatTime(u.last_login) : "Never";
                    let pairs = u.trading_pairs;
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
                                '<span style="color:#5a7090;font-size:11px">Tier: <b class="text-purple">' + (u.tier||"free").toUpperCase() + '</b></span>' +
                            '</div>' +
                            '<div style="display:flex;gap:6px">' +
                                '<button onclick="adminToggleUser(\'' + u.id + '\',' + !u.is_active + ')" style="padding:4px 12px;font-size:11px;border:1px solid #333;background:rgba(255,255,255,.05);color:#aaa;border-radius:4px;cursor:pointer">' + (u.is_active?"Disable":"Enable") + '</button>' +
                                '<button onclick="adminResetPassword(\'' + u.id + '\',\'' + (u.email||"") + '\')" style="padding:4px 12px;font-size:11px;border:1px solid #f97316;background:rgba(249,115,22,.1);color:#f97316;border-radius:4px;cursor:pointer">Reset PW</button>' +
                                '<button onclick="adminEditUser(\'' + u.id + '\')" style="padding:4px 12px;font-size:11px;border:1px solid #00d4ff;background:rgba(0,212,255,.1);color:#00d4ff;border-radius:4px;cursor:pointer">Edit</button>' +
                                '<button onclick="adminDeleteUser(\'' + u.id + '\',\'' + (u.email||"") + '\')" style="padding:4px 12px;font-size:11px;border:1px solid #ff3b5c;background:rgba(255,59,92,.1);color:#ff3b5c;border-radius:4px;cursor:pointer" title="Delete user permanently">Delete</button>' +
                            '</div>' +
                        '</div>' +
                        // Config grid
                        '<div style="display:grid;grid-template-columns:repeat(6,1fr);gap:8px;font-size:12px">' +
                            '<div class="bg-card-soft"><div class="metric-tiny-label">Max Leverage</div><div style="font-weight:700;font-family:monospace;color:#ffd700">' + (u.max_leverage||20) + 'x</div></div>' +
                            '<div class="bg-card-soft"><div class="metric-tiny-label">Daily Loss Limit</div><div style="font-weight:700;font-family:monospace;color:#ff3b5c">' + (u.max_daily_loss_pct||3) + '%</div></div>' +
                            '<div class="bg-card-soft"><div class="metric-tiny-label">Max Positions</div><div style="font-weight:700;font-family:monospace;color:#00d4ff">' + (u.max_open_positions||3) + '</div></div>' +
                            '<div class="bg-card-soft"><div class="metric-tiny-label">Pref Leverage</div><div style="font-weight:700;font-family:monospace;color:#ffd700">' + (u.preferred_leverage||5) + 'x</div></div>' +
                            '<div class="bg-card-soft"><div class="metric-tiny-label">Risk/Trade</div><div style="font-weight:700;font-family:monospace;color:#f97316">' + (u.risk_per_trade_pct||1) + '%</div></div>' +
                            '<div class="bg-card-soft"><div class="metric-tiny-label">Timezone</div><div style="font-weight:600;color:#9ba3b5;font-size:11px">' + (u.timezone||"Asia/Kolkata") + '</div></div>' +
                        '</div>' +
                        // API Keys section
                        '<div style="margin-top:10px;padding-top:8px;border-top:1px solid rgba(255,255,255,.06);display:flex;align-items:center;gap:8px">' +
                            '<span style="color:#5a7090;font-size:11px;font-weight:600">API KEYS:</span>' +
                            '<span id="admin-keys-' + u.id + '" style="font-size:11px;color:#888">loading...</span>' +
                            '<button onclick="adminAddApiKey(\'' + u.id + '\',\'' + (u.email||"") + '\')" style="padding:2px 8px;font-size:10px;border:1px solid #00ff9d;background:rgba(0,255,157,.08);color:#00ff9d;border-radius:3px;cursor:pointer;margin-left:auto">+ Add Key</button>' +
                        '</div>' +
                        // Trading pairs + extra info
                        '<div style="display:flex;justify-content:space-between;align-items:center;margin-top:8px;font-size:11px">' +
                            '<div class="text-muted">Pairs: ' + pairs.map(function(p){return '<span style="color:#00d4ff;font-weight:600;margin-right:4px">'+p+'</span>';}).join("") + '</div>' +
                            '<div class="text-muted">Last login: <span class="text-9ba3b5">' + lastLogin + '</span>' +
                                (u.telegram_chat_id ? ' | Telegram: <span class="text-info">' + u.telegram_chat_id + '</span>' : '') +
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
                                let el = document.getElementById("admin-keys-" + uid);
                                if (!el || !d) return;
                                let keys = d.keys || [];
                                if (keys.length === 0) {
                                    el.innerHTML = '<span class="text-orange-soft">No keys configured</span>';
                                } else {
                                    el.innerHTML = keys.map(function(k) {
                                        let lc = k.label === "live" ? "#ff3b5c" : "#00d4ff";
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
            let sessStatEl = document.getElementById("admin-stat-sessions");
            if (sessStatEl) sessStatEl.textContent = sessions.length;

            let sessBody = document.getElementById("admin-sessions-body");
            if (sessBody) {
                if (sessions.length === 0) {
                    sessBody.innerHTML = '<tr><td colspan="5" class="empty-state-pad">No active sessions</td></tr>';
                } else {
                    let shtml = "";
                    for (var si = 0; si < sessions.length; si++) {
                        let s = sessions[si];
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
            let auditBody = document.getElementById("admin-audit-body");
            if (auditBody) {
                if (audit.length === 0) {
                    auditBody.innerHTML = '<tr><td colspan="5" class="empty-state-pad">No login history</td></tr>';
                } else {
                    let ahtml = "";
                    for (var ai = 0; ai < audit.length; ai++) {
                        let a = audit[ai];
                        let resultColor = a.success ? "#00ff9d" : "#ff3b5c";
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

async function refreshAttribution() {
    try {
        const r = await fetch("/api/attribution", {credentials:"same-origin"});
        const d = await r.json();
        const rows = d.attribution || [];
        const body = document.getElementById("attribution-body");
        if (!body) return;
        if (rows.length === 0) {
            body.innerHTML = '<tr><td colspan="7" class="empty-state">No closed trades yet</td></tr>';
            return;
        }
        body.innerHTML = rows.slice(0, 50).map(function(r) {
            const wrClr = r.wr_pct >= 60 ? "#00ff9d" : r.wr_pct >= 45 ? "#ffd700" : "#ff3b5c";
            const pnlClr = r.total_pnl >= 0 ? "#00ff9d" : "#ff3b5c";
            return "<tr>" +
                "<td style='padding:6px 8px;font-weight:600'>" + (r.scanner||"--") + "</td>" +
                "<td style='padding:6px 8px;color:#9ba3b5'>" + (r.regime||"--") + "</td>" +
                "<td style='padding:6px 8px;color:#a78bfa'>" + (r.grade||"--") + "</td>" +
                "<td style='padding:6px 8px;text-align:right;font-family:monospace'>" + r.trades + "</td>" +
                "<td style='padding:6px 8px;text-align:right;color:" + wrClr + ";font-weight:700'>" + r.wr_pct + "%</td>" +
                "<td style='padding:6px 8px;text-align:right;font-family:monospace'>$" + r.avg_pnl + "</td>" +
                "<td style='padding:6px 8px;text-align:right;color:" + pnlClr + ";font-weight:700;font-family:monospace'>$" + r.total_pnl.toFixed(2) + "</td>" +
            "</tr>";
        }).join("");
    } catch(e) { console.error("refreshAttribution:", e); }
}

async function runBacktest() {
    const symbolsStr = document.getElementById("bt-symbols").value.trim();
    const scannersStr = document.getElementById("bt-scanners").value.trim();
    const body = {
        symbols: symbolsStr ? symbolsStr.split(",").map(s => s.trim()).filter(Boolean) : [],
        scanners: scannersStr ? scannersStr.split(",").map(s => s.trim()).filter(Boolean) : [],
        ml_threshold: parseFloat(document.getElementById("bt-ml").value),
        confidence_floor: parseInt(document.getElementById("bt-conf").value),
    };
    const resEl = document.getElementById("bt-result");
    if (resEl) resEl.textContent = "Running backtest...";
    try {
        const r = await fetch("/api/backtest/run", {
            method: "POST",
            headers: {"Content-Type":"application/json"},
            credentials: "same-origin",
            body: JSON.stringify(body)
        });
        const d = await r.json();
        if (d.error) {
            resEl.textContent = "Error: " + d.error;
            resEl.style.color = "#ff3b5c";
            return;
        }
        const wrClr = d.win_rate >= 60 ? "#00ff9d" : d.win_rate >= 45 ? "#ffd700" : "#ff3b5c";
        const pnlClr = d.total_pnl >= 0 ? "#00ff9d" : "#ff3b5c";
        resEl.innerHTML = '<div class="grid-4-eq" style="margin-top:12px">' +
            '<div class="bg-card-medium"><div class="metric-tiny-label">Trades</div><div class="metric-md-value">' + d.total_trades + '</div></div>' +
            '<div class="bg-card-medium"><div class="metric-tiny-label">Win Rate</div><div class="metric-md-value" style="color:' + wrClr + '">' + d.win_rate + '%</div></div>' +
            '<div class="bg-card-medium"><div class="metric-tiny-label">Total PnL</div><div class="metric-md-value" style="color:' + pnlClr + '">$' + d.total_pnl + '</div></div>' +
            '<div class="bg-card-medium"><div class="metric-tiny-label">Avg R</div><div class="metric-md-value">' + d.avg_r.toFixed(2) + 'R</div></div>' +
            '<div class="bg-card-medium"><div class="metric-tiny-label">Max DD</div><div class="metric-md-value" style="color:#ff3b5c">$' + d.max_drawdown + '</div></div>' +
            '<div class="bg-card-medium"><div class="metric-tiny-label">Sharpe</div><div class="metric-md-value">' + d.sharpe + '</div></div>' +
            '<div class="bg-card-medium"><div class="metric-tiny-label">Wins</div><div class="metric-md-value text-success">' + d.wins + '</div></div>' +
            '<div class="bg-card-medium"><div class="metric-tiny-label">Losses</div><div class="metric-md-value text-danger">' + d.losses + '</div></div>' +
            '</div>';
        resEl.style.color = "";
    } catch(e) {
        resEl.textContent = "Error: " + e.message;
        resEl.style.color = "#ff3b5c";
    }
}

async function adminAddUser() {
    const html = '<div style="background:rgba(15,25,45,.95);border:1px solid rgba(0,255,157,.2);border-radius:12px;padding:24px;max-width:450px;margin:20px auto">' +
        '<h3 style="color:#00ff9d;margin:0 0 16px">Create New User</h3>' +
        '<div class="grid-1 gap-2" style="display:grid;gap:10px;font-size:13px">' +
            '<label class="text-9ba3b5">Email<input id="nu-email" type="email" class="form-input-dark" required></label>' +
            '<label class="text-9ba3b5">Password<input id="nu-pw" type="password" class="form-input-dark" placeholder="min 8 chars" required></label>' +
            '<label class="text-9ba3b5">Full Name<input id="nu-name" type="text" class="form-input-dark"></label>' +
            '<label class="text-9ba3b5">Role<select id="nu-role" class="form-input-dark"><option value="trader">Trader</option><option value="admin">Admin</option><option value="viewer">Viewer</option></select></label>' +
            '<label class="text-9ba3b5">Tier<select id="nu-tier" class="form-input-dark"><option value="free">Free</option><option value="pro">Pro</option><option value="enterprise">Enterprise</option></select></label>' +
        '</div>' +
        '<div style="display:flex;gap:10px;margin-top:16px;justify-content:flex-end">' +
            '<button data-action="closeModal" data-arg="admin-edit-modal" style="padding:8px 20px;border:1px solid #333;background:transparent;color:#aaa;border-radius:6px;cursor:pointer">Cancel</button>' +
            '<button data-action="adminSubmitNewUser" style="padding:8px 20px;border:1px solid #00ff9d;background:rgba(0,255,157,.15);color:#00ff9d;border-radius:6px;cursor:pointer;font-weight:700">Create User</button>' +
        '</div>' +
    '</div>';

    let modal = document.getElementById("admin-edit-modal");
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

async function adminSubmitNewUser() {
    const email = document.getElementById("nu-email").value.trim();
    const pw = document.getElementById("nu-pw").value;
    const name = document.getElementById("nu-name").value.trim();
    const role = document.getElementById("nu-role").value;
    const tier = document.getElementById("nu-tier").value;
    if (!email || pw.length < 8) { alert("Email + 8-char password required"); return; }
    try {
        const r = await fetch("/api/admin/users/create", {
            method: "POST",
            headers: {"Content-Type":"application/json"},
            credentials: "same-origin",
            body: JSON.stringify({email, password: pw, full_name: name, role, tier})
        });
        const d = await r.json();
        if (d.ok) {
            alert("User created: " + d.user.email);
            document.getElementById("admin-edit-modal").style.display = "none";
            refreshAdmin();
        } else {
            alert("Error: " + (d.error || "unknown"));
        }
    } catch (e) { alert("Failed: " + e); }
}

async function adminDeleteUser(userId, email) {
    if (!confirm("DELETE user " + email + "?\n\nThis will cascade delete their trades, API keys, and sessions.\nThis action cannot be undone.")) return;
    try {
        const r = await fetch("/api/admin/users/" + userId, {method: "DELETE", credentials: "same-origin"});
        const d = await r.json();
        if (d.ok) { alert("User deleted"); refreshAdmin(); }
        else alert("Error: " + (d.error || "unknown"));
    } catch (e) { alert("Failed: " + e); }
}

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
    let html = '<div style="background:rgba(15,25,45,.95);border:1px solid rgba(0,255,157,.2);border-radius:12px;padding:24px;max-width:450px;margin:20px auto">' +
        '<h3 style="color:#00ff9d;margin:0 0 16px">Add API Key: ' + email + '</h3>' +
        '<div style="display:grid;gap:10px;font-size:13px">' +
            '<label class="text-9ba3b5">Label<select id="admin-ak-label" class="form-input-dark"><option value="demo">Demo (Testnet)</option><option value="live">Live (Real Money)</option></select></label>' +
            '<label class="text-9ba3b5">API Key<input id="admin-ak-key" type="text" placeholder="Enter Delta API Key" style="width:100%;padding:6px;background:#0a1429;color:#e8ecf4;border:1px solid #333;border-radius:4px;margin-top:4px;font-family:monospace"></label>' +
            '<label class="text-9ba3b5">API Secret<input id="admin-ak-secret" type="password" placeholder="Enter Delta API Secret" style="width:100%;padding:6px;background:#0a1429;color:#e8ecf4;border:1px solid #333;border-radius:4px;margin-top:4px;font-family:monospace"></label>' +
            '<label class="text-9ba3b5">Base URL (optional)<input id="admin-ak-url" type="text" placeholder="https://cdn-ind.testnet.deltaex.org (leave empty for default)" style="width:100%;padding:6px;background:#0a1429;color:#e8ecf4;border:1px solid #333;border-radius:4px;margin-top:4px;font-size:11px"></label>' +
        '</div>' +
        '<div style="background:rgba(255,215,0,.08);border:1px solid rgba(255,215,0,.2);border-radius:6px;padding:8px;margin-top:12px;font-size:11px;color:#ffd700">' +
            'Keys are encrypted with Fernet (AES-128-CBC) before storage. Only the last 4 characters are visible after saving.' +
        '</div>' +
        '<div style="display:flex;gap:10px;margin-top:16px;justify-content:flex-end">' +
            '<button onclick="document.getElementById(\'admin-edit-modal\').style.display=\'none\'" style="padding:8px 20px;border:1px solid #333;background:transparent;color:#aaa;border-radius:6px;cursor:pointer">Cancel</button>' +
            '<button onclick="adminSaveApiKey(\'' + userId + '\')" style="padding:8px 20px;border:1px solid #00ff9d;background:rgba(0,255,157,.15);color:#00ff9d;border-radius:6px;cursor:pointer;font-weight:700">Save Key</button>' +
        '</div>' +
    '</div>';

    let modal = document.getElementById("admin-edit-modal");
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
    let label = document.getElementById("admin-ak-label").value;
    let apiKey = document.getElementById("admin-ak-key").value.trim();
    let apiSecret = document.getElementById("admin-ak-secret").value.trim();
    let baseUrl = document.getElementById("admin-ak-url").value.trim();

    if (!apiKey || !apiSecret) { alert("API Key and Secret are required"); return; }

    if (label === "live" && !confirm("You are adding a LIVE (real money) API key. Are you sure?")) return;

    try {
        let r = await fetch("/api/admin/users/" + userId + "/api-keys", {
            method: "POST",
            headers: {"Content-Type":"application/json"},
            credentials: "same-origin",
            body: JSON.stringify({api_key: apiKey, api_secret: apiSecret, label: label, base_url: baseUrl})
        });
        let d = await r.json();
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
        let r = await fetch("/api/admin/api-keys/" + keyId, {
            method: "DELETE",
            credentials: "same-origin"
        });
        let d = await r.json();
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
        let r = await fetch("/api/admin/users", {credentials:"same-origin"});
        let d = await r.json();
        let users = d.users || d;
        let u = null;
        for (var i = 0; i < users.length; i++) { if (users[i].id === userId) { u = users[i]; break; } }
        if (!u) { alert("User not found"); return; }

        let pairs = u.trading_pairs;
        if (typeof pairs === "string") try { pairs = JSON.parse(pairs); } catch(e) { pairs = []; }

        let html = '<div style="background:rgba(15,25,45,.95);border:1px solid rgba(0,212,255,.2);border-radius:12px;padding:24px;max-width:500px;margin:20px auto">' +
            '<h3 style="color:#00d4ff;margin:0 0 16px">Edit User: ' + u.email + '</h3>' +
            '<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;font-size:13px">' +
                '<label class="text-9ba3b5">Role<select id="eu-role" class="form-input-dark"><option value="admin"'+(u.role==="admin"?" selected":"")+'>Admin</option><option value="trader"'+(u.role==="trader"?" selected":"")+'>Trader</option><option value="viewer"'+(u.role==="viewer"?" selected":"")+'>Viewer</option></select></label>' +
                '<label class="text-9ba3b5">Tier<select id="eu-tier" class="form-input-dark"><option value="free"'+(u.tier==="free"?" selected":"")+'>Free</option><option value="pro"'+(u.tier==="pro"?" selected":"")+'>Pro</option><option value="enterprise"'+(u.tier==="enterprise"?" selected":"")+'>Enterprise</option></select></label>' +
                '<label class="text-9ba3b5">Bot Mode<select id="eu-mode" class="form-input-dark"><option value="paper"'+(u.bot_mode==="paper"?" selected":"")+'>Paper</option><option value="demo"'+(u.bot_mode==="demo"?" selected":"")+'>Demo</option><option value="live"'+(u.bot_mode==="live"?" selected":"")+'>Live</option></select></label>' +
                '<label class="text-9ba3b5">Max Leverage<input id="eu-lev" type="number" value="'+(u.max_leverage||20)+'" min="1" max="50" class="form-input-dark"></label>' +
                '<label class="text-9ba3b5">Daily Loss %<input id="eu-loss" type="number" value="'+(u.max_daily_loss_pct||3)+'" min="0.5" max="20" step="0.5" class="form-input-dark"></label>' +
                '<label class="text-9ba3b5">Max Positions<input id="eu-pos" type="number" value="'+(u.max_open_positions||3)+'" min="1" max="10" class="form-input-dark"></label>' +
                '<label class="text-9ba3b5">Risk/Trade %<input id="eu-risk" type="number" value="'+(u.risk_per_trade_pct||1)+'" min="0.1" max="10" step="0.1" class="form-input-dark"></label>' +
                '<label class="text-9ba3b5">Pref Leverage<input id="eu-plev" type="number" value="'+(u.preferred_leverage||5)+'" min="1" max="50" class="form-input-dark"></label>' +
            '</div>' +
            '<label style="color:#9ba3b5;display:block;margin-top:10px">Trading Pairs (comma separated)<input id="eu-pairs" type="text" value="'+(Array.isArray(pairs)?pairs.join(", "):"")+'" class="form-input-dark"></label>' +
            '<label style="color:#9ba3b5;display:block;margin-top:10px">Telegram Chat ID<input id="eu-tg" type="text" value="'+(u.telegram_chat_id||"")+'" class="form-input-dark"></label>' +
            '<div style="display:flex;gap:10px;margin-top:16px;justify-content:flex-end">' +
                '<button onclick="document.getElementById(\'admin-edit-modal\').style.display=\'none\'" style="padding:8px 20px;border:1px solid #333;background:transparent;color:#aaa;border-radius:6px;cursor:pointer">Cancel</button>' +
                '<button onclick="adminSaveUser(\''+userId+'\')" style="padding:8px 20px;border:1px solid #00d4ff;background:rgba(0,212,255,.15);color:#00d4ff;border-radius:6px;cursor:pointer;font-weight:700">Save</button>' +
            '</div>' +
        '</div>';

        let modal = document.getElementById("admin-edit-modal");
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
    let body = {
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
    let pairsStr = document.getElementById("eu-pairs").value;
    if (pairsStr) body.trading_pairs = pairsStr.split(",").map(function(s){return s.trim();}).filter(Boolean);

    try {
        let r = await fetch("/api/admin/users/" + userId, {
            method: "PUT",
            headers: {"Content-Type":"application/json"},
            credentials: "same-origin",
            body: JSON.stringify(body)
        });
        let d = await r.json();
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
    let newPass = prompt("Enter new password for " + email + "\n(minimum 8 characters):");
    if (!newPass) return;
    if (newPass.length < 8) { alert("Password must be at least 8 characters"); return; }
    if (!confirm("Reset password for " + email + "?\nThey will be logged out of all sessions.")) return;
    try {
        let r = await fetch("/api/admin/users/" + userId + "/reset-password", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            credentials: "same-origin",
            body: JSON.stringify({new_password: newPass})
        });
        let d = await r.json();
        if (d.ok) {
            alert("Password reset for " + email + ". They need to login again.");
            refreshAdmin();
        } else {
            alert("Error: " + (d.error || "unknown"));
        }
    } catch(e) { alert("Failed: " + e); }
}

