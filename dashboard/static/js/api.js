/* ═══════════════════════════════════════════════════════════
   VN Edge API Layer v3.0
   Centralized fetch wrapper for all 37+ endpoints
   ═══════════════════════════════════════════════════════════ */

'use strict';

const API = {
  // ── Base fetch wrappers ──
  async get(path) {
    const r = await fetch(path, { credentials: 'same-origin' });
    if (!r.ok) return null;
    return r.json();
  },

  async post(path, body) {
    const r = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: body ? JSON.stringify(body) : undefined,
    });
    return r.json();
  },

  async put(path, body) {
    const r = await fetch(path, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(body),
    });
    return r.json();
  },

  // ── Auth ──
  session:    () => API.get('/api/session'),
  login:      (d) => API.post('/api/login', d),
  logout:     () => fetch('/api/logout', { method: 'POST', credentials: 'same-origin' }),
  register:   (d) => API.post('/api/register', d),

  // ── Core data (polled every 2-3s) ──
  status:         () => API.get('/api/status'),
  decision:       () => API.get('/api/decision'),
  trackerActive:  () => API.get('/api/tracker/active'),
  trackerClosed:  () => API.get('/api/tracker/closed'),
  trackerStats:   () => API.get('/api/tracker/stats'),
  signals:        () => API.get('/api/signals'),
  funnel:         () => API.get('/api/opportunity-funnel'),
  alerts:         () => API.get('/api/alerts'),

  // ── Real Trading ──
  realStatus:     () => API.get('/api/real/status'),
  emergencyStatus:() => API.get('/api/emergency-status'),
  lockReal75:     (d) => API.post('/api/real/lock_75', d),
  forceFlat:      () => API.post('/api/real/force_flat', {}),

  // ── Analytics ──
  scannerStats:   () => API.get('/api/scanner_stats'),
  aiInsights:     () => API.get('/api/ai_insights'),
  monitorReport:  () => API.get('/api/monitor_report'),
  exitQuality:    () => API.get('/api/exit-quality'),
  rMetrics:       () => API.get('/api/r_metrics'),
  regime:         () => API.get('/api/regime'),
  riskMetrics:    () => API.get('/api/risk-metrics'),
  riskReturn:     () => API.get('/api/risk-return'),
  sessionHeatmap: () => API.get('/api/session-heatmap'),

  // ── Pipeline / Agents ──
  pipelineOverview: () => API.get('/api/pipeline/overview'),
  pipelineJourney:  (id) => API.get(`/api/pipeline/journey/${id}`),
  stageStats:       (h, l) => API.get(`/api/pipeline/stage_stats?hours=${h || 24}&limit=${l || 50}`),
  rdrift:           () => API.get('/api/pipeline/rdrift'),
  hotfixStats:      () => API.get('/api/pipeline/hotfix_stats'),
  lossTaxonomy:     (h) => API.get(`/api/pipeline/loss_taxonomy?hours=${h || 24}`),
  agentsStatus:     () => API.get('/api/agents/status'),
  supervisorStatus: () => API.get('/api/supervisor/status'),

  // ── ML ──
  mlFamilyMatrix:   () => API.get('/api/ml/family-verdict-matrix'),
  mlCalibration:    () => API.get('/api/ml/live-calibration'),
  mlVerdictTrend:   () => API.get('/api/ml/edge-verdict-trend'),
  mlHealth:         () => API.get('/api/ml/health'),
  mlOOSMetrics:     () => API.get('/api/ml/oos-metrics'),

  // ── Infrastructure ──
  infra:            () => API.get('/api/infra'),
  infraHealth:      () => API.get('/api/infra/health'),
  ping:             () => API.get('/api/ping'),
  latency:          () => API.get('/api/latency'),

  // ── Grid Bot ──
  gridStatus:       () => API.get('/api/grid/status'),
  gridPositions:    () => API.get('/api/grid/positions'),

  // ── Latency Arb ──

  // ── Config ──
  config:       () => API.get('/api/config'),
  configSave:   (d) => API.post('/api/config', d),

  // ── BotBrain ──
  brainState:     () => API.get('/api/brain/state'),
  brainMatrix:    () => API.get('/api/brain/matrix'),
  brainHourly:    () => API.get('/api/brain/hourly-heatmap'),
  brainRegime:    () => API.get('/api/brain/regime-history'),
  brainSessions:  () => API.get('/api/brain/sessions'),

  // ── User ──
  userProfile:      () => API.get('/api/user/profile'),
  userProfileSave:  (d) => API.put('/api/user/profile', d),
  userApiKeys:      () => API.get('/api/user/api-keys'),
  userApiKeyCreate: (d) => API.post('/api/user/api-keys', d),
  userApiKeyDelete: (id) => fetch(`/api/user/api-keys/${id}`, { method: 'DELETE', credentials: 'same-origin' }),

  // ── Control ──
  controlPause:   () => fetch('/api/control/pause', { method: 'POST', credentials: 'same-origin' }),
  controlResume:  () => fetch('/api/control/resume', { method: 'POST', credentials: 'same-origin' }),
};

window.API = API;
