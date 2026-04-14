/* ═══════════════════════════════════════════════════════════
   VN Edge Core v3.0
   Auth, tab switching, timer orchestration, global state, utilities
   ═══════════════════════════════════════════════════════════ */

'use strict';

// ── Global State Namespace ──
window.VNE = {
  activeTab: 'live',
  timers: {
    live: null, analytics: null, system: null,
    latency: null, agents: null, brain: null,
  },

  // Data caches
  cachedPrices: {},
  prevRealTotalPnl: null,
  equityHistory: [],
  EQUITY_HISTORY_MAX: 720,
  closedTradesCache: [],
  signalFilter: 'ALL',
  activeTradeTab: 'paper',
  currentClosedTab: 'paper',
  scannerView: 'paper',
  scannerDataPaper: [],
  scannerDataReal: [],
  lastRealStatus: null,
  laSelectedSymbol: 'BTC/USDT',

  // Window selectors
  slWindowHours: 4,
  ltWindowHours: 24,
  rdWindowHours: 24,
  eqRange: 300,

  // Chart instances
  charts: {
    equity: null,
    dailyPnl: null,
    deploySpark: null,
    radar: null,
    scannerPies: [null, null, null],
    eqCurve: null,
    rHist: null,
    divergence: null,
  },

  // Symbol color map
  SYMBOL_COLORS: {
    'BTC/USDT':'#f7931a', 'ETH/USDT':'#627eea', 'SOL/USDT':'#9945ff',
    'XRP/USDT':'#00aae4', 'AVAX/USDT':'#e84142', 'LINK/USDT':'#2a5ada',
    'DOGE/USDT':'#c3a634', 'LTC/USDT':'#bfbbbb', 'ADA/USDT':'#0033ad',
    'DOT/USDT':'#e6007a', 'TAO/USDT':'#00c4b3', 'SUI/USDT':'#4da2ff',
    'WIF/USDT':'#8b5cf6', 'NEAR/USDT':'#00ec97', 'PEPE/USDT':'#4ca843',
    'SHIB/USDT':'#ffa409', 'BONK/USDT':'#f9a602',
  },

  SCANNER_COLORS: {
    structure_bounce: '#00d4ff', ema_momentum: '#ff8c00', vwap_bounce: '#a78bfa',
    rsi_divergence: '#ff3b5c', liquidity_sweep: '#ffd700', bos_choch: '#00ff9d',
    cvd_divergence: '#ff69b4', trend_continuation: '#4ecdc4', order_block_entry: '#9b59b6',
  },
};

// ── Utility Functions ──
function formatTime(isoStr) {
  if (!isoStr) return '--';
  try {
    const d = new Date(isoStr);
    const day = d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', timeZone: 'Asia/Kolkata' });
    const time = d.toLocaleTimeString('en-IN', { hour12: false, hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Kolkata' });
    return `${day} ${time}`;
  } catch { return isoStr; }
}

function pnlClass(v) { return v > 0 ? 'pnl-pos' : v < 0 ? 'pnl-neg' : 'pnl-zero'; }
function pnlSign(v) { return v > 0 ? '+' + v.toFixed(2) : v.toFixed(2); }
function pct(v) { return v != null ? (v * 100).toFixed(1) + '%' : '--'; }
function num(v, d = 2) { return v != null ? Number(v).toFixed(d) : '--'; }
function esc(s) { const el = document.createElement('span'); el.textContent = s; return el.innerHTML; }

function timeSince(isoStr) {
  if (!isoStr) return '--';
  const ms = Date.now() - new Date(isoStr).getTime();
  if (ms < 0) return '--';
  const mins = Math.floor(ms / 60000);
  if (mins < 60) return mins + 'm';
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return hrs + 'h ' + (mins % 60) + 'm';
  return Math.floor(hrs / 24) + 'd ' + (hrs % 24) + 'h';
}

function healthColor(pct) {
  if (pct > 85) return 'var(--danger)';
  if (pct > 70) return 'var(--warning)';
  return 'var(--success)';
}

function makeHealthRow(label, value, unit, max) {
  const p = max ? (value / max * 100) : value;
  return `<div class="mb-3">
    <div class="flex justify-between text-sm mb-1">
      <span class="text-secondary">${label}</span>
      <span class="font-bold font-mono">${num(value, 1)}${unit}</span>
    </div>
    <div class="progress-bar"><div class="progress-bar__fill" style="width:${Math.min(p, 100)}%;background:${healthColor(p)}"></div></div>
  </div>`;
}

function makeKV(label, value, color) {
  const c = color ? `color:${color}` : '';
  return `<div class="data-row">
    <span class="data-row__key">${label}</span>
    <span class="data-row__value" style="${c}">${value}</span>
  </div>`;
}

// ── Quick DOM helper ──
function el(id) { return document.getElementById(id); }

// ── Toast notification ──
function showToast(msg, type) {
  const existing = document.querySelector('.toast');
  if (existing) existing.remove();
  const t = document.createElement('div');
  t.className = 'toast' + (type === 'success' ? ' toast--success' : type === 'error' ? ' toast--danger' : '');
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 4000);
}

// ── Legacy api() wrapper (kept for backward compat with inline JS) ──
async function api(path) {
  try {
    const r = await fetch(path, { credentials: 'same-origin' });
    if (!r.ok) return null;
    return await r.json();
  } catch { return null; }
}
