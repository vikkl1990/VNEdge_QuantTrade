/**
 * CryptoAlgoBot Dashboard - Two-Section (Investment + Scalp)
 *
 * Polls /api/* endpoints and splits signals into scalp vs investment sections.
 */

(function () {
    "use strict";

    let REFRESH_MS = 5000;
    let intervalId = null;
    let isPaused = false;
    let apiFailCount = 0;

    // Symbol filter state
    const symbolFilters = { scalp: "all", tracked: "all", closed: "all" };

    // Cache raw data for re-filtering without API call
    let cachedScalpSignals = [];
    let cachedTrackedSignals = [];
    let cachedClosedSignals = [];

    const $ = (sel) => document.querySelector(sel);

    // ── Helpers ───────────────────────────────────────────────────────

    function formatPrice(price) {
        if (price == null) return "--";
        const n = Number(price);
        if (n >= 1000) return n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        if (n >= 1) return n.toFixed(4);
        return n.toPrecision(4);
    }

    function formatPnl(value) {
        if (value == null) return "$0.00";
        const n = Number(value);
        return (n >= 0 ? "+" : "") + "$" + Math.abs(n).toFixed(2);
    }

    function pnlClass(value) {
        const n = Number(value);
        if (n > 0) return "pnl-positive";
        if (n < 0) return "pnl-negative";
        return "pnl-zero";
    }

    function formatPct(value) {
        if (value == null) return "0%";
        return Number(value).toFixed(2) + "%";
    }

    function formatTime(isoStr) {
        if (!isoStr) return "--";
        try { return new Date(isoStr).toLocaleTimeString("en-IN", { hour12: false, timeZone: "Asia/Kolkata" }); }
        catch (e) { return isoStr; }
    }

    function formatDateTime(isoStr) {
        if (!isoStr) return "--";
        try {
            return new Date(isoStr).toLocaleString("en-IN", {
                month: "short", day: "numeric",
                hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
                timeZone: "Asia/Kolkata",
            }) + " IST";
        } catch (e) { return isoStr; }
    }

    function escapeHtml(str) {
        const el = document.createElement("span");
        el.textContent = str;
        return el.innerHTML;
    }

    function gradeClass(grade) {
        if (!grade) return "grade-c";
        const g = grade.toUpperCase();
        if (g === "A+") return "grade-a-plus";
        if (g === "A") return "grade-a";
        if (g === "B") return "grade-b";
        if (g === "C") return "grade-c";
        return "grade-reject";
    }

    function confidenceColor(pct) {
        pct = Number(pct) || 0;
        if (pct >= 75) return "var(--accent-buy)";
        if (pct >= 50) return "var(--accent-info)";
        if (pct >= 30) return "var(--accent-warn)";
        return "var(--accent-sell)";
    }

    function signalTypeClass(type) {
        if (!type) return "";
        return type.toLowerCase().replace("+", "plus");
    }

    // ── Fetch wrapper ─────────────────────────────────────────────────

    async function api(path) {
        try {
            const resp = await fetch(path);
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            apiFailCount = 0;
            updateConnStatus(true);
            return await resp.json();
        } catch (err) {
            apiFailCount++;
            if (apiFailCount > 2) updateConnStatus(false);
            console.warn(`API ${path} failed:`, err.message);
            return null;
        }
    }

    function updateConnStatus(connected) {
        const el = document.getElementById("conn-status");
        const label = document.getElementById("conn-label");
        if (!el) return;
        if (connected) {
            el.classList.remove("disconnected");
            label.textContent = "LIVE";
        } else {
            el.classList.add("disconnected");
            label.textContent = "OFFLINE";
        }
    }

    // ── Update: Status + Prices ───────────────────────────────────────

    function updateStatus(data) {
        if (!data) return;

        $("#bot-name").textContent = data.bot_name || "CryptoAlgoBot";
        $("#bot-version").textContent = "v" + (data.bot_version || "1.0.0");

        // Status dot
        const dot = $("#status-dot"), text = $("#status-text");
        dot.className = "dot";
        if (data.bot_status === "running") { dot.classList.add("green"); text.textContent = "running"; }
        else if (data.bot_status === "paused") { dot.classList.add("yellow"); text.textContent = "paused"; }
        else { dot.classList.add("red"); text.textContent = data.bot_status || "offline"; }

        // Exchange dot
        const eDot = $("#exchange-dot"), eText = $("#exchange-text");
        eDot.className = "dot";
        if (data.exchange_status === "connected") { eDot.classList.add("green"); eText.textContent = "exchange connected"; }
        else { eDot.classList.add("red"); eText.textContent = data.exchange_status || "disconnected"; }

        // Mode badge
        const mb = $("#mode-badge");
        mb.textContent = (data.mode || "paper").toUpperCase();
        mb.className = "mode-badge";
        if (data.mode === "live") mb.classList.add("live");

        // Pause button
        isPaused = !!data.paused;
        const btn = $("#pause-btn");
        if (isPaused) { btn.textContent = "Resume"; btn.classList.add("active"); }
        else { btn.textContent = "Pause"; btn.classList.remove("active"); }

        if (data.refresh_interval) {
            REFRESH_MS = data.refresh_interval * 1000;
            $("#footer-refresh").textContent = "Auto-refresh: " + data.refresh_interval + "s";
        }

        $("#server-time").textContent = formatTime(data.server_time);

        // Bot status card
        $("#st-mode").textContent = (data.mode || "--").replace(/_/g, " ");
        $("#st-uptime").textContent = data.uptime || "--";
        $("#st-strategy").textContent = (data.active_strategy || "--").replace(/_/g, " ");
        $("#st-symbols").textContent = (data.symbols || []).join(", ") || "--";

        // System health
        const shLatency = $("#sh-latency");
        if (shLatency) shLatency.textContent = (data.exchange_latency_ms != null ? data.exchange_latency_ms.toFixed(0) + " ms" : "-- ms");
        const shLastUpdate = $("#sh-last-update");
        if (shLastUpdate) shLastUpdate.textContent = data.last_data_update || "--";
        const shMemory = $("#sh-memory");
        if (shMemory) shMemory.textContent = (data.memory_mb != null ? data.memory_mb.toFixed(1) + " MB" : "-- MB");

        // Fee rates
        if (data.fees) {
            const tf = $("#st-taker-fee");
            const mf = $("#st-maker-fee");
            const sf = $("#st-settlement-fee");
            if (tf) tf.textContent = (data.fees.taker * 100).toFixed(2) + "%";
            if (mf) mf.textContent = (data.fees.maker * 100).toFixed(2) + "%";
            if (sf) sf.textContent = (data.fees.settlement * 100).toFixed(2) + "%";
        }
        const shExchange = $("#sh-exchange");
        if (shExchange) shExchange.textContent = data.exchange_status || "--";

        // Prices
        updatePrices(data.prices || {});
    }

    function updatePrices(prices) {
        const container = $("#prices-list");
        const symbols = Object.keys(prices);
        if (symbols.length === 0) {
            container.innerHTML = '<p class="muted">Waiting for data...</p>';
            return;
        }
        let html = "";
        for (const sym of symbols) {
            html += `<div class="price-row">
                <span class="price-symbol">${escapeHtml(sym)}</span>
                <span class="price-value mono">${formatPrice(prices[sym])}</span>
            </div>`;
        }
        container.innerHTML = html;
    }

    // ── Update: Two-section signals ───────────────────────────────────

    function updateSignals(signals) {
        if (!signals) signals = [];

        // Split signals by strategy_type metadata
        const scalpSignals = [];
        const investSignals = [];

        for (const s of signals) {
            const meta = s.metadata || {};
            const stratType = meta.strategy_type || s.strategy_type || "";
            if (stratType === "scalp") {
                scalpSignals.push(s);
            } else if (stratType === "investment") {
                investSignals.push(s);
            } else {
                // Guess from signal properties
                const setup = meta.setup_type || "";
                if (setup) scalpSignals.push(s);
                else investSignals.push(s);
            }
        }

        cachedScalpSignals = scalpSignals;
        updateScalpSection(scalpSignals);
        updateInvestSection(investSignals);
    }

    function calcLeverage(confidence, entryPrice, stopLoss) {
        // Fixed Fractional Risk Model (Phase 2)
        // Risk 0.75% of $1000 account = $7.50 per trade
        // Position size = risk / SL_distance, leverage = position / stake
        const conf = Number(confidence || 0);
        const entry = Number(entryPrice || 0);
        const sl = Number(stopLoss || 0);
        const slDistPct = entry > 0 ? Math.abs(entry - sl) / entry * 100 : 1.0;

        const ACCOUNT = 1000.0;
        const RISK_PCT = 0.75;
        const riskAmount = ACCOUNT * RISK_PCT / 100; // $7.50
        const stake = 25.0;

        // Max leverage caps by grade
        let maxLev = 3;
        if (conf >= 90) maxLev = 8;
        else if (conf >= 80) maxLev = 6;
        else if (conf >= 65) maxLev = 4;

        // Derive leverage from risk model
        const positionUsd = slDistPct > 0 ? riskAmount / (slDistPct / 100) : riskAmount * 100;
        let lev = Math.min(Math.floor(positionUsd / stake), maxLev);
        lev = Math.max(1, lev);

        return lev;
    }

    function calcStake(confidence) {
        // Fixed stake $25 base (leverage adjusts via risk model)
        return 25;
    }

    // Delta India contract specs
    const CONTRACT_SPECS = {
        "BTC": { size: 0.001, unit: "BTC" },
        "ETH": { size: 0.01, unit: "ETH" },
    };

    function calcContracts(symbol, positionUsd, entryPrice) {
        const sym = (symbol || "").toUpperCase();
        const spec = sym.includes("BTC") ? CONTRACT_SPECS.BTC : CONTRACT_SPECS.ETH;
        if (!entryPrice || entryPrice <= 0) return { contracts: 0, qty: 0, unit: spec.unit, contractSize: spec.size };
        const rawContracts = positionUsd / (entryPrice * spec.size);
        const contracts = Math.max(1, Math.floor(rawContracts));
        const qty = contracts * spec.size;
        return { contracts, qty, unit: spec.unit, contractSize: spec.size };
    }

    function formatQty(symbol, contracts, qty) {
        const sym = (symbol || "").toUpperCase();
        const unit = sym.includes("BTC") ? "BTC" : "ETH";
        return contracts + " ct (" + qty.toFixed(sym.includes("BTC") ? 3 : 2) + " " + unit + ")";
    }

    function updateScalpSection(signals) {
        cachedScalpSignals = signals;
        const body = $("#scalp-signals-body");
        const countEl = $("#scalp-count");
        const filter = symbolFilters.scalp;

        const filtered = signals.filter(s => matchesSymbolFilter(s.symbol, filter));

        if (countEl) {
            const label = filter === "all" ? "" : " " + filter;
            countEl.textContent = filtered.length + label + " signal" + (filtered.length !== 1 ? "s" : "");
        }

        if (filtered.length === 0) {
            body.innerHTML = '<tr><td colspan="14" class="muted">Scanning for scalp setups on 1m/5m...</td></tr>';
            return;
        }

        let html = "";
        for (const s of filtered.slice(0, 20)) {
            const side = (s.side || "long").toLowerCase();
            const sideClass = side === "long" ? "side-long" : "side-short";
            const grade = s.grade || "--";
            const gClass = gradeClass(grade);
            const conf = Number(s.confidence || 0);
            const cColor = confidenceColor(conf);
            const meta = s.metadata || {};
            const setup = meta.setup_type || "unknown";
            const tps = s.take_profits || [];
            const symClass = symbolColorClass(s.symbol);

            // Calculate leverage and position size
            const lev = calcLeverage(conf, s.entry_price, s.stop_loss);
            const stake = calcStake(conf);
            const posSize = stake * lev;
            const ct = calcContracts(s.symbol, posSize, s.entry_price);

            html += `<tr>
                <td>${formatTime(s.timestamp)}</td>
                <td><strong class="${symClass}">${escapeHtml(s.symbol || "--")}</strong></td>
                <td><span class="setup-badge">${escapeHtml(setup.replace(/_/g, " "))}</span></td>
                <td><span class="${sideClass}">${side.toUpperCase()}</span></td>
                <td><span class="leverage-badge">${lev}x</span></td>
                <td class="mono position-size">$${(ct.qty * s.entry_price).toFixed(0)}</td>
                <td class="mono qty-cell" title="${ct.qty.toFixed(ct.unit==='BTC'?3:2)} ${ct.unit}">${ct.contracts} ct</td>
                <td class="mono">${formatPrice(s.entry_price)}</td>
                <td class="sl-price">${formatPrice(s.stop_loss)}</td>
                <td class="tp-price">${formatPrice(tps[0])}</td>
                <td class="tp-price">${formatPrice(tps[1])}</td>
                <td class="tp-price">${formatPrice(tps[2])}</td>
                <td>
                    <span class="confidence-bar">
                        <span class="bar"><span class="bar-fill" style="width:${conf}%;background:${cColor}"></span></span>
                        ${conf}%${meta.original_confidence && meta.original_confidence !== conf ? ` <span class="ai-adj" style="font-size:0.65rem;color:var(--accent-purple)">(${meta.original_confidence}&rarr;${conf})</span>` : ""}
                    </span>
                </td>
                <td><span class="grade ${gClass}">${escapeHtml(grade)}</span></td>
            </tr>`;
        }
        body.innerHTML = html;
    }

    function updateInvestSection(signals) {
        const body = $("#invest-signals-body");
        const countEl = $("#invest-count");

        if (countEl) countEl.textContent = signals.length + " signal" + (signals.length !== 1 ? "s" : "");

        if (signals.length === 0) {
            body.innerHTML = '<tr><td colspan="12" class="muted">Waiting for swing setups on 5m/15m...</td></tr>';
            return;
        }

        let html = "";
        for (const s of signals.slice(0, 20)) {
            const side = (s.side || "long").toLowerCase();
            const sideClass = side === "long" ? "side-long" : "side-short";
            const grade = s.grade || "--";
            const gClass = gradeClass(grade);
            const conf = Number(s.confidence || 0);
            const cColor = confidenceColor(conf);
            const tps = s.take_profits || [];
            const typeClass = signalTypeClass(s.signal_type || s.type || "");

            html += `<tr>
                <td>${formatTime(s.timestamp)}</td>
                <td><strong>${escapeHtml(s.symbol || "--")}</strong></td>
                <td><span class="signal-type ${typeClass}">${escapeHtml((s.signal_type || s.type || "--").toUpperCase())}</span></td>
                <td><span class="${sideClass}">${side.toUpperCase()}</span></td>
                <td class="mono">${formatPrice(s.entry_price)}</td>
                <td class="sl-price">${formatPrice(s.stop_loss)}</td>
                <td class="tp-price">${formatPrice(tps[0])}</td>
                <td class="tp-price">${formatPrice(tps[1])}</td>
                <td>
                    <span class="confidence-bar">
                        <span class="bar"><span class="bar-fill" style="width:${conf}%;background:${cColor}"></span></span>
                        ${conf}%
                    </span>
                </td>
                <td><span class="grade ${gClass}">${escapeHtml(grade)}</span></td>
                <td>${escapeHtml(s.regime || "--")}</td>
                <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis">${escapeHtml(s.reason || "--")}</td>
            </tr>`;
        }
        body.innerHTML = html;
    }

    // ── Update: Positions, Performance, Alerts, Trades ────────────────

    function updatePositions(positions) {
        const body = $("#positions-body");
        if (!positions || positions.length === 0) {
            body.innerHTML = '<tr><td colspan="9" class="muted">No open positions</td></tr>';
            return;
        }
        let html = "";
        for (const p of positions) {
            const side = (p.side || "long").toLowerCase();
            const sideClass = side === "long" ? "side-long" : "side-short";
            const pnl = Number(p.pnl || p.unrealized_pnl || 0);
            const pnlPct = Number(p.pnl_pct || p.unrealized_pnl_pct || 0);
            html += `<tr>
                <td>${escapeHtml(p.symbol || "--")}</td>
                <td><span class="${sideClass}">${side}</span></td>
                <td class="mono">${formatPrice(p.entry_price || p.entry)}</td>
                <td class="mono">${formatPrice(p.current_price || p.current)}</td>
                <td class="mono">${p.size != null ? Number(p.size).toFixed(4) : "--"}</td>
                <td class="${pnlClass(pnl)}">${formatPnl(pnl)}</td>
                <td class="${pnlClass(pnlPct)}">${formatPct(pnlPct)}</td>
                <td class="mono">${formatPrice(p.stop_loss || p.sl)}</td>
                <td class="mono">${formatPrice(p.take_profit || p.tp)}</td>
            </tr>`;
        }
        body.innerHTML = html;
    }

    function updatePerformance(perf) {
        if (!perf) return;
        // This now receives data from /api/tracker/stats
    }

    function updateTrackerStats(stats) {
        if (!stats) return;
        const set = (id, val) => { const n = $(id); if (n) n.textContent = val; };

        const wr = stats.win_rate || 0;
        const wrEl = $("#pf-winrate");
        if (wrEl) {
            wrEl.textContent = wr.toFixed(1) + "%";
            wrEl.style.color = wr >= 60 ? "var(--accent-buy)" : wr >= 40 ? "var(--accent-warn)" : "var(--accent-sell)";
        }

        const tpnl = Number(stats.total_pnl || 0);
        const tpnlEl = $("#pf-total-pnl");
        if (tpnlEl) {
            tpnlEl.textContent = (tpnl >= 0 ? "+" : "") + tpnl.toFixed(3) + "%";
            tpnlEl.className = "value mono " + pnlClass(tpnl);
        }

        set("#pf-trades", String(stats.closed || 0));
        set("#pf-wl", (stats.wins || 0) + " / " + (stats.losses || 0));
        set("#pf-active", String(stats.active || 0));

        const pf = stats.profit_factor;
        const pfEl = $("#pf-profit-factor");
        if (pfEl) {
            pfEl.textContent = pf === Infinity || pf > 999 ? "INF" : (pf || 0).toFixed(2);
            pfEl.style.color = pf >= 1.5 ? "var(--accent-buy)" : pf >= 1 ? "var(--accent-warn)" : "var(--accent-sell)";
        }

        set("#pf-tp-rates",
            (stats.tp1_rate || 0).toFixed(0) + "% / " +
            (stats.tp2_rate || 0).toFixed(0) + "% / " +
            (stats.tp3_rate || 0).toFixed(0) + "%");

        // Paper balance
        const bal = stats.paper_balance || 1000;
        const balEl = $("#pf-balance");
        if (balEl) {
            balEl.textContent = "$" + bal.toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
            balEl.style.color = bal >= 1000 ? "var(--accent-buy)" : bal >= 900 ? "var(--accent-warn)" : "var(--accent-sell)";
        }

        const pnlUsd = stats.paper_pnl_usd || 0;
        const pnlUsdEl = $("#pf-pnl-usd");
        if (pnlUsdEl) {
            pnlUsdEl.textContent = (pnlUsd >= 0 ? "+$" : "-$") + Math.abs(pnlUsd).toFixed(2);
            pnlUsdEl.className = "value mono " + pnlClass(pnlUsd);
        }

        const bestEl = $("#pf-best");
        if (bestEl) bestEl.textContent = stats.best_trade ? "+" + stats.best_trade.toFixed(3) + "%" : "--";
        const worstEl = $("#pf-worst");
        if (worstEl) worstEl.textContent = stats.worst_trade ? stats.worst_trade.toFixed(3) + "%" : "--";
    }

    // ── Update: Tracked Active Signals ───────────────────────────────

    let currentPrices = {};

    function updateTrackedSignals(tracked) {
        if (tracked) cachedTrackedSignals = tracked;
        tracked = cachedTrackedSignals;
        const body = $("#tracked-body");
        const countEl = $("#tracked-count");
        const filter = symbolFilters.tracked;

        const filtered = (tracked || []).filter(t => matchesSymbolFilter(t.symbol, filter));

        if (!filtered || filtered.length === 0) {
            if (body) body.innerHTML = '<tr><td colspan="14" class="muted">No signals being tracked yet</td></tr>';
            if (countEl) countEl.textContent = "0 active";
            return;
        }
        if (countEl) {
            const label = filter === "all" ? "" : " " + filter;
            countEl.textContent = filtered.length + label + " active";
        }

        let html = "";
        for (const t of filtered) {
            const side = (t.side || "long").toLowerCase();
            const sideClass = side === "long" ? "side-long" : "side-short";
            const curPrice = currentPrices[t.symbol] || 0;
            const isLong = side === "long";
            const unrealPnl = t.entry_price ? ((isLong ? 1 : -1) * (curPrice - t.entry_price) / t.entry_price * 100) : 0;
            const pnlCls = unrealPnl > 0 ? "pnl-positive" : unrealPnl < 0 ? "pnl-negative" : "pnl-zero";
            const symClass = symbolColorClass(t.symbol);

            // Status badge
            let statusBadge = '<span class="status-active">ACTIVE</span>';
            if (t.tp1_hit && !t.tp2_hit) statusBadge = '<span class="status-tp1">TP1 HIT</span>';
            if (t.tp2_hit && !t.tp3_hit) statusBadge = '<span class="status-tp2">TP2 HIT</span>';

            // TP cell styling — green check if hit
            const tp1Cell = t.tp1_hit ? `<td class="tp-hit">${formatPrice(t.tp1)}</td>` : `<td class="tp-price">${formatPrice(t.tp1)}</td>`;
            const tp2Cell = t.tp2_hit ? `<td class="tp-hit">${formatPrice(t.tp2)}</td>` : `<td class="tp-price">${formatPrice(t.tp2)}</td>`;
            const tp3Cell = t.tp3_hit ? `<td class="tp-hit">${formatPrice(t.tp3)}</td>` : `<td class="tp-price">${formatPrice(t.tp3)}</td>`;

            // Dollar PnL with leverage
            const lev = t.leverage || 1;
            const posSize = t.position_size_usd || (25 * lev);
            const dollarPnl = posSize * unrealPnl / 100;
            const dollarCls = dollarPnl > 0 ? "pnl-positive" : dollarPnl < 0 ? "pnl-negative" : "pnl-zero";

            // Contract quantity (from API data or calculated)
            const tContracts = t.contracts || 0;
            const tQty = t.quantity || 0;
            let qtyDisplay = "--";
            if (tContracts > 0) {
                const u = (t.symbol || "").toUpperCase().includes("BTC") ? "BTC" : "ETH";
                const dec = u === "BTC" ? 3 : 2;
                qtyDisplay = tContracts + " ct (" + tQty.toFixed(dec) + " " + u + ")";
            } else if (posSize > 0 && t.entry_price > 0) {
                const ct2 = calcContracts(t.symbol, posSize, t.entry_price);
                qtyDisplay = ct2.contracts + " ct";
            }

            html += `<tr>
                <td>${formatTime(t.entry_time)}</td>
                <td><strong class="${symClass}">${escapeHtml(t.symbol || "--")}</strong></td>
                <td><span class="${sideClass}">${side.toUpperCase()}</span></td>
                <td><span class="leverage-badge">${lev}x</span></td>
                <td class="mono qty-cell">${qtyDisplay}</td>
                <td class="mono">${formatPrice(t.entry_price)}</td>
                <td class="mono">${curPrice ? formatPrice(curPrice) : "--"}</td>
                <td class="sl-price">${formatPrice(t.stop_loss)}</td>
                ${tp1Cell}
                ${tp2Cell}
                ${tp3Cell}
                <td>${statusBadge}</td>
                <td class="${pnlCls} mono">${unrealPnl >= 0 ? "+" : ""}${unrealPnl.toFixed(3)}%</td>
                <td class="${dollarCls} mono">${dollarPnl >= 0 ? "+$" : "-$"}${Math.abs(dollarPnl).toFixed(2)}</td>
            </tr>`;
        }
        body.innerHTML = html;
    }

    // ── Update: Closed Signal Results ────────────────────────────────

    function updateClosedSignals(closed) {
        if (closed) cachedClosedSignals = closed;
        closed = cachedClosedSignals;
        const body = $("#closed-body");
        const countEl = $("#closed-count");
        const statsEl = $("#closed-symbol-stats");
        const filter = symbolFilters.closed;

        if (!closed || closed.length === 0) {
            if (body) body.innerHTML = '<tr><td colspan="15" class="muted">No closed signals yet</td></tr>';
            if (countEl) countEl.textContent = "0 closed";
            if (statsEl) statsEl.innerHTML = "";
            return;
        }

        // Build per-symbol stats from ALL closed signals
        if (statsEl) {
            const symStats = {};
            for (const c of closed) {
                const sym = (c.symbol || "").toUpperCase().includes("BTC") ? "BTC" :
                            (c.symbol || "").toUpperCase().includes("ETH") ? "ETH" : "OTHER";
                if (!symStats[sym]) symStats[sym] = { wins: 0, losses: 0, totalPnl: 0, totalUsd: 0 };
                const pnl = Number(c.pnl_pct || 0);
                const usd = Number(c.pnl_usd || 0);
                if (pnl > 0) symStats[sym].wins++;
                else symStats[sym].losses++;
                symStats[sym].totalPnl += pnl;
                symStats[sym].totalUsd += usd;
            }
            let statsHtml = "";
            for (const [sym, st] of Object.entries(symStats)) {
                const total = st.wins + st.losses;
                const wr = total > 0 ? (st.wins / total * 100).toFixed(1) : "0";
                const pnlCls = st.totalUsd >= 0 ? "pnl-positive" : "pnl-negative";
                const wrColor = Number(wr) >= 60 ? "color:var(--accent-buy)" : Number(wr) >= 40 ? "color:var(--accent-warn)" : "color:var(--accent-sell)";
                statsHtml += `<div class="symbol-stat-group">
                    <span class="sym-icon ${sym.toLowerCase()}">${sym}</span>
                    <div class="sym-stat">
                        <span class="sym-stat-label">Trades</span>
                        <span class="sym-stat-value">${total}</span>
                    </div>
                    <div class="sym-stat">
                        <span class="sym-stat-label">WR</span>
                        <span class="sym-stat-value" style="${wrColor}">${wr}%</span>
                    </div>
                    <div class="sym-stat">
                        <span class="sym-stat-label">W/L</span>
                        <span class="sym-stat-value">${st.wins}/${st.losses}</span>
                    </div>
                    <div class="sym-stat">
                        <span class="sym-stat-label">P&L</span>
                        <span class="sym-stat-value ${pnlCls}">${st.totalUsd >= 0 ? "+$" : "-$"}${Math.abs(st.totalUsd).toFixed(2)}</span>
                    </div>
                </div>`;
            }
            statsEl.innerHTML = statsHtml;
        }

        // Apply filter
        const filtered = closed.filter(c => matchesSymbolFilter(c.symbol, filter));
        if (countEl) {
            const label = filter === "all" ? "" : " " + filter;
            countEl.textContent = filtered.length + label + " closed";
        }

        if (filtered.length === 0) {
            body.innerHTML = `<tr><td colspan="15" class="muted">No ${filter} closed signals</td></tr>`;
            return;
        }

        let html = "";
        // Show newest first
        const sorted = [...filtered].reverse();
        for (const c of sorted.slice(0, 50)) {
            const side = (c.side || "long").toLowerCase();
            const sideClass = side === "long" ? "side-long" : "side-short";
            const pnl = Number(c.pnl_pct || 0);
            const pnlCls = pnl > 0 ? "pnl-positive" : pnl < 0 ? "pnl-negative" : "pnl-zero";
            const symClass = symbolColorClass(c.symbol);

            // Leverage & dollar PnL
            const lev = c.leverage || 1;
            const stake = c.paper_stake || 25;
            const dollarPnl = Number(c.pnl_usd || 0);
            const dollarCls = dollarPnl > 0 ? "pnl-positive" : dollarPnl < 0 ? "pnl-negative" : "pnl-zero";

            // Gross and fees
            const grossPct = Number(c.gross_pnl_pct || 0);
            const grossCls = grossPct > 0 ? "pnl-positive" : grossPct < 0 ? "pnl-negative" : "pnl-zero";
            const feesUsd = Number(c.total_fees_usd || 0);

            // Result badge
            let result = c.status || "closed";
            let resultClass = "status-active";
            if (result === "stopped") resultClass = "status-sl";
            else if (result === "partial_win") resultClass = "status-tp1";
            else if (result === "tp3_hit") resultClass = "status-tp3";
            else if (result === "tp2_hit") resultClass = "status-tp2";
            else if (result === "tp1_hit") resultClass = "status-tp1";
            else if (result === "expired") resultClass = "status-expired";
            const resultLabel = result.replace(/_/g, " ").toUpperCase();

            // Duration
            let duration = "--";
            if (c.entry_time && c.exit_time) {
                try {
                    const ms = new Date(c.exit_time) - new Date(c.entry_time);
                    const mins = Math.floor(ms / 60000);
                    if (mins < 60) duration = mins + "m";
                    else duration = Math.floor(mins / 60) + "h " + (mins % 60) + "m";
                } catch (e) {}
            }

            html += `<tr>
                <td>${formatTime(c.entry_time)}</td>
                <td>${formatTime(c.exit_time)}</td>
                <td><strong class="${symClass}">${escapeHtml(c.symbol || "--")}</strong></td>
                <td><span class="${sideClass}">${side.toUpperCase()}</span></td>
                <td><span class="setup-badge">${escapeHtml((c.setup_type || "?").replace(/_/g, " "))}</span></td>
                <td><span class="leverage-badge">${lev}x</span></td>
                <td class="mono">$${stake}</td>
                <td class="mono">${formatPrice(c.entry_price)}</td>
                <td class="mono">${formatPrice(c.exit_price)}</td>
                <td><span class="${resultClass}">${resultLabel}</span></td>
                <td class="${grossCls} mono">${grossPct >= 0 ? "+" : ""}${grossPct.toFixed(3)}%</td>
                <td class="${pnlCls} mono">${pnl >= 0 ? "+" : ""}${pnl.toFixed(3)}%</td>
                <td class="pnl-negative mono">-$${Math.abs(feesUsd).toFixed(2)}</td>
                <td class="${dollarCls} mono">${dollarPnl >= 0 ? "+$" : "-$"}${Math.abs(dollarPnl).toFixed(2)}</td>
                <td>${duration}</td>
            </tr>`;
        }
        body.innerHTML = html;
    }

    // ── Update: AI Learning Insights ──────────────────────────────────

    function updateAIInsights(data) {
        if (!data) return;

        // Status tag
        const tag = $("#ai-status-tag");
        if (tag) {
            tag.className = "section-tag ai-tag";
            if (data.learning_active) {
                tag.textContent = "ACTIVE";
                tag.classList.add("active");
            } else {
                tag.textContent = "COLLECTING";
                tag.classList.add("inactive");
            }
        }

        // Counters
        const evalEl = $("#ai-evaluated");
        if (evalEl) evalEl.textContent = (data.total_evaluated || 0) + " evaluated";
        const adjEl = $("#ai-adjustments");
        if (adjEl) adjEl.textContent = (data.total_adjustments || 0) + " adjustments";

        // Setup rankings table
        const setupBody = $("#ai-setup-body");
        if (setupBody) {
            const rankings = data.setup_rankings || [];
            if (rankings.length === 0) {
                setupBody.innerHTML = '<tr><td colspan="6" class="muted">Collecting data (need 3+ trades per setup)...</td></tr>';
            } else {
                let html = "";
                for (const s of rankings) {
                    const wr = Number(s.win_rate || 0);
                    const avgPnl = Number(s.avg_pnl || 0);
                    const mult = Number(s.confidence_mult || 1);

                    // WR bar color
                    const wrColor = wr >= 60 ? "var(--accent-buy)" : wr >= 40 ? "var(--accent-warn)" : "var(--accent-sell)";

                    // Multiplier class
                    let multClass = "neutral";
                    if (mult >= 1.1) multClass = "boost";
                    else if (mult <= 0.9) multClass = "penalize";

                    // Status
                    let statusClass = "normal";
                    let statusText = "NORMAL";
                    if (mult >= 1.2) { statusClass = "boosted"; statusText = "BOOSTED"; }
                    else if (mult >= 1.05) { statusClass = "boosted"; statusText = "FAVORED"; }
                    else if (mult <= 0.7) { statusClass = "blocked"; statusText = "BLOCKED"; }
                    else if (mult <= 0.85) { statusClass = "penalized"; statusText = "PENALIZED"; }

                    const pnlCls = avgPnl > 0 ? "pnl-positive" : avgPnl < 0 ? "pnl-negative" : "pnl-zero";

                    html += `<tr>
                        <td><span class="setup-badge">${escapeHtml((s.setup || "?").replace(/_/g, " "))}</span></td>
                        <td>
                            <span class="ai-wr-bar">
                                <span class="bar-track"><span class="bar-fill" style="width:${wr}%;background:${wrColor}"></span></span>
                                ${wr.toFixed(1)}%
                            </span>
                        </td>
                        <td class="${pnlCls} mono">${avgPnl >= 0 ? "+" : ""}${avgPnl.toFixed(3)}%</td>
                        <td>${s.total || 0}</td>
                        <td><span class="ai-mult ${multClass}">${mult.toFixed(2)}x</span></td>
                        <td><span class="ai-setup-status ${statusClass}">${statusText}</span></td>
                    </tr>`;
                }
                setupBody.innerHTML = html;
            }
        }

        // Best conditions
        const bestConds = $("#ai-best-conditions");
        if (bestConds) {
            const best = data.best_conditions || [];
            if (best.length === 0) {
                bestConds.innerHTML = '<span class="muted" style="font-size:0.8rem">Need more data...</span>';
            } else {
                let html = "";
                for (const c of best) {
                    html += `<span class="ai-cond-pill good">
                        ${escapeHtml((c.condition || "?").replace(/_/g, " "))}
                        <span class="ai-cond-wr">${(c.win_rate || 0).toFixed(0)}% (${c.total})</span>
                    </span>`;
                }
                bestConds.innerHTML = html;
            }
        }

        // Worst conditions
        const worstConds = $("#ai-worst-conditions");
        if (worstConds) {
            const worst = data.worst_conditions || [];
            if (worst.length === 0) {
                worstConds.innerHTML = '<span class="muted" style="font-size:0.8rem">Need more data...</span>';
            } else {
                let html = "";
                for (const c of worst) {
                    html += `<span class="ai-cond-pill bad">
                        ${escapeHtml((c.condition || "?").replace(/_/g, " "))}
                        <span class="ai-cond-wr">${(c.win_rate || 0).toFixed(0)}% (${c.total})</span>
                    </span>`;
                }
                worstConds.innerHTML = html;
            }
        }

        // Blocked patterns
        const blockedList = $("#ai-blocked-list");
        if (blockedList) {
            const blocked = data.blocked_combos || [];
            if (blocked.length === 0) {
                blockedList.innerHTML = '<span class="muted" style="font-size:0.8rem">No blocked patterns</span>';
            } else {
                let html = "";
                for (const b of blocked) {
                    html += `<span class="ai-blocked-pill">${escapeHtml(String(b).replace(/_/g, " "))}</span>`;
                }
                blockedList.innerHTML = html;
            }
        }

        // Last retrain
        const retrainEl = $("#ai-last-retrain");
        if (retrainEl) {
            retrainEl.textContent = data.last_retrain ? formatDateTime(data.last_retrain) : "Not yet";
        }
    }

    function updateAlerts(alerts) {
        const container = $("#alerts-list");
        if (!alerts || alerts.length === 0) {
            container.innerHTML = '<p class="muted">No alerts yet</p>';
            return;
        }
        let html = "";
        for (const a of alerts.slice(0, 50)) {
            const level = (a.level || "info").toLowerCase();
            html += `<div class="alert-row ${escapeHtml(level)}">
                <span class="alert-time">${formatTime(a.timestamp)}</span>
                <span class="alert-msg">${escapeHtml(a.message || "")}</span>
                <span class="alert-source">${escapeHtml(a.source || "")}</span>
            </div>`;
        }
        container.innerHTML = html;
    }

    function updateTrades(trades) {
        const body = $("#trades-body");
        if (!trades || trades.length === 0) {
            body.innerHTML = '<tr><td colspan="9" class="muted">No trades recorded</td></tr>';
            return;
        }
        let html = "";
        for (const t of trades.slice(0, 50)) {
            const side = (t.side || "long").toLowerCase();
            const sideClass = side === "long" ? "side-long" : "side-short";
            const pnl = Number(t.pnl || t.realized_pnl || 0);
            const pnlPct = Number(t.pnl_pct || t.realized_pnl_pct || 0);
            const sType = t.strategy_type || (t.metadata || {}).strategy_type || "";
            html += `<tr>
                <td>${formatDateTime(t.closed_at || t.exit_time || t.timestamp)}</td>
                <td>${escapeHtml(t.symbol || "--")}</td>
                <td>${escapeHtml(sType || "--")}</td>
                <td><span class="${sideClass}">${side}</span></td>
                <td class="mono">${formatPrice(t.entry_price || t.entry)}</td>
                <td class="mono">${formatPrice(t.exit_price || t.exit)}</td>
                <td class="${pnlClass(pnl)}">${formatPnl(pnl)}</td>
                <td class="${pnlClass(pnlPct)}">${formatPct(pnlPct)}</td>
                <td>${escapeHtml(t.duration || "--")}</td>
            </tr>`;
        }
        body.innerHTML = html;
    }

    // ── Update: P&L Monitor Agent ────────────────────────────────────

    function updateMonitorReport(data) {
        if (!data || !data.active) return;

        // Status tag
        const tag = $("#monitor-status-tag");
        if (tag) {
            tag.textContent = data.total_analyzed > 0 ? "ACTIVE" : "WAITING";
        }

        // Header stats
        const totalEl = $("#monitor-total");
        if (totalEl) totalEl.textContent = (data.total_analyzed || 0) + " analyzed";
        const streakEl = $("#monitor-streak");
        if (streakEl) {
            const s = data.current_streak || 0;
            streakEl.textContent = "streak: " + (s > 0 ? "+" : "") + s;
            streakEl.style.color = s >= 3 ? "var(--green)" : s <= -3 ? "var(--red)" : "";
        }

        // Metric cards
        const rwEl = $("#mon-rolling-wr");
        if (rwEl) {
            const rw = data.rolling_20_wr || 0;
            rwEl.textContent = rw.toFixed(1) + "%";
            rwEl.className = "monitor-metric-value " + (rw >= 55 ? "positive" : rw < 45 ? "negative" : "warning");
        }
        const ddEl = $("#mon-drawdown");
        if (ddEl) {
            const dd = data.current_drawdown || 0;
            ddEl.textContent = dd.toFixed(1) + "%";
            ddEl.className = "monitor-metric-value " + (dd > 5 ? "negative" : dd > 2 ? "warning" : "positive");
        }
        const balEl = $("#mon-balance");
        if (balEl) {
            const b = data.paper_balance || 1000;
            balEl.textContent = "$" + b.toFixed(2);
            balEl.className = "monitor-metric-value " + (b >= 1000 ? "positive" : "negative");
        }
        const mddEl = $("#mon-max-dd");
        if (mddEl) {
            const mdd = data.max_drawdown || 0;
            mddEl.textContent = mdd.toFixed(1) + "%";
            mddEl.className = "monitor-metric-value " + (mdd > 10 ? "negative" : mdd > 5 ? "warning" : "positive");
        }

        // Loss causes bars
        const causesEl = $("#mon-loss-causes");
        if (causesEl) {
            const causes = data.loss_causes || [];
            if (causes.length === 0) {
                causesEl.innerHTML = '<span class="muted">No losses to analyze yet</span>';
            } else {
                const maxCount = Math.max(...causes.map(c => c.count), 1);
                let html = "";
                for (const c of causes) {
                    const pct = (c.count / maxCount * 100).toFixed(0);
                    html += `<div class="monitor-cause-row">
                        <span class="monitor-cause-label">${escapeHtml(c.cause)}</span>
                        <div class="monitor-cause-bar-bg">
                            <div class="monitor-cause-bar" style="width:${pct}%"></div>
                        </div>
                        <span class="monitor-cause-count">${c.count}</span>
                    </div>`;
                }
                causesEl.innerHTML = html;
            }
        }

        // Side performance
        const sideEl = $("#mon-side-perf");
        if (sideEl && data.side_performance) {
            let html = "";
            for (const [sideName, sp] of Object.entries(data.side_performance)) {
                const wr = sp.win_rate || 0;
                const pnl = sp.pnl || 0;
                const labelCls = sideName === "long" ? "long-label" : "short-label";
                const barCls = sideName === "long" ? "long-bar" : "short-bar";
                const pnlCls = pnl >= 0 ? "pnl-positive" : "pnl-negative";
                html += `<div class="monitor-side-row">
                    <span class="monitor-side-label ${labelCls}">${sideName}</span>
                    <span class="monitor-side-wr">${wr.toFixed(0)}% WR</span>
                    <div class="monitor-side-bar-bg">
                        <div class="monitor-side-bar ${barCls}" style="width:${wr}%"></div>
                    </div>
                    <span class="monitor-side-pnl ${pnlCls}">${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}%</span>
                </div>`;
            }
            sideEl.innerHTML = html || '<span class="muted">--</span>';
        }

        // Recommendations
        const recsEl = $("#mon-recommendations");
        if (recsEl) {
            const recs = data.recommendations || [];
            if (recs.length === 0) {
                recsEl.innerHTML = '<span class="muted" style="font-size:0.8rem">✅ No issues detected — strategy performing well</span>';
            } else {
                let html = "";
                for (const r of recs) {
                    const sev = r.severity || "low";
                    html += `<div class="monitor-rec sev-${sev}">${escapeHtml(r.message)}</div>`;
                }
                recsEl.innerHTML = html;
            }
        }

        // Recent losses table
        const lossBody = $("#mon-losses-body");
        if (lossBody) {
            const losses = data.recent_losses || [];
            if (losses.length === 0) {
                lossBody.innerHTML = '<tr><td colspan="7" class="muted">No losses yet 🎉</td></tr>';
            } else {
                let html = "";
                for (const l of losses) {
                    const sideClass = l.side === "long" ? "side-long" : "side-short";
                    const causes = (l.loss_causes || []).map(c =>
                        `<span class="cause-pill">${escapeHtml(c.replace(/_/g, " "))}</span>`
                    ).join("");
                    html += `<tr>
                        <td>${escapeHtml(l.symbol || "")}</td>
                        <td><span class="${sideClass}">${l.side}</span></td>
                        <td>${escapeHtml((l.setup || "").replace(/_/g, " "))}</td>
                        <td class="pnl-negative mono">${(l.pnl_pct || 0).toFixed(3)}%</td>
                        <td class="pnl-negative mono">$${(l.pnl_usd || 0).toFixed(2)}</td>
                        <td>${causes}</td>
                        <td><span class="sev-badge ${l.severity || 'low'}">${(l.severity || "low").toUpperCase()}</span></td>
                    </tr>`;
                }
                lossBody.innerHTML = html;
            }
        }

        // Hourly heatmap
        const heatmapEl = $("#mon-hourly-heatmap");
        if (heatmapEl && data.hourly_heatmap) {
            let html = "";
            for (const h of data.hourly_heatmap) {
                const pnl = h.pnl || 0;
                let cellClass = "hm-neutral";
                if (pnl > 0.3) cellClass = "hm-strong-positive";
                else if (pnl > 0) cellClass = "hm-positive";
                else if (pnl < -0.3) cellClass = "hm-strong-negative";
                else if (pnl < 0) cellClass = "hm-negative";

                const trades = h.trades || 0;
                const istHour = (h.hour + 5) % 24;  // UTC+5:30 approx display
                html += `<div class="heatmap-cell ${cellClass}" title="${istHour}:00 IST | ${trades} trades | WR: ${h.wr}%">
                    <span class="hm-hour">${String(istHour).padStart(2,"0")}</span>
                    <span class="hm-pnl">${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}</span>
                </div>`;
            }
            heatmapEl.innerHTML = html;
        }
    }

    // ── P&L Equity Curve ──────────────────────────────────────────────

    function drawEquityCurve(closedSignals) {
        const canvas = document.getElementById("equity-canvas");
        if (!canvas || !closedSignals || closedSignals.length === 0) return;

        const ctx = canvas.getContext("2d");
        const dpr = window.devicePixelRatio || 1;
        const rect = canvas.parentElement.getBoundingClientRect();
        canvas.width = rect.width * dpr;
        canvas.height = rect.height * dpr;
        ctx.scale(dpr, dpr);
        const W = rect.width;
        const H = rect.height;

        // Sort by exit time
        const sorted = [...closedSignals]
            .filter(s => s.exit_time)
            .sort((a, b) => new Date(a.exit_time) - new Date(b.exit_time));

        if (sorted.length === 0) return;

        // Build equity curve: cumulative P&L starting at $1000
        const startBalance = 1000;
        const points = [{ x: 0, y: startBalance, time: null }];
        let balance = startBalance;
        sorted.forEach((sig, i) => {
            balance += (sig.pnl_usd || 0);
            points.push({ x: i + 1, y: balance, time: sig.exit_time });
        });

        // Update header stats
        const eqBal = document.getElementById("eq-balance");
        const eqPnl = document.getElementById("eq-pnl-total");
        if (eqBal) eqBal.textContent = "$" + balance.toFixed(2);
        if (eqPnl) {
            const totalPnl = balance - startBalance;
            eqPnl.textContent = (totalPnl >= 0 ? "+" : "") + "$" + totalPnl.toFixed(2);
            eqPnl.style.color = totalPnl >= 0 ? "#00e676" : "#ff1744";
        }

        // Scale
        const minY = Math.min(...points.map(p => p.y));
        const maxY = Math.max(...points.map(p => p.y));
        const yRange = maxY - minY || 1;
        const pad = { top: 20, right: 50, bottom: 30, left: 60 };
        const chartW = W - pad.left - pad.right;
        const chartH = H - pad.top - pad.bottom;

        const scaleX = (i) => pad.left + (i / (points.length - 1)) * chartW;
        const scaleY = (v) => pad.top + chartH - ((v - minY) / yRange) * chartH;

        // Clear
        ctx.clearRect(0, 0, W, H);

        // Grid lines
        ctx.strokeStyle = "rgba(255,255,255,0.06)";
        ctx.lineWidth = 1;
        const gridLines = 5;
        for (let i = 0; i <= gridLines; i++) {
            const y = pad.top + (i / gridLines) * chartH;
            ctx.beginPath();
            ctx.moveTo(pad.left, y);
            ctx.lineTo(W - pad.right, y);
            ctx.stroke();

            // Y-axis labels
            const val = maxY - (i / gridLines) * yRange;
            ctx.fillStyle = "rgba(255,255,255,0.3)";
            ctx.font = "10px 'SF Mono', monospace";
            ctx.textAlign = "right";
            ctx.fillText("$" + val.toFixed(1), pad.left - 8, y + 3);
        }

        // $1000 baseline
        const baseY = scaleY(startBalance);
        ctx.strokeStyle = "rgba(255,255,255,0.15)";
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(pad.left, baseY);
        ctx.lineTo(W - pad.right, baseY);
        ctx.stroke();
        ctx.setLineDash([]);

        // Fill gradient under curve
        const gradient = ctx.createLinearGradient(0, pad.top, 0, H - pad.bottom);
        if (balance >= startBalance) {
            gradient.addColorStop(0, "rgba(0, 230, 118, 0.25)");
            gradient.addColorStop(1, "rgba(0, 230, 118, 0.02)");
        } else {
            gradient.addColorStop(0, "rgba(255, 23, 68, 0.25)");
            gradient.addColorStop(1, "rgba(255, 23, 68, 0.02)");
        }

        ctx.beginPath();
        ctx.moveTo(scaleX(0), scaleY(points[0].y));
        for (let i = 1; i < points.length; i++) {
            ctx.lineTo(scaleX(i), scaleY(points[i].y));
        }
        ctx.lineTo(scaleX(points.length - 1), H - pad.bottom);
        ctx.lineTo(scaleX(0), H - pad.bottom);
        ctx.closePath();
        ctx.fillStyle = gradient;
        ctx.fill();

        // Line
        ctx.beginPath();
        ctx.moveTo(scaleX(0), scaleY(points[0].y));
        for (let i = 1; i < points.length; i++) {
            ctx.lineTo(scaleX(i), scaleY(points[i].y));
        }
        ctx.strokeStyle = balance >= startBalance ? "#00e676" : "#ff1744";
        ctx.lineWidth = 2;
        ctx.lineJoin = "round";
        ctx.stroke();

        // Dots for wins/losses
        for (let i = 1; i < points.length; i++) {
            const sig = sorted[i - 1];
            const x = scaleX(i);
            const y = scaleY(points[i].y);
            const isWin = (sig.pnl_usd || 0) > 0;

            ctx.beginPath();
            ctx.arc(x, y, 3, 0, Math.PI * 2);
            ctx.fillStyle = isWin ? "#00e676" : "#ff1744";
            ctx.fill();
        }

        // Current balance end label
        const lastPoint = points[points.length - 1];
        const endX = scaleX(points.length - 1);
        const endY = scaleY(lastPoint.y);
        ctx.fillStyle = balance >= startBalance ? "#00e676" : "#ff1744";
        ctx.font = "bold 11px 'SF Mono', monospace";
        ctx.textAlign = "left";
        ctx.fillText("$" + lastPoint.y.toFixed(2), endX + 6, endY + 4);

        // X-axis labels (every ~10 trades)
        ctx.fillStyle = "rgba(255,255,255,0.3)";
        ctx.font = "9px 'SF Mono', monospace";
        ctx.textAlign = "center";
        const step = Math.max(1, Math.floor(sorted.length / 8));
        for (let i = 0; i < sorted.length; i += step) {
            const x = scaleX(i + 1);
            ctx.fillText("#" + (i + 1), x, H - pad.bottom + 15);
        }
    }

    // ── Refresh loop ──────────────────────────────────────────────────

    // ── Signal Scanner Status ──────────────────────────────────────────
    function updateSignalStatus(data) {
        const container = document.getElementById("signal-status-grid");
        if (!container || !data || Object.keys(data).length === 0) {
            if (container) container.innerHTML = '<p class="muted">Waiting for scan data...</p>';
            return;
        }

        let html = '';
        for (const [symbol, strategies] of Object.entries(data)) {
            const scalp = strategies.scalp || {};
            const invest = strategies.investment || {};

            // Determine card state
            const anySignal = (scalp.signal || invest.signal);
            const cardClass = anySignal ? 'has-signal' : 'no-signal';

            // Format time ago
            const latestTime = scalp.time || invest.time || '';
            let timeAgo = '';
            if (latestTime) {
                const diff = Math.floor((Date.now() - new Date(latestTime).getTime()) / 1000);
                if (diff < 60) timeAgo = diff + 's ago';
                else if (diff < 3600) timeAgo = Math.floor(diff/60) + 'm ago';
                else timeAgo = Math.floor(diff/3600) + 'h ' + (Math.floor(diff/60)%60) + 'm ago';
            }

            html += '<div class="signal-status-card ' + cardClass + '">';
            html += '<div class="ss-header">';
            html += '<span class="ss-symbol">' + symbol + '</span>';
            html += '<span class="ss-time">' + timeAgo + '</span>';
            html += '</div>';

            html += '<div class="ss-strategies">';

            // ── SCALP strategy status ──
            if (scalp.reason) {
                const reasonClass = scalp.signal ? 'signal-yes' : 'signal-no';
                html += '<div class="ss-strat">';
                html += '<div class="ss-strat-header">';
                html += '<span class="ss-strat-label scalp">SCALP</span>';
                html += '<span class="ss-reason ' + reasonClass + '">' + scalp.reason + '</span>';
                html += '</div>';

                // Setup pills WITH per-scanner reasons
                if (scalp.setups_checked && scalp.setups_checked.length > 0) {
                    html += '<div class="ss-setups">';
                    for (const setup of scalp.setups_checked) {
                        const cls = setup.triggered ? 'triggered' : 'not-triggered';
                        const conf = setup.triggered ? ' (' + setup.confidence + ')' : '';
                        html += '<span class="ss-setup-pill ' + cls + '" title="' + (setup.reason || '').replace(/"/g, '&quot;') + '">' + setup.name + conf + '</span>';
                    }
                    html += '</div>';

                    // Show detailed per-scanner reasons (expandable)
                    const failedScanners = scalp.setups_checked.filter(s => !s.triggered && s.reason);
                    if (failedScanners.length > 0 && !scalp.signal) {
                        html += '<div class="ss-scanner-reasons">';
                        html += '<div class="ss-reasons-title">Why no signal:</div>';
                        for (const s of failedScanners) {
                            html += '<div class="ss-reason-row">';
                            html += '<span class="ss-reason-scanner">' + s.name + ':</span> ';
                            html += '<span class="ss-reason-detail">' + s.reason + '</span>';
                            html += '</div>';
                        }
                        html += '</div>';
                    }
                }
                html += '</div>';
            }

            // ── INVESTMENT strategy status ──
            if (invest.reason) {
                const reasonClass = invest.signal ? 'signal-yes' : 'signal-no';
                html += '<div class="ss-strat">';
                html += '<div class="ss-strat-header">';
                html += '<span class="ss-strat-label investment">INVESTMENT</span>';
                html += '<span class="ss-reason ' + reasonClass + '">' + invest.reason + '</span>';
                html += '</div>';
                if (invest.regime) {
                    html += '<span class="ss-regime-badge regime-' + invest.regime.replace(/_/g, '-') + '">' + invest.regime + '</span>';
                }

                // Show investment diagnostics (why no signal)
                const diag = invest.diagnostics;
                if (diag && !invest.signal) {
                    html += '<div class="ss-scanner-reasons">';
                    html += '<div class="ss-reasons-title">Why no signal:</div>';
                    if (diag.regime_note) {
                        html += '<div class="ss-reason-row"><span class="ss-reason-scanner">Regime:</span> <span class="ss-reason-detail">' + diag.regime_note + '</span></div>';
                    }
                    if (diag.long_blockers && diag.long_blockers.length > 0) {
                        html += '<div class="ss-reason-row"><span class="ss-reason-scanner">LONG blocked:</span> <span class="ss-reason-detail">' + diag.long_blockers.join(' | ') + '</span></div>';
                    }
                    if (diag.short_blockers && diag.short_blockers.length > 0) {
                        html += '<div class="ss-reason-row"><span class="ss-reason-scanner">SHORT blocked:</span> <span class="ss-reason-detail">' + diag.short_blockers.join(' | ') + '</span></div>';
                    }
                    html += '</div>';
                }
                html += '</div>';
            }

            html += '</div>';

            // Indicator values (show both scalp and invest indicators)
            const ind = (scalp.indicators && Object.keys(scalp.indicators).length > 0) ? scalp.indicators :
                        (invest.indicators && Object.keys(invest.indicators).length > 0) ? invest.indicators : null;
            if (ind) {
                html += '<div class="ss-indicators">';
                const keyMap = {
                    'rsi': 'RSI', 'close': 'Price', 'rel_vol': 'Vol',
                    'macd': 'MACD', 'atr': 'ATR', 'supertrend_dir': 'ST',
                    'htf_bias': 'HTF', 'ema_8': 'EMA8', 'ema_21': 'EMA21',
                };
                const show = ['close', 'rsi', 'rel_vol', 'macd', 'supertrend_dir', 'htf_bias', 'atr'];
                for (const key of show) {
                    if (ind[key] !== undefined && ind[key] !== null) {
                        let val = ind[key];
                        if (key === 'supertrend_dir') val = val === 1 ? 'Bull' : (val === -1 ? 'Bear' : 'Flat');
                        if (typeof val === 'number' && key !== 'close') val = val.toFixed ? val.toFixed(key === 'macd' ? 4 : key === 'rel_vol' ? 1 : 1) : val;
                        const label = keyMap[key] || key;
                        // Color-code RSI
                        let valClass = '';
                        if (key === 'rsi') {
                            const rsiN = Number(ind[key]);
                            if (rsiN > 70) valClass = ' rsi-ob';
                            else if (rsiN < 30) valClass = ' rsi-os';
                            else if (rsiN > 60) valClass = ' rsi-high';
                            else if (rsiN < 40) valClass = ' rsi-low';
                        }
                        html += '<span class="ss-indicator"><span class="ind-label">' + label + ' </span><span class="ind-val' + valClass + '">' + val + '</span></span>';
                    }
                }
                html += '</div>';
            }

            html += '</div>';
        }

        container.innerHTML = html;
    }

    function updateSignalDrought(signals, closedSignals) {
        const lastSignalEl = document.getElementById("last-signal-time");
        const droughtEl = document.getElementById("signal-drought");
        if (!lastSignalEl || !droughtEl) return;

        // Find the most recent signal time from active signals or closed signals
        let lastTime = null;

        // Check active signals
        if (signals && signals.length > 0) {
            for (const s of signals) {
                const meta = s.meta || s;
                const t = meta.timestamp || meta.time || meta.created_at;
                if (t) {
                    const d = new Date(t);
                    if (!lastTime || d > lastTime) lastTime = d;
                }
            }
        }

        // Check recently closed signals
        if (closedSignals && closedSignals.length > 0) {
            for (const s of closedSignals) {
                const t = s.entry_time || s.created_at;
                if (t) {
                    const d = new Date(t);
                    if (!lastTime || d > lastTime) lastTime = d;
                }
            }
        }

        if (lastTime) {
            const diffMs = Date.now() - lastTime.getTime();
            const diffMin = Math.floor(diffMs / 60000);
            const diffHrs = Math.floor(diffMin / 60);

            let timeStr;
            if (diffMin < 60) timeStr = diffMin + "m ago";
            else if (diffHrs < 24) timeStr = diffHrs + "h " + (diffMin % 60) + "m ago";
            else timeStr = Math.floor(diffHrs / 24) + "d " + (diffHrs % 24) + "h ago";

            lastSignalEl.textContent = "Last signal: " + timeStr;

            // Show drought warning if no signal for > 2 hours
            if (diffMin > 120) {
                droughtEl.style.display = "inline-flex";
                droughtEl.textContent = "\u26A0 Signal drought: " + timeStr;
                if (diffMin > 360) droughtEl.style.color = "#ef4444"; // red > 6h
                else droughtEl.style.color = "#f59e0b"; // amber 2-6h
            } else {
                droughtEl.style.display = "none";
            }
        } else {
            lastSignalEl.textContent = "Last signal: none yet";
            droughtEl.style.display = "inline-flex";
            droughtEl.textContent = "\u26A0 No signals generated yet";
            droughtEl.style.color = "#f59e0b";
        }
    }

    // ── R-Metrics Rendering ──────────────────────────────────────────
    function updateRMetrics(data) {
        if (!data || data.error) return;

        const g = data.global || {};

        // Helper: color R values
        function rColor(val) {
            if (val > 0.5) return "r-positive";
            if (val > 0) return "r-neutral";
            if (val < 0) return "r-negative";
            return "";
        }

        function setR(id, val, decimals) {
            const el = document.querySelector(id);
            if (!el) return;
            const n = Number(val || 0);
            el.textContent = n.toFixed(decimals || 2) + "R";
            el.className = "r-metric-value " + rColor(n);
        }

        setR("#r-avg", g.avg_r, 3);
        setR("#r-total", g.total_r, 2);
        setR("#r-expectancy", g.expectancy_r, 3);
        setR("#r-avg-win", g.avg_win_r, 3);
        setR("#r-avg-loss", g.avg_loss_r, 3);
        setR("#r-avg-mae", g.avg_mae_r, 3);
        setR("#r-avg-mfe", g.avg_mfe_r, 3);

        // Edge ratio (not in R units)
        const edgeEl = document.querySelector("#r-edge-ratio");
        if (edgeEl) {
            const e = Number(g.edge_ratio || 0);
            edgeEl.textContent = e.toFixed(2);
            edgeEl.className = "r-metric-value " + (e > 1.5 ? "r-positive" : e > 1 ? "r-neutral" : "r-negative");
        }

        // Header pills
        const expPill = document.querySelector("#r-expectancy-pill");
        if (expPill) {
            const exp = Number(g.expectancy_r || 0);
            expPill.textContent = "Exp: " + exp.toFixed(3) + "R";
            expPill.style.color = exp > 0 ? "var(--accent-buy)" : "var(--accent-sell)";
        }
        const edgePill = document.querySelector("#r-edge-pill");
        if (edgePill) {
            const edge = Number(g.edge_ratio || 0);
            edgePill.textContent = "Edge: " + edge.toFixed(2);
            edgePill.style.color = edge > 1 ? "var(--accent-buy)" : "var(--accent-sell)";
        }

        // Per-scanner table
        const tbody = document.querySelector("#r-scanner-body");
        if (!tbody) return;
        const scanners = data.by_scanner || [];
        if (scanners.length === 0) {
            tbody.innerHTML = '<tr><td colspan="12" class="muted">No R-data yet (need closed trades)</td></tr>';
            return;
        }

        tbody.innerHTML = scanners.map(s => {
            const exp = Number(s.expectancy_r || 0);
            const healthClass = exp > 0.3 ? "r-scanner-excellent" :
                                exp > 0 ? "r-scanner-good" :
                                exp > -0.3 ? "r-scanner-warning" : "r-scanner-bad";
            return `<tr class="${healthClass}">
                <td style="color:var(--text-primary)">${s.scanner || "unknown"}</td>
                <td>${s.trades}</td>
                <td>${Number(s.win_rate).toFixed(1)}%</td>
                <td>${Number(s.avg_r).toFixed(3)}</td>
                <td style="font-weight:700">${exp.toFixed(3)}</td>
                <td>${Number(s.total_r).toFixed(2)}</td>
                <td>${Number(s.avg_win_r).toFixed(3)}</td>
                <td>${Number(s.avg_loss_r).toFixed(3)}</td>
                <td>${Number(s.best_r).toFixed(2)}</td>
                <td>${Number(s.worst_r).toFixed(2)}</td>
                <td>${Number(s.avg_mae_r).toFixed(3)}</td>
                <td>${Number(s.avg_mfe_r).toFixed(3)}</td>
            </tr>`;
        }).join("");
    }

    // ── Scanner Health Rendering ──────────────────────────────────────
    function updateScannerHealth(data) {
        if (!data || !Array.isArray(data)) return;
        const tbody = document.querySelector("#sh-body");
        if (!tbody) return;

        if (data.length === 0) {
            tbody.innerHTML = '<tr><td colspan="8" class="muted">No scanner data yet</td></tr>';
            return;
        }

        let activeCount = 0;
        tbody.innerHTML = data.map(s => {
            if (s.status === "active" || s.status === "reduced") activeCount++;
            const statusCls = "sh-status sh-status-" + s.status;
            const expColor = s.expectancy_r > 0.3 ? "var(--accent-buy)" :
                            s.expectancy_r > 0 ? "#22d3ee" :
                            s.expectancy_r > -0.1 ? "var(--accent-warn)" : "var(--accent-sell)";
            return `<tr>
                <td style="color:var(--text-primary)">${s.scanner}</td>
                <td><span class="${statusCls}">${s.status}</span></td>
                <td style="font-family:monospace">${s.weight.toFixed(1)}x</td>
                <td style="font-family:monospace;color:${expColor}">${Number(s.expectancy_r).toFixed(3)}R</td>
                <td>${Number(s.win_rate).toFixed(0)}%</td>
                <td>${s.trades}</td>
                <td style="font-family:monospace">${Number(s.total_r).toFixed(1)}R</td>
                <td style="font-size:0.7rem;color:var(--text-muted)">${s.reason || ""}</td>
            </tr>`;
        }).join("");

        const pill = document.querySelector("#sh-active-count");
        if (pill) pill.textContent = activeCount + " active";
    }

    function updateOpportunityFunnel(data) {
        if (!data) return;
        const funnel = data.funnel || {};
        const fields = ["scanned", "strong", "valid", "weak", "near_miss", "rejected", "blocked_regime", "blocked_cost", "blocked_htf"];
        fields.forEach(f => {
            const el = document.querySelector("#fn-" + f);
            if (el) el.textContent = funnel[f] || 0;
        });

        const signalsPill = document.querySelector("#sh-funnel-signals");
        if (signalsPill) {
            const s = (funnel.strong || 0) + (funnel.valid || 0);
            signalsPill.textContent = s + " signals/hr";
            signalsPill.style.color = s > 0 ? "var(--accent-buy)" : "var(--accent-warn)";
        }

        // Near misses
        const nmList = document.querySelector("#near-misses-list");
        if (!nmList) return;
        const nearMisses = data.near_misses || {};
        const allNm = [];
        Object.entries(nearMisses).forEach(([sym, nms]) => {
            nms.forEach(nm => allNm.push({symbol: sym, ...nm}));
        });
        if (allNm.length === 0) {
            nmList.innerHTML = '<span class="muted">None right now</span>';
        } else {
            nmList.innerHTML = allNm.slice(0, 5).map(nm =>
                `<div class="nm-item">
                    <span class="nm-scanner">${nm.scanner || "?"}</span>
                    ${nm.side ? nm.side.toUpperCase() : "?"} &middot;
                    Score: <span class="nm-score">${Number(nm.weighted_score || 0).toFixed(0)}</span>/50
                </div>`
            ).join("");
        }
    }

    function updateCommandCenter(data) {
        if (!data) return;
        const action = data.action || "WAIT";
        const badge = document.getElementById("cmd-action");
        if (badge) {
            badge.textContent = action;
            badge.className = "cmd-action-badge";
            if (action === "TRADE" || action === "LONG") badge.classList.add("trade-long");
            else if (action === "SHORT") badge.classList.add("trade-short");
            else if (data.risk_state === "BLOCKED") badge.classList.add("blocked");
            else badge.classList.add("wait");
        }
        const el = (id, val) => { const e = document.getElementById(id); if (e) e.textContent = val; };
        el("cmd-reason", data.reason || "");

        // Edge badge
        const edgeBadge = document.getElementById("cmd-edge");
        if (edgeBadge) {
            const es = (data.edge_status || "OFF").toLowerCase();
            edgeBadge.textContent = "EDGE: " + (data.edge_status || "--");
            edgeBadge.className = "cmd-badge cmd-edge " + es;
        }

        // Risk badge
        const riskBadge = document.getElementById("cmd-risk");
        if (riskBadge) {
            const rs = (data.risk_state || "NORMAL").toLowerCase();
            riskBadge.textContent = "RISK: " + (data.risk_state || "--");
            riskBadge.className = "cmd-badge cmd-risk " + rs;
        }

        // Market badge
        const mktBadge = document.getElementById("cmd-market");
        if (mktBadge) {
            const ms = (data.market_state || "UNKNOWN").toLowerCase().replace("_", "-");
            mktBadge.textContent = "MKT: " + (data.market_state || "--").replace("_", " ");
            mktBadge.className = "cmd-badge cmd-market " + ms;
        }

        // Grid cells
        const setup = data.best_scanner ? (data.best_symbol ? data.best_symbol.split("/")[0] + " " : "") + data.best_scanner : "--";
        el("cmd-best-setup", setup);
        el("cmd-best-score", data.best_score ? data.best_score.toFixed(0) : "--");
        el("cmd-regime", (data.regime || "--").replace("_", " "));
        el("cmd-expectancy", data.rolling_expectancy != null ? data.rolling_expectancy.toFixed(3) + "R" : "--");
        el("cmd-session", (data.session || "--").replace("_", " "));
        el("cmd-drawdown", data.drawdown_pct != null ? data.drawdown_pct.toFixed(1) + "%" : "--");

        // Reasons pills
        const reasonsDiv = document.getElementById("cmd-reasons");
        if (reasonsDiv && data.reasons) {
            reasonsDiv.innerHTML = data.reasons.map(r =>
                '<span class="cmd-reason-pill">' + r + '</span>'
            ).join("");
        }
    }

    function updateRegime(data) {
        if (!data) return;
        const tag = document.getElementById("regime-tag");
        const regime = data.regime || "unknown";
        if (tag) {
            tag.textContent = regime.toUpperCase().replace("_", " ");
            tag.className = "section-tag regime-tag " + regime.replace("_", "-");
        }
        const action = data.action || {};
        const el = (id, val) => { const e = document.getElementById(id); if (e) e.textContent = val; };
        el("rg-regime", regime.replace("_", " "));
        el("rg-action", action.allow_trade === false ? "BLOCKED" : "ALLOWED");
        el("rg-size-mult", (action.size_multiplier || 1.0).toFixed(1) + "x");
        el("rg-sl-mult", (action.sl_multiplier || 1.0).toFixed(1) + "x");
        el("rg-min-conf", action.min_confidence || "--");
        el("rg-reason", action.reason || "--");
        const es = data.early_exit_stats || {};
        el("rg-hard-caps", es.hard_loss_caps || 0);
        el("rg-momentum-exits", es.momentum_exits || 0);
        el("rg-shadow-recoveries", es.shadow_recoveries || 0);
    }

    async function refreshAll() {
        const [status, positions, signals, performance, alerts, trackerStats, trackerActive, trackerClosed, aiInsights, monitorReport, signalStatus] = await Promise.all([
            api("/api/status"),
            api("/api/positions"),
            api("/api/signals"),
            api("/api/performance"),
            api("/api/alerts"),
            api("/api/tracker/stats"),
            api("/api/tracker/active"),
            api("/api/tracker/closed"),
            api("/api/ai/insights"),
            api("/api/monitor/report"),
            api("/api/signal-status"),
        ]);

        // Store current prices for tracked signal unrealized PnL
        if (status && status.prices) currentPrices = status.prices;

        updateStatus(status);
        updatePositions(positions);
        updateSignals(signals);
        updatePerformance(performance);
        updateAlerts(alerts);
        updateTrackerStats(trackerStats);

        // Override paper balance from monitor report (tracker stats shows exchange balance, not paper tracking balance)
        if (monitorReport && monitorReport.paper_balance) {
            const balEl = document.querySelector("#pf-balance");
            if (balEl) {
                const bal = monitorReport.paper_balance;
                balEl.textContent = "$" + bal.toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
                balEl.style.color = bal >= 1000 ? "var(--accent-buy)" : bal >= 900 ? "var(--accent-warn)" : "var(--accent-sell)";
            }
            const pnlEl = document.querySelector("#pf-pnl-usd");
            if (pnlEl) {
                const bal = monitorReport.paper_balance;
                const pnlUsd = bal - 1000;
                pnlEl.textContent = (pnlUsd >= 0 ? "+$" : "-$") + Math.abs(pnlUsd).toFixed(2);
                pnlEl.className = "value mono " + (pnlUsd >= 0 ? "pnl-positive" : "pnl-negative");
            }
        }

        // Exchange wallet balance (from tracker stats which reports real Delta India balance)
        if (trackerStats) {
            const realBal = Number(trackerStats.paper_balance || 0);
            // Status card
            const exchBal = document.querySelector("#st-exchange-bal");
            if (exchBal) exchBal.textContent = "$" + realBal.toFixed(2);
            // Performance card (prominent)
            const walletEl = document.querySelector("#pf-exchange-wallet");
            if (walletEl) {
                walletEl.textContent = "$" + realBal.toFixed(2);
                walletEl.style.color = realBal > 100 ? "var(--accent-buy)" : realBal > 50 ? "var(--accent-warn)" : "var(--accent-sell)";
            }
        }

        // Sharpe & Sortino ratios from monitor report
        if (monitorReport) {
            const sharpeEl = document.querySelector("#pf-sharpe");
            if (sharpeEl && monitorReport.sharpe_ratio != null) {
                sharpeEl.textContent = monitorReport.sharpe_ratio.toFixed(2);
                sharpeEl.style.color = monitorReport.sharpe_ratio >= 1.5 ? "var(--accent-buy)" : monitorReport.sharpe_ratio >= 0.5 ? "var(--accent-warn)" : "var(--accent-sell)";
            }
            const sortinoEl = document.querySelector("#pf-sortino");
            if (sortinoEl && monitorReport.sortino_ratio != null) {
                sortinoEl.textContent = monitorReport.sortino_ratio.toFixed(2);
                sortinoEl.style.color = monitorReport.sortino_ratio >= 2.0 ? "var(--accent-buy)" : monitorReport.sortino_ratio >= 1.0 ? "var(--accent-warn)" : "var(--accent-sell)";
            }
        }

        // Risk guard status
        const rgBadge = document.querySelector("#risk-guard-badge");
        if (rgBadge && monitorReport) {
            const dd = monitorReport.current_drawdown || 0;
            const streak = monitorReport.current_streak || 0;
            const rw = monitorReport.rolling_20_wr || 100;

            let guardActive = false;
            let guardReason = "";
            if (dd > 8) { guardActive = true; guardReason = "DD>" + dd.toFixed(1) + "%"; }
            else if (streak <= -5) { guardActive = true; guardReason = streak + " streak"; }
            else if (rw < 40 && monitorReport.total_analyzed >= 15) { guardActive = true; guardReason = "WR " + rw.toFixed(0) + "%"; }

            if (guardActive) {
                rgBadge.style.display = "inline-flex";
                document.querySelector("#risk-guard-text").textContent = "PAUSED: " + guardReason;
            } else {
                rgBadge.style.display = "none";
            }
        }

        updateTrackedSignals(trackerActive);
        updateClosedSignals(trackerClosed);
        drawEquityCurve(trackerClosed);
        updateAIInsights(aiInsights);
        updateMonitorReport(monitorReport);
        updateSignalStatus(signalStatus);

        // Signal drought indicator
        updateSignalDrought(signals, trackerClosed);

        // R-Metrics (fetch every 30s, not every 5s)
        if (!window._lastRMetricsFetch || Date.now() - window._lastRMetricsFetch > 30000) {
            window._lastRMetricsFetch = Date.now();
            api("/api/r-metrics").then(updateRMetrics).catch(() => {});
        }

        // Scanner Health, Opportunity Funnel & Regime (fetch every 30s)
        if (!window._lastScannerHealthFetch || Date.now() - window._lastScannerHealthFetch > 30000) {
            window._lastScannerHealthFetch = Date.now();
            api("/api/scanner-health").then(updateScannerHealth).catch(() => {});
            api("/api/opportunity-funnel").then(updateOpportunityFunnel).catch(() => {});
            api("/api/regime").then(updateRegime).catch(() => {});
            api("/api/decision").then(updateCommandCenter).catch(() => {});
        }

        // VM Infrastructure (fetch every 30s, not every 5s)
        if (!window._lastInfraFetch || Date.now() - window._lastInfraFetch > 30000) {
            window._lastInfraFetch = Date.now();
            api("/api/infra").then(updateInfra).catch(() => {});
        }
    }

    function startRefresh() {
        stopRefresh();
        refreshAll();
        intervalId = setInterval(refreshAll, REFRESH_MS);
    }

    function stopRefresh() {
        if (intervalId) { clearInterval(intervalId); intervalId = null; }
    }

    // ── Pause / Resume ────────────────────────────────────────────────

    window.togglePause = async function () {
        const btn = $("#pause-btn");
        btn.disabled = true;
        try {
            await fetch(isPaused ? "/api/control/resume" : "/api/control/pause", { method: "POST" });
            await refreshAll();
        } catch (err) {
            console.error("Control action failed:", err);
        } finally {
            btn.disabled = false;
        }
    };

    // ── Symbol filter click handler ──────────────────────────────────

    function matchesSymbolFilter(symbol, filter) {
        if (filter === "all") return true;
        return (symbol || "").toUpperCase().includes(filter.toUpperCase());
    }

    function symbolColorClass(symbol) {
        if (!symbol) return "";
        const s = symbol.toUpperCase();
        if (s.includes("BTC")) return "symbol-btc";
        if (s.includes("ETH")) return "symbol-eth";
        return "";
    }

    // ── VM Infrastructure ────────────────────────────────────────────

    function updateInfra(data) {
        if (!data) return;

        const el = (id) => document.getElementById(id);

        // Current VM info
        if (data.shape) el("vm-shape").textContent = data.shape;
        if (data.ocpus) el("vm-ocpus").textContent = data.ocpus;

        if (data.memory) {
            const m = data.memory;
            el("vm-memory-total").textContent = m.total_mb + " MB";
            el("vm-memory-used").textContent = m.used_mb + " MB";
            el("vm-memory-free").textContent = m.free_mb + " MB";
            el("vm-swap-used").textContent = m.swap_used_mb + " / " + m.swap_total_mb + " MB";

            // Memory bar
            const bar = el("vm-memory-bar");
            const pctEl = el("vm-memory-pct");
            if (bar && pctEl) {
                bar.style.width = m.percent + "%";
                pctEl.textContent = m.percent.toFixed(0) + "%";
                bar.className = "vm-memory-bar-inner" +
                    (m.percent > 85 ? " critical" : m.percent > 70 ? " warn" : "");
                pctEl.style.color = m.percent > 85 ? "var(--accent-sell)" : m.percent > 70 ? "var(--accent-warn)" : "var(--accent-buy)";
            }

            // Health indicator
            const healthMsg = el("vm-health-msg");
            if (healthMsg) {
                if (m.percent > 85) {
                    healthMsg.textContent = "CRITICAL: Memory usage " + m.percent.toFixed(0) + "% — VM may crash. Upgrade recommended!";
                    healthMsg.className = "vm-health-indicator critical";
                } else if (m.percent > 70) {
                    healthMsg.textContent = "WARNING: Memory usage " + m.percent.toFixed(0) + "% — running low";
                    healthMsg.className = "vm-health-indicator warning";
                } else {
                    healthMsg.textContent = "Healthy: Memory usage " + m.percent.toFixed(0) + "% — stable";
                    healthMsg.className = "vm-health-indicator healthy";
                }
            }
        }

        if (data.cpu) {
            el("vm-cpu-load").textContent = data.cpu.load_1m + " / " + data.cpu.load_5m + " / " + data.cpu.load_15m;
        }

        if (data.disk) {
            el("vm-disk-used").textContent = data.disk.used_gb + " / " + data.disk.total_gb + " GB (" + data.disk.percent + "%)";
        }

        if (data.public_ip) el("vm-public-ip").textContent = data.public_ip;
        if (data.os_uptime) {
            el("vm-os-uptime").textContent = data.os_uptime;
            el("vm-uptime-pill").textContent = "uptime: " + data.os_uptime;
        }

        // Shape pill
        if (data.shape) {
            el("vm-shape-pill").textContent = data.arch === "aarch64" ? "ARM A1.Flex" : "AMD E2.Micro";
        }

        // Status tag
        const tag = el("vm-status-tag");
        if (tag) {
            if (data.memory && data.memory.percent > 85) {
                tag.textContent = "CRITICAL";
                tag.style.background = "rgba(255,23,68,0.18)";
                tag.style.color = "#ff1744";
                tag.style.borderColor = "rgba(255,23,68,0.35)";
            } else if (data.memory && data.memory.percent > 70) {
                tag.textContent = "WARNING";
                tag.style.background = "rgba(255,171,0,0.18)";
                tag.style.color = "#ffab00";
                tag.style.borderColor = "rgba(255,171,0,0.35)";
            } else {
                tag.textContent = "HEALTHY";
                tag.style.background = "rgba(0,230,118,0.18)";
                tag.style.color = "#00e676";
                tag.style.borderColor = "rgba(0,230,118,0.35)";
            }
        }

        // Upgrade status
        if (data.upgrade) {
            const u = data.upgrade;
            el("vm-upgrade-status").textContent = u.status || "not_started";
            el("vm-upgrade-attempts").textContent = u.attempts != null ? u.attempts + " / " + (u.max_attempts || "--") : "--";
            el("vm-upgrade-last").textContent = u.last_attempt ? new Date(u.last_attempt).toLocaleString() : "--";
            el("vm-upgrade-started").textContent = u.started_at ? new Date(u.started_at).toLocaleString() : "--";

            if (u.target_ocpus && u.target_memory_gb) {
                el("vm-target-shape").textContent = "A1.Flex (" + u.target_ocpus + " OCPU / " + u.target_memory_gb + "GB)";
            }

            // Update health message with upgrade info
            const healthMsg = el("vm-health-msg");
            if (healthMsg && u.status === "retrying") {
                healthMsg.textContent = "UPGRADING: Attempt " + u.attempts + "/" + (u.max_attempts || "?") + " — waiting for A1.Flex capacity";
                healthMsg.className = "vm-health-indicator upgrading";
            } else if (healthMsg && u.status === "complete") {
                healthMsg.textContent = "UPGRADED: Now running on A1.Flex with " + (u.target_memory_gb || 4) + "GB RAM";
                healthMsg.className = "vm-health-indicator healthy";
            }
        }
    }

    document.addEventListener("click", function (e) {
        const btn = e.target.closest(".symbol-filter-btn");
        if (!btn) return;

        const target = btn.dataset.target;
        const symbol = btn.dataset.symbol;
        symbolFilters[target] = symbol;

        // Update active class on sibling buttons
        btn.parentElement.querySelectorAll(".symbol-filter-btn").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");

        // Re-render the affected table from cache
        if (target === "scalp") updateScalpSection(cachedScalpSignals);
        else if (target === "tracked") updateTrackedSignals(cachedTrackedSignals);
        else if (target === "closed") updateClosedSignals(cachedClosedSignals);
    });

    // ── Boot ──────────────────────────────────────────────────────────

    document.addEventListener("DOMContentLoaded", startRefresh);
    document.addEventListener("visibilitychange", function () {
        document.hidden ? stopRefresh() : startRefresh();
    });
})();
