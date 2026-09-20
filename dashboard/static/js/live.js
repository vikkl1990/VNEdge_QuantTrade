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
    const [summary, active, closed, signals, status, funnel, taxonomy, scanners, infra, ml, sup, fresh, live, ctx] = await Promise.all([
      get("/api/paper/summary"), get("/api/tracker/active"), get("/api/tracker/closed"), get("/api/signals"),
      get("/api/status"), get("/api/pipeline/overview"), get("/api/pipeline/loss_taxonomy?hours=24"),
      get("/api/scanner-health"), get("/api/infra/health"), get("/api/ml/health"), get("/api/supervisor/status"),
      get("/api/feed/freshness"), get("/api/live/position"), get("/api/live/context"),
    ]);
    const D = {
      summary: summary && summary.available !== false ? summary : lastGood.summary,
      active: Array.isArray(active) ? active : (active && active.active) || lastGood.active || [],
      closed: Array.isArray(closed) ? closed : lastGood.closed || [],
      signals: Array.isArray(signals) ? signals : (signals && signals.signals) || lastGood.signals || [],
      status: status || lastGood.status || {}, funnel: (funnel && funnel.funnel) || lastGood.funnel || {},
      taxonomy: taxonomy || lastGood.taxonomy || {}, scanners: Array.isArray(scanners) ? scanners : lastGood.scanners || [],
      infra: infra || lastGood.infra || {}, ml: ml || lastGood.ml || {}, sup: sup || lastGood.sup || {},
      fresh: fresh || lastGood.fresh || null,
      live: (live && Array.isArray(live.trades)) ? live : lastGood.live || {trades: [], max_open: 3},
      ctx: ctx || lastGood.ctx || null,
    };
    if (D.live && D.live.max_open) window.LIVE_MAX_OPEN = D.live.max_open;
    lastGood = D;
    if (!D.summary) return;
    try { paintHealth(D); } catch (e) { console.warn("live: health", e); }
    try { paintKpis(D); } catch (e) { console.warn("live: kpis", e); }
    try { paintContext(D); } catch (e) { console.warn("live: context", e); }
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
    // Per-symbol freshness wins over the global stamp: a dead LTC feed hid
    // behind a fresh BTC one for 17 minutes on 2026-09-11.
    let feedCls, feedTxt, feedTitle = "Age of the newest candle";
    const fr = D.fresh;
    if (fr && fr.symbols) {
      const worst = Number(fr.worst_age_s || 0), stale = (fr.stale || []).concat(fr.excluded || []);
      const nStale = new Set(stale).size;
      // worst = age of the newest completed 5m bar beyond its close: amber past
      // one bar (300 s), red past two (the orchestrator's stale-frame gate).
      feedCls = worst > 600 || nStale > 3 ? "off" : (worst > 300 || nStale > 0) ? "warn" : "";
      feedTxt = "Feed " + (worst < 60 ? worst.toFixed(0) + "s" : Math.round(worst / 60) + "m") + (nStale ? " · " + nStale + " stale" : "");
      feedTitle = "Worst symbol age " + worst.toFixed(0) + "s" + (nStale ? " · stale: " + [...new Set(stale)].map(s => s.replace("/USDT", "")).join(", ") : " · all " + Object.keys(fr.symbols).length + " symbols fresh");
    } else {
      feedCls = feedAge == null ? "off" : feedAge > 600 ? "off" : feedAge > 90 ? "warn" : "";
      feedTxt = feedAge == null ? "Feed --" : feedAge < 60 ? "Feed " + feedAge + "s" : "Feed " + Math.round(feedAge / 60) + "m";
    }
    const mlOk = D.ml && D.ml.summary ? D.ml.summary.ok + "/" + D.ml.summary.total_expected : "--";
    const mlCls = mlp.cb_open ? "off" : (D.ml && D.ml.overall_health === "OK") ? "" : "warn";
    const supCls = D.sup && D.sup.running ? ((D.sup.anomaly_count || 0) > 0 ? "warn" : "") : "off";
    const paused = st.paused;
    el.innerHTML =
      '<span class="chip mode" title="Production Delta India market data, simulated fills, real money off">Paper &middot; live data</span>' +
      '<span class="chip ' + feedCls + '" title="' + esc(feedTitle) + '"><span class="dot"></span>' + feedTxt + '</span>' +
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

  /* ── tier 2: position row (perp-terminal layout, 2026-09-20) ── */
  const fmtDur = sec => { sec = Math.max(0, Math.round(Number(sec || 0))); const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s2 = sec % 60; return h ? `${h}h ${m}m` : m ? `${m}m ${s2}s` : `${s2}s`; };
  const fmtBig = v => { v = Number(v || 0); return v >= 1e9 ? (v / 1e9).toFixed(2) + "B" : v >= 1e6 ? (v / 1e6).toFixed(1) + "M" : v >= 1e3 ? (v / 1e3).toFixed(0) + "K" : v.toFixed(0); };
  const pctS = (v, d) => v == null ? "--" : (v >= 0 ? "+" : "") + Number(v).toFixed(d == null ? 2 : d) + "%";

  function paintContext(D) {
    const el = $("lv-context"); if (!el) return;
    const C = D.ctx; if (!C) { el.innerHTML = ""; return; }
    const open = new Set((D.live.trades || []).map(t => t.symbol));
    const rows = (C.symbols || []).filter(r => open.has(r.symbol) || r.symbol === "BTC/USDT" || r.symbol === "ETH/USDT").slice(0, 6);
    const fund = `<span class="c fund"><span class="sym">Funding</span> next in <b>${fmtDur(C.next_funding_sec)}</b><span class="sub">00 / 08 / 16 UTC</span></span>`;
    el.innerHTML = fund + rows.map(r => {
      const fr = r.funding_rate_8h_pct;
      return `<span class="c${open.has(r.symbol) ? " open" : ""}" title="${esc(r.symbol)}: mark ${px(r.mark)} · index ${px(r.index)} · 24h high ${px(r.high_24h)} low ${px(r.low_24h)}">
        <span class="sym">${esc(base(r.symbol))}</span><b>${px(r.mark || r.last)}</b>
        <span class="${cls(r.change_24h_pct)}">${pctS(r.change_24h_pct)}</span>
        <span>fund <b class="${fr > 0 ? "down" : fr < 0 ? "up" : ""}">${fr == null ? "--" : Number(fr).toFixed(4) + "%"}</b></span>
        <span>OI <b>${r.oi_value_usd != null ? "$" + fmtBig(r.oi_value_usd) : r.open_interest == null ? "--" : fmtBig(r.open_interest)}</b></span>
        <span>basis <b>${r.basis_pct == null ? "--" : pctS(r.basis_pct, 3)}</b></span></span>`;
    }).join("") + `<span class="push ${wsLive ? "on" : ""}" id="lv-push" title="Price and position frames pushed over the dashboard websocket; polling continues as the fallback">${wsLive ? "push live" : "polling"}</span>`;
  }

  function paintPosition(D) {
    const wrap = $("lv-position"), cnt = $("lv-pos-count"); if (!wrap) return;
    const rows = (D.live && D.live.trades && D.live.trades.length) ? D.live.trades : null;
    const maxOpen = window.LIVE_MAX_OPEN || 3;
    const n = rows ? rows.length : D.active.length;
    cnt.textContent = n + " of " + maxOpen + " slots";
    if (!n) { wrap.innerHTML = '<div class="empty">No open position. Next 5m close at ' + nextClose() + '.</div>'; return; }
    const byId = {}; D.active.forEach(t => { byId[t.trade_id] = t; });
    wrap.innerHTML = (rows || D.active.map(t => ({trade_id: t.trade_id, symbol: t.symbol, side: t.side, entry: t.entry_price, stop: t.stop_loss, tp1: t.tp1, tp2: t.tp2, mark: (D.status.prices || {})[t.symbol] || t.entry_price, leverage: t.leverage, margin_usd: t.paper_stake, notional_usd: t.position_size_usd, remaining_pct: 1, setup_type: t.setup_type, trade_type: t.trade_type, entry_time: t.entry_time, mfe_r: t.mfe_r}))).map(e => {
      const t = byId[e.trade_id] || {}, short = e.side === "short", dir = short ? -1 : 1, price = Number(e.mark || 0);
      const stop = e.effective_stop || e.stop;
      const levels = [e.stop, e.entry, e.tp1, e.tp2, price, stop, e.liq_price].filter(v => v > 0);
      const lo = Math.min(...levels), hi = Math.max(...levels), span = hi - lo || 1;
      const pos = v => ((v - lo) / span * 100).toFixed(1);
      const rung = (lab, p, extra) => p > 0 ? `<div class="rung"><span class="lab">${lab}</span><span class="bar"><i style="left:${pos(p)}%" class="${extra || ""}"></i><i style="left:${pos(price)}%" class="here"></i></span><span class="px">${px(p)}</span><span class="d">${r2((p - price) / price * 100 * dir)}%</span></div>` : "";
      const net = e.net_usd, roe = e.roe_pct, liqWarn = e.liq_dist_pct != null && e.liq_dist_pct < 1.0;
      const feeNote = e.exit_free_now ? `free exit for <b>${fmtDur(e.scalper_remaining_sec)}</b>` : (e.scalper_window_sec ? `exit taker &middot; window ${e.scalper_remaining_sec > 0 ? "open " + fmtDur(e.scalper_remaining_sec) : "closed"}` : "exit taker");
      const grid = e.net_usd == null ? "" : `
        <div class="posgrid">
          <div class="cell"><span class="eyebrow">Mark / index</span><span class="v">${px(e.mark)}</span><span class="s">idx ${e.index ? px(e.index) : "--"} &middot; basis ${e.basis_pct == null ? "--" : pctS(e.basis_pct, 3)}</span></div>
          <div class="cell ${liqWarn ? "bad" : "warn"}"><span class="eyebrow">Liquidation</span><span class="v">${px(e.liq_price)}</span><span class="s">${e.liq_dist_pct == null ? "--" : Math.abs(e.liq_dist_pct).toFixed(2) + "% away"}${e.liq_dist_atr != null ? " &middot; " + e.liq_dist_atr + " ATR" : ""} &middot; ${e.leverage}x isolated</span></div>
          <div class="cell"><span class="eyebrow">${e.trail ? "Trail" : "Stop"} distance</span><span class="v">${e.stop_dist_pct == null ? "--" : Math.abs(e.stop_dist_pct).toFixed(2) + "%"}</span><span class="s">${e.stop_dist_atr != null ? e.stop_dist_atr + " ATR &middot; " : ""}${e.breakeven_set ? "breakeven locked" : "initial risk"}</span></div>
          <div class="cell ${cls(e.r_now)}"><span class="eyebrow">R now</span><span class="v">${e.r_now == null ? "--" : r2(e.r_now) + "R"}</span><span class="s">MFE ${Number(e.mfe_r || 0).toFixed(2)}R &middot; MAE ${Number(e.mae_r || 0).toFixed(2)}R</span></div>
          <div class="cell"><span class="eyebrow">Margin / notional</span><span class="v">$${Number(e.margin_usd || 0).toFixed(0)} / $${Number(e.notional_usd || 0).toFixed(0)}</span><span class="s">${Math.round((e.remaining_pct || 1) * 100)}% open${e.tp1_hit ? " &middot; TP1 booked" : ""}</span></div>
          <div class="cell"><span class="eyebrow">Fees so far</span><span class="v">$${(Number(e.entry_fee_usd || 0) + Number(e.exit_fee_usd || 0)).toFixed(2)}</span><span class="s">entry ${esc(e.entry_liquidity || "")} $${Number(e.entry_fee_usd || 0).toFixed(2)} &middot; ${feeNote}</span></div>
          <div class="cell"><span class="eyebrow">Funding</span><span class="v">${e.funding_rate_8h_pct == null ? "--" : Number(e.funding_rate_8h_pct).toFixed(4) + "% / 8h"}</span><span class="s">est ${e.funding_est_usd == null ? "--" : money(e.funding_est_usd, true)} (not debited) &middot; next ${fmtDur(e.next_funding_sec)}</span></div>
          <div class="cell"><span class="eyebrow">Hold</span><span class="v">${fmtDur(e.hold_sec)}</span><span class="s">opened ${ist(e.entry_time)} IST &middot; ${esc(e.grade || "")} ${e.confidence != null ? Number(e.confidence).toFixed(0) : ""}</span></div>
        </div>
        <div class="pos-actions" data-tid="${esc(e.trade_id)}">
          <button class="rowbtn danger" data-act="close" title="Close the whole position at the current price">Close</button>
          <button class="rowbtn" data-act="half" title="Book half of what is still open at the current price; the rest keeps running" ${(e.remaining_pct || 1) <= 0.26 ? "disabled" : ""}>Close 50%</button>
          <button class="rowbtn" data-act="be" title="Move the stop to entry plus the taker round trip (only once price is beyond it)" ${e.breakeven_set ? "disabled" : ""}>Stop &rarr; BE</button>
          <span class="msg"></span>
        </div>`;
      return `<div class="pos">
        <div class="pos-head" data-trade='${esc(JSON.stringify(t))}' title="Click for detail"><span class="side ${esc(e.side)}">${esc(e.side)}</span><span class="sym">${esc(base(e.symbol))}</span><span class="meta">${esc(e.trade_type || "")} &middot; ${esc(String(e.setup_type || "").replace(/_/g, " "))} &middot; ${e.leverage || "--"}x</span><span class="pnl ${cls(net)}">${net == null ? "--" : money(net, true)}<span class="roe">${roe == null ? "" : "ROE " + pctS(roe, 1)}</span></span></div>
        <div class="ladder">${rung("Liq", e.liq_price, "liq")}${rung(e.trail ? "Trail" : "Stop", stop)}${rung("Entry", e.entry)}${rung("TP1", e.tp1)}${rung("TP2", e.tp2)}</div>
        ${grid}
      </div>`;
    }).join("");
    wrap.querySelectorAll(".pos-head").forEach(p => p.addEventListener("click", () => { try { openTradeDetail(JSON.parse(p.dataset.trade)); } catch (e) {} }));
    wrap.querySelectorAll(".pos-actions button").forEach(b => b.addEventListener("click", onAction));
  }

  async function onAction(ev) {
    const btn = ev.currentTarget, box = btn.closest(".pos-actions"), tid = box.dataset.tid, act = btn.dataset.act, msg = box.querySelector(".msg");
    const label = {close: "Close the whole position at the current price?", half: "Book 50% of the open position at the current price?", be: "Move the stop to breakeven (entry + fees)?"}[act];
    if (!confirm(label)) return;
    const url = act === "close" ? `/api/trade/${encodeURIComponent(tid)}/close` : act === "half" ? `/api/trade/${encodeURIComponent(tid)}/close-partial` : `/api/trade/${encodeURIComponent(tid)}/stop`;
    const body = act === "half" ? {fraction: 0.5} : act === "be" ? {breakeven: true} : {reason: "manual_close"};
    box.querySelectorAll("button").forEach(b => b.disabled = true); msg.textContent = "working…";
    try {
      const r = await fetch(url, {method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
      const d = await r.json().catch(() => ({}));
      if (r.status === 401) { alert("Session expired — log in again."); return; }
      msg.textContent = d.ok ? (act === "close" ? "closed" : act === "half" ? `booked, ${Math.round((d.remaining_pct || 0) * 100)}% left` : `stop ${px(d.new_sl)}`) : ("refused: " + (d.error || r.status));
      if (typeof showToast === "function") showToast((d.ok ? "OK: " : "Refused: ") + (d.error || act), d.ok ? "success" : "danger");
    } catch (e) { msg.textContent = "request failed"; }
    finally { setTimeout(refresh, 400); }
  }

  /* ── websocket push (2026-09-20): /api/ws, cookie-authenticated; polling stays as fallback ── */
  let wsLive = false, wsSock = null, wsRetry = 1000, tickTimer = null;
  function connectWS() {
    if (wsSock || !("WebSocket" in window)) return;
    try {
      wsSock = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/api/ws");
    } catch (e) { wsSock = null; return; }
    wsSock.onopen = () => { wsLive = true; wsRetry = 1000; const p = $("lv-push"); if (p) { p.className = "push on"; p.textContent = "push live"; } };
    wsSock.onclose = () => { wsLive = false; wsSock = null; const p = $("lv-push"); if (p) { p.className = "push"; p.textContent = "polling"; } setTimeout(connectWS, wsRetry); wsRetry = Math.min(wsRetry * 2, 30000); };
    wsSock.onerror = () => { try { wsSock.close(); } catch (e) {} };
    wsSock.onmessage = m => {
      let f; try { f = JSON.parse(m.data); } catch (e) { return; }
      if (!f || !f.channel) return;
      if (f.channel === "tick") {
        const D = lastGood; if (!D.status) return;
        D.status.prices = Object.assign(D.status.prices || {}, f.data.prices || {});
        if (D.live && D.live.trades) D.live.trades.forEach(t => { const mk = (f.data.marks || {})[t.symbol] || (f.data.prices || {})[t.symbol]; if (mk) t.mark = mk; });
        if (!tickTimer) tickTimer = setTimeout(() => { tickTimer = null; try { paintContext(D); if (D.live && D.live.trades.length && D.live.trades[0].net_usd == null) paintPosition(D); } catch (e) {} }, 500);
      } else if (f.channel === "position") {
        if (lastGood.live) { lastGood.live.trades = f.data.trades || []; try { paintPosition(lastGood); } catch (e) {} }
      } else if (f.channel === "event") {
        const d = f.data || {};
        if (typeof showToast === "function" && d.message) showToast(d.message, /close|hit|kill|expired|flatten/i.test(d.type || "") ? "warning" : "success");
        setTimeout(refresh, 300);
      }
    };
  }
  connectWS();

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
