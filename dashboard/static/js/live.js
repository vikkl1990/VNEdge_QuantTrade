/* ═══════════════════════════════════════════════════════════════
   Live tab (2026-09 redesign). Three tiers:
     1. KPI rail — six numbers, none repeated elsewhere on the screen
     2. Now — open position ladder + signal queue with outcomes
     3. Diagnostics — closed today, funnel + losses, scanner edge
   Everything is read from existing endpoints; nothing here writes
   to the paper ledger. Exposed as window.LiveTab for app.js.
   ═══════════════════════════════════════════════════════════════ */
(function () {
  "use strict";
  const $ = id => document.getElementById(id);
  const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
  const money = (v, sign) => {
    v = Number(v || 0);
    const s = sign ? (v >= 0 ? "+" : "&minus;") : (v < 0 ? "&minus;" : "");
    return s + "$" + Math.abs(v).toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
  };
  const cls = v => v > 0 ? "up" : v < 0 ? "down" : "flat";
  const ist = iso => { if (!iso) return "--"; try { return new Date(iso).toLocaleTimeString("en-IN", {hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Kolkata"}); } catch (e) { return "--"; } };
  const px = (v, sym) => { v = Number(v || 0); const d = v >= 1000 ? 1 : v >= 10 ? 2 : v >= 0.1 ? 4 : 6; return v.toLocaleString("en-US", {minimumFractionDigits: d, maximumFractionDigits: d}); };
  const base = s => String(s || "").replace("/USDT", "");
  const r2 = v => (v >= 0 ? "+" : "") + Number(v || 0).toFixed(2);
  const get = async p => { try { const r = await fetch(p, {credentials: "same-origin", cache: "no-store"}); return r.ok ? await r.json() : null; } catch (e) { return null; } };
  const todayUTC = () => new Date().toISOString().slice(0, 10);
  const tokenColor = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  let lastGood = {};   // last successful payloads, so a single failed poll never blanks a tile
  let lastPaint = 0;

  async function refresh() {
    if (!$("lv-kpis")) return;
    const [summary, active, closed, signals, status, funnel, taxonomy, scanners, infra, ml, sup] = await Promise.all([
      get("/api/paper/summary"), get("/api/tracker/active"), get("/api/tracker/closed"), get("/api/signals"),
      get("/api/status"), get("/api/pipeline/overview"), get("/api/pipeline/loss_taxonomy?hours=24"),
      get("/api/scanner-health"), get("/api/infra/health"), get("/api/ml/health"), get("/api/supervisor/status"),
    ]);
    const D = {
      summary: summary && summary.available !== false ? summary : lastGood.summary,
      active: Array.isArray(active) ? active : (active && active.active) || lastGood.active || [],
      closed: Array.isArray(closed) ? closed : lastGood.closed || [],
      signals: Array.isArray(signals) ? signals : (signals && signals.signals) || lastGood.signals || [],
      status: status || lastGood.status || {}, funnel: (funnel && funnel.funnel) || lastGood.funnel || {},
      taxonomy: taxonomy || lastGood.taxonomy || {}, scanners: Array.isArray(scanners) ? scanners : lastGood.scanners || [],
      infra: infra || lastGood.infra || {}, ml: ml || lastGood.ml || {}, sup: sup || lastGood.sup || {},
    };
    lastGood = D;
    if (!D.summary) return;
    try { paintHealth(D); } catch (e) { console.warn("live: health", e); }
    try { paintKpis(D); } catch (e) { console.warn("live: kpis", e); }
    try { paintPosition(D); } catch (e) { console.warn("live: position", e); }
    try { paintSignals(D); } catch (e) { console.warn("live: signals", e); }
    try { paintClosed(D); } catch (e) { console.warn("live: closed", e); }
    try { paintFunnel(D); } catch (e) { console.warn("live: funnel", e); }
    try { paintScanners(D); } catch (e) { console.warn("live: scanners", e); }
    try { paintFoot(D); } catch (e) { console.warn("live: foot", e); }
    lastPaint = Date.now();
  }

  /* ── top bar health chips (always visible, every tab) ── */
  function paintHealth(D) {
    const el = $("tb-health"); if (!el) return;
    const st = D.status, ob = (D.infra && D.infra.orderbook_cache) || {}, mlp = (D.infra && D.infra.ml_proxy) || {};
    // feed age from the last data update stamp (IST HH:MM:SS)
    let feedAge = null;
    try {
      const m = /(\d{2}):(\d{2}):(\d{2})/.exec(st.last_data_update || "");
      if (m) { const now = new Date(); const ist = new Date(now.toLocaleString("en-US", {timeZone: "Asia/Kolkata"})); const t = new Date(ist); t.setHours(+m[1], +m[2], +m[3], 0); feedAge = Math.max(0, Math.round((ist - t) / 1000)); }
    } catch (e) {}
    const feedCls = feedAge == null ? "off" : feedAge > 600 ? "off" : feedAge > 90 ? "warn" : "";
    const feedTxt = feedAge == null ? "Feed --" : feedAge < 60 ? "Feed " + feedAge + "s" : "Feed " + Math.round(feedAge / 60) + "m";
    const mlOk = D.ml && D.ml.summary ? D.ml.summary.ok + "/" + D.ml.summary.total_expected : "--";
    const mlCls = mlp.cb_open ? "off" : (D.ml && D.ml.overall_health === "OK") ? "" : "warn";
    const supCls = D.sup && D.sup.running ? ((D.sup.anomaly_count || 0) > 0 ? "warn" : "") : "off";
    const paused = st.paused;
    el.innerHTML =
      '<span class="chip mode" title="Production Delta India market data, simulated fills, real money off">Paper &middot; live data</span>' +
      '<span class="chip ' + feedCls + '" title="Age of the newest candle"><span class="dot"></span>' + feedTxt + '</span>' +
      '<span class="chip ' + (ob.running ? "" : "off") + '" title="Orderbook cache: ' + (ob.cached || 0) + ' of ' + (ob.symbols || 0) + ' symbols"><span class="dot"></span>Book ' + (ob.cached || 0) + '/' + (ob.symbols || 0) + '</span>' +
      '<span class="chip ' + mlCls + '" title="ML models loaded on the local ML Lab"><span class="dot"></span>ML ' + mlOk + '</span>' +
      '<span class="chip ' + supCls + '" title="Supervisor"><span class="dot"></span>' + (D.sup && D.sup.running ? (D.sup.anomaly_count || 0) + " alerts" : "Supervisor off") + '</span>' +
      '<span class="chip" title="Bot uptime">' + (paused ? "Paused" : "Up " + esc(st.uptime || "--")) + '</span>';
  }

  /* ── tier 1 ── */
  function paintKpis(D) {
    const S = D.summary, st = D.status, prices = st.prices || {};
    const sorted = [...D.closed].sort((a, b) => new Date(a.exit_time || 0) - new Date(b.exit_time || 0));
    const recent = sorted.slice(-20);
    const rs = recent.map(t => Number(t.exit_r || 0));
    const exp = rs.length ? rs.reduce((a, b) => a + b, 0) / rs.length : 0;
    const openRisk = D.active.reduce((a, t) => a + Math.abs(t.entry_price - t.stop_loss) / (t.entry_price || 1) * (t.position_size_usd || 0), 0);
    const openNotional = D.active.reduce((a, t) => a + (t.position_size_usd || 0), 0);
    const feePct = S.gross_pnl_usd ? S.fees_usd / Math.abs(S.gross_pnl_usd) * 100 : 0;
    const lastBar = String(st.last_data_update || "--").replace(" IST", "");
    $("lv-kpis").innerHTML = `
      <div class="kpi balance">
        <span class="eyebrow">Paper balance</span>
        <span class="v hero">${money(S.balance)}</span>
        <canvas class="spark" id="lv-spark" width="150" height="34" aria-label="Equity since start"></canvas>
        <span class="sub"><b class="${cls(S.net_pnl_usd)}">${money(S.net_pnl_usd, true)}</b> since $${Number(S.start_balance || 0).toLocaleString()} &middot; peak ${money(S.peak_balance)} &middot; DD ${Number(S.max_drawdown_pct || 0).toFixed(1)}%</span>
      </div>
      <div class="kpi"><span class="eyebrow">Today</span><span class="v ${cls(S.today_pnl_usd)}">${money(S.today_pnl_usd, true)}</span><span class="sub">${S.trades_today || 0} trades &middot; <b>${Number(S.today_win_rate || 0).toFixed(0)}%</b> won &middot; fees $${Number(S.today_fees_usd || 0).toFixed(2)}</span></div>
      <div class="kpi"><span class="eyebrow">Open risk</span><span class="v">${money(openRisk)}</span><span class="sub">${D.active.length} of ${window.LIVE_MAX_OPEN || 3} slots &middot; $${openNotional.toFixed(0)} notional${D.active[0] ? " at " + D.active[0].leverage + "x" : ""}</span></div>
      <div class="kpi"><span class="eyebrow">Expectancy &middot; last ${rs.length}</span><span class="v ${cls(exp)}">${r2(exp)}R</span><span class="sub">win rate <b>${Number(S.win_rate || 0).toFixed(0)}%</b> &middot; PF <b>${Number(S.profit_factor || 0).toFixed(2)}</b> &middot; ${S.closed || 0} closed</span></div>
      <div class="kpi"><span class="eyebrow">Fees &middot; all time</span><span class="v">${money(S.fees_usd)}</span><span class="sub"><b>${feePct.toFixed(0)}%</b> of gross &middot; $${(S.closed ? S.fees_usd / S.closed : 0).toFixed(2)} per trade</span></div>
      <div class="kpi"><span class="eyebrow">Last bar</span><span class="v sm">${esc(lastBar)}</span><span class="sub">IST &middot; BTC <b>${(prices["BTC/USDT"] || 0).toLocaleString()}</b> &middot; ${Object.keys(prices).length} symbols</span></div>`;
    drawSpark(S);
  }

  function drawSpark(S) {
    const c = $("lv-spark"); if (!c) return;
    const eq = (S.equity_curve || []).map(p => Number(p.balance)).filter(n => !isNaN(n));
    if (eq.length < 2) return;
    const ctx = c.getContext("2d"), dpr = window.devicePixelRatio || 1;
    c.width = 150 * dpr; c.height = 34 * dpr; ctx.scale(dpr, dpr);
    const start = Number(S.start_balance || 1000);
    const min = Math.min(...eq, start), max = Math.max(...eq, start);
    const x = i => 2 + i / (eq.length - 1) * 146, y = v => 31 - (v - min) / (max - min || 1) * 28;
    const col = tokenColor(eq[eq.length - 1] >= start ? "--profit" : "--loss");
    ctx.beginPath(); eq.forEach((v, i) => i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v)));
    ctx.lineTo(x(eq.length - 1), 34); ctx.lineTo(x(0), 34); ctx.closePath(); ctx.fillStyle = col; ctx.globalAlpha = .14; ctx.fill(); ctx.globalAlpha = 1;
    ctx.beginPath(); eq.forEach((v, i) => i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))); ctx.strokeStyle = col; ctx.lineWidth = 1.5; ctx.stroke();
    ctx.setLineDash([2, 3]); ctx.strokeStyle = tokenColor("--line-strong"); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, y(start)); ctx.lineTo(150, y(start)); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = col; ctx.beginPath(); ctx.arc(x(eq.length - 1), y(eq[eq.length - 1]), 2.5, 0, Math.PI * 2); ctx.fill();
  }

  /* ── tier 2: position ladder ── */
  function paintPosition(D) {
    const wrap = $("lv-position"), cnt = $("lv-pos-count"); if (!wrap) return;
    const prices = D.status.prices || {};
    cnt.textContent = D.active.length + " of " + (window.LIVE_MAX_OPEN || 3) + " slots";
    if (!D.active.length) { wrap.innerHTML = '<div class="empty">No open position. Next 5m close at ' + nextClose() + '.</div>'; return; }
    wrap.innerHTML = D.active.map(t => {
      const price = prices[t.symbol] || t.entry_price, short = t.side === "short", dir = short ? -1 : 1;
      const pnl = (price - t.entry_price) / t.entry_price * dir * (t.position_size_usd || 0);
      const stop = t.chandelier_stop && t.atr_trail_active ? t.chandelier_stop : t.stop_loss;
      const levels = [t.stop_loss, t.entry_price, t.tp1, t.tp2, price, stop].filter(v => v > 0);
      const lo = Math.min(...levels), hi = Math.max(...levels), span = hi - lo || 1;
      const pos = v => ((v - lo) / span * 100).toFixed(1);
      const rung = (lab, p, extra) => p > 0 ? `<div class="rung"><span class="lab">${lab}</span><span class="bar"><i style="left:${pos(p)}%" class="${extra || ""}"></i><i style="left:${pos(price)}%" class="here"></i></span><span class="px">${px(p)}</span><span class="d">${r2((p - price) / price * 100 * dir)}%</span></div>` : "";
      const risk = Math.abs(t.entry_price - t.stop_loss) / t.entry_price * (t.position_size_usd || 0);
      return `<div class="pos" data-trade='${esc(JSON.stringify(t))}' title="Click for detail">
        <div class="pos-head"><span class="side ${esc(t.side)}">${esc(t.side)}</span><span class="sym">${esc(base(t.symbol))}</span><span class="meta">${esc(t.trade_type || "")} &middot; ${esc(String(t.setup_type || "").replace(/_/g, " "))} &middot; ${t.leverage || "--"}x</span><span class="pnl ${cls(pnl)}">${money(pnl, true)}</span></div>
        <div class="ladder">${rung(stop !== t.stop_loss ? "Trail" : "Stop", stop)}${rung("Entry", t.entry_price)}${rung("TP1", t.tp1)}${rung("TP2", t.tp2)}</div>
        <div class="pos-foot"><span>Opened <b>${ist(t.entry_time)}</b></span><span>Stake <b>$${Number(t.paper_stake || 0).toFixed(0)}</b></span><span>Risk <b>$${risk.toFixed(2)}</b></span><span>MFE <b>${Number(t.mfe_r || 0).toFixed(2)}R</b></span><span>Slip <b>${Number(t.slippage_bps || 0).toFixed(0)} bp</b></span>${t.tp1_hit ? '<span class="tag fill">TP1 hit</span>' : ""}${t.breakeven_set ? '<span class="tag">breakeven</span>' : ""}</div>
      </div>`;
    }).join("");
    wrap.querySelectorAll(".pos").forEach(p => p.addEventListener("click", () => { try { openTradeDetail(JSON.parse(p.dataset.trade)); } catch (e) {} }));
  }
  function nextClose() { const d = new Date(); const m = d.getMinutes(); const n = new Date(d); n.setMinutes(m - m % 5 + 5, 0, 0); return ist(n.toISOString()); }

  /* ── tier 2: signal queue ── */
  function paintSignals(D) {
    const tbl = $("lv-signals"), sub = $("lv-sig-sub"); if (!tbl) return;
    const sig = [...D.signals].sort((a, b) => new Date(b.timestamp || 0) - new Date(a.timestamp || 0)).slice(0, 10);
    const filled = new Map();
    D.active.concat(D.closed).forEach(t => filled.set(t.symbol + "|" + (t.entry_time || "").slice(0, 16), t));
    let nFilled = 0;
    const rows = sig.map(s => {
      const m = s.metadata || {}; const setup = m.setup_type || s.setup_type || "--";
      const key = s.symbol + "|" + (s.timestamp || "").slice(0, 16);
      const t = filled.get(key); if (t) nFilled++;
      const blocked = s.blocked_reason || m.block_reason || m.blocked_reason || (m.confirmations || []).find(c => /BLOCK|PREFILTER|VETO/i.test(c)) || "";
      let outcome;
      if (t && t.exit_time) outcome = `<span class="tag ${t.pnl_usd >= 0 ? "fill" : "loss"}">closed ${money(t.pnl_usd, true)}</span>`;
      else if (t) outcome = '<span class="tag fill">open</span>';
      else if (blocked) outcome = '<span class="tag block">blocked</span> <span class="reason" title="' + esc(blocked) + '">' + esc(String(blocked).replace(/^\[|\]$/g, "").slice(0, 40)) + '</span>';
      else if (s.status === "expired") outcome = '<span class="tag">expired</span>';
      else outcome = '<span class="tag">not filled</span>';
      const g = String(s.grade || "");
      return `<tr><td class="mono">${ist(s.timestamp)}</td><td><span class="side ${esc(s.side)}">${esc(s.side)}</span> <b>${esc(base(s.symbol))}</b></td><td>${esc(String(setup).replace(/_/g, " "))}${s.trade_type || m.trade_type ? ' <span class="tag">' + esc(s.trade_type || m.trade_type) + "</span>" : ""}</td><td class="num">${px(s.entry_price)}</td><td class="num">${px(s.stop_loss)}</td><td class="num">${s.confidence || 0}</td><td><span class="grade ${g.startsWith("A") ? "a" : ""}">${esc(g || "--")}</span></td><td>${m.ml_probability != null ? '<span class="mono">' + Math.round(m.ml_probability * 100) + "%</span>" : "--"}</td><td>${outcome}</td></tr>`;
    });
    sub.innerHTML = "last " + sig.length + " &middot; " + nFilled + " filled";
    tbl.innerHTML = '<thead><tr><th>Time</th><th>Symbol</th><th>Setup</th><th class="num">Entry</th><th class="num">Stop</th><th class="num">Conf</th><th>Grade</th><th>ML</th><th>Outcome</th></tr></thead><tbody>' +
      (rows.length ? rows.join("") : '<tr><td colspan="9"><div class="empty">No signals yet this session</div></td></tr>') + "</tbody>";
  }

  /* ── tier 3: closed today ── */
  function paintClosed(D) {
    const tbl = $("lv-closed"), peek = $("lv-closed-peek"); if (!tbl) return;
    const today = todayUTC();
    const ct = [...D.closed].filter(t => (t.exit_time || "").startsWith(today)).sort((a, b) => new Date(b.exit_time) - new Date(a.exit_time));
    const S = D.summary, wins = ct.filter(t => t.pnl_usd > 0).length;
    const worst = ct.length ? Math.min(...ct.map(t => t.pnl_usd)) : 0, best = ct.length ? Math.max(...ct.map(t => t.pnl_usd)) : 0;
    peek.innerHTML = `<span><b>${ct.length}</b> trades</span><span><b>${wins}</b> won</span><span>net <b class="${cls(S.today_pnl_usd)}">${money(S.today_pnl_usd, true)}</b></span><span>best <b class="up">${money(best, true)}</b></span><span>worst <b class="down">${money(worst, true)}</b></span>`;
    const rows = ct.map(t => {
      const held = Math.max(0, Math.round((new Date(t.exit_time) - new Date(t.entry_time)) / 60000));
      return `<tr class="rowlink" data-trade='${esc(JSON.stringify(t))}'><td class="mono">${ist(t.exit_time)}</td><td><span class="side ${esc(t.side)}">${esc(t.side)}</span> <b>${esc(base(t.symbol))}</b></td><td><span class="tag">${esc(t.trade_type || "")}</span></td><td>${esc(String(t.setup_type || "").replace(/_/g, " "))}</td><td class="num">${px(t.entry_price)}</td><td class="num">${px(t.exit_price)}</td><td class="num ${cls(t.pnl_usd)}">${money(t.pnl_usd, true)}</td><td class="num ${cls(t.exit_r)}">${r2(t.exit_r)}</td><td class="num">${Number(t.mfe_r || 0).toFixed(2)}</td><td class="num">$${Number(t.total_fees_usd || 0).toFixed(2)}</td><td>${esc(String(t.exit_reason || "").replace(/_/g, " "))}</td><td class="num">${held >= 60 ? Math.floor(held / 60) + "h " + held % 60 + "m" : held + "m"}</td></tr>`;
    });
    tbl.innerHTML = '<thead><tr><th>Closed</th><th>Symbol</th><th>Type</th><th>Setup</th><th class="num">Entry</th><th class="num">Exit</th><th class="num">P&amp;L</th><th class="num">R</th><th class="num">MFE</th><th class="num">Fees</th><th>Exit reason</th><th class="num">Held</th></tr></thead><tbody>' +
      (rows.length ? rows.join("") : '<tr><td colspan="12"><div class="empty">Nothing closed yet today (UTC day)</div></td></tr>') + "</tbody>";
    tbl.querySelectorAll("tr.rowlink").forEach(r => { r.style.cursor = "pointer"; r.addEventListener("click", () => { try { openTradeDetail(JSON.parse(r.dataset.trade)); } catch (e) {} }); });
  }

  /* ── tier 3: funnel + losses ── */
  function paintFunnel(D) {
    const f = D.funnel || {}, peek = $("lv-funnel-peek"), fw = $("lv-funnel"), lw = $("lv-losses"); if (!fw) return;
    const steps = [["Scanned", f.scanned], ["Rejected by scanner", f.rejected_strategy], ["Blocked by regime", f.blocked_regime], ["Valid setups", f.tier_valid], ["Paper filled", f.paper_emitted]];
    const maxN = Math.max(...steps.map(s => Number(s[1] || 0)), 1);
    const hit = f.scanned ? (Number(f.paper_emitted || 0) / f.scanned * 100).toFixed(1) : "0.0";
    peek.innerHTML = `<span><b>${f.scanned || 0}</b> scanned</span><span><b>${f.tier_valid || 0}</b> valid</span><span><b>${f.paper_emitted || 0}</b> filled</span><span>hit rate <b>${hit}%</b></span>`;
    fw.innerHTML = '<span class="eyebrow">Where candidates went today</span>' + steps.map(([l, n]) => `<div class="frow"><span>${l}</span><span class="bar"><i style="width:${Number(n || 0) / maxN * 100}%"></i></span><span class="n">${n || 0}</span></div>`).join("");
    const lt = D.taxonomy || {}, b = lt.buckets || {};
    const rows = Object.entries(b).filter(([, v]) => v && v.count > 0).sort((a, c) => a[1].total_loss_usd - c[1].total_loss_usd);
    const maxL = Math.max(...rows.map(([, v]) => Math.abs(v.total_loss_usd)), 1);
    lw.innerHTML = '<span class="eyebrow">Losses &middot; last 24h &middot; ' + money(lt.total_loss_usd || 0, true) + '</span>' +
      (rows.length ? rows.map(([k, v]) => `<div class="lossrow"><span title="${esc((v.sample_symbols || []).join(", "))}">${esc(k.replace(/_/g, " "))}</span><span class="bar"><i style="width:${Math.abs(v.total_loss_usd) / maxL * 100}%"></i></span><span class="n down">${money(v.total_loss_usd, true)} &middot; ${v.count}</span></div>`).join("") + '<p class="note">A trade can sit in more than one bucket.</p>'
        : '<div class="empty" style="margin-top:8px">No losses in the last 24h</div>');
  }

  /* ── tier 3: scanner edge ── */
  function paintScanners(D) {
    const tbl = $("lv-scanners"), peek = $("lv-scan-peek"); if (!tbl) return;
    const sc = D.scanners || [];
    peek.innerHTML = sc.length ? sc.map(s => `<span>${esc(s.scanner)} <b class="${cls(s.expectancy_r)}">${r2(s.expectancy_r)}R</b> &middot; ${s.trades}t</span>`).join("") : "<span>no closed trades yet</span>";
    tbl.innerHTML = '<thead><tr><th>Scanner</th><th>Status</th><th class="num">Trades</th><th class="num">Win rate</th><th class="num">Expectancy</th><th class="num">Total R</th><th class="num">Edge ratio</th><th class="num">Weight</th><th>Verdict</th></tr></thead><tbody>' +
      (sc.length ? sc.map(s => `<tr><td><b>${esc(s.scanner)}</b></td><td><span class="tag ${s.status === "active" ? "fill" : "block"}">${esc(s.status)}</span></td><td class="num">${s.trades}</td><td class="num">${Number(s.win_rate || 0).toFixed(0)}%</td><td class="num ${cls(s.expectancy_r)}">${(s.expectancy_r >= 0 ? "+" : "") + Number(s.expectancy_r || 0).toFixed(3)}R</td><td class="num">${Number(s.total_r || 0).toFixed(2)}</td><td class="num">${Number(s.edge_ratio || 0).toFixed(1)}</td><td class="num">${Number(s.weight || 0).toFixed(1)}x</td><td class="reason">${esc(s.reason || "")}</td></tr>`).join("")
        : '<tr><td colspan="9"><div class="empty">Scanner grades appear after the first closed trade</div></td></tr>') + "</tbody>";
  }

  function paintFoot(D) {
    const el = $("lv-foot"); if (!el) return;
    const S = D.summary, fees = D.status.fees || {};
    const pct = v => v == null ? "--" : (v * 100).toFixed(4).replace(/0+$/, "").replace(/\.$/, "") + "%";
    el.innerHTML = `<span>Ledger <b>${S.closed || 0} closed</b> &middot; ${S.trading_days || 0} days</span><span>Fees maker <b>${pct(fees.maker)}</b> taker <b>${pct(fees.taker)}</b> incl. GST</span><span>Analysis on <b>5m close</b>, completed bars only</span><span>Server <b>${esc(String(D.status.server_time || "").slice(11, 19))}</b></span><span class="ml">Delta Exchange India &middot; production data &middot; simulated fills</span>`;
  }

  window.LiveTab = { refresh, get lastPaint() { return lastPaint; } };
  // The health chips live in the top bar and are visible on every tab, so keep
  // them fresh even when app.js is not polling the Live tab.
  setInterval(() => { if (Date.now() - lastPaint > 12000) refresh(); }, 15000);
})();
