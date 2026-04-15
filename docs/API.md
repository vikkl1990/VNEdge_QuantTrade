# VN Edge API Reference

All endpoints return JSON. Session cookie `vn_session` required for write operations.
GET endpoints are public (read-only dashboard data). POST/PUT/DELETE require auth.

## Authentication

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/api/login` | POST | Public | `{email, password}` → sets session cookie |
| `/api/logout` | POST | User | Clear session |
| `/api/register` | POST | Public | `{email, password, full_name}` |
| `/api/session` | GET | Public | Current session info |

## User

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/api/user/profile` | GET/PUT | User | Personal profile + settings |
| `/api/user/api-keys` | GET/POST | User | Manage own Delta keys |
| `/api/user/api-keys/{id}` | DELETE | User | Remove key |
| `/api/user/settings` | GET/PUT | User | Trading preferences |
| `/api/user/dashboard` | GET | User | Combined paper + own real trades |

## Per-User Real Trading

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/api/user/real/status` | GET | User | Balance, positions, CB |
| `/api/user/real/toggle` | POST | User | Switch paper/demo/live |
| `/api/user/real/trades` | GET | User | Closed trade history |
| `/api/user/real/config` | GET/PUT | User | Leverage, loss limits, pairs |
| `/api/user/real/cb-reset` | POST | User | Reset own circuit breaker |
| `/api/user/strategies` | GET/POST | User | Named strategy configs |

## 2FA

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/api/2fa/status` | GET | User | Check enabled |
| `/api/2fa/setup` | POST | User | Generate TOTP secret + QR URI |
| `/api/2fa/verify` | POST | User | Verify code + enable |
| `/api/2fa/disable` | POST | User | Disable 2FA |

## Email Verification

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/api/email/send-verify` | POST | User | Generate verification link |
| `/api/email/verify?token=X` | GET | Public | Verify email via token |
| `/api/email/status` | GET | User | Check verified status |

## Admin

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/api/admin/users` | GET | Admin | List all users with full config |
| `/api/admin/users/create` | POST | Admin | Create new user |
| `/api/admin/users/{id}` | PUT/DELETE | Admin | Update/delete user |
| `/api/admin/users/{id}/reset-password` | POST | Admin | Reset user password |
| `/api/admin/users/{id}/api-keys` | GET/POST | Admin | Manage user's keys |
| `/api/admin/api-keys/{id}` | DELETE | Admin | Remove any user's key |
| `/api/admin/sessions` | GET | Admin | Active sessions |
| `/api/admin/sessions/{token}` | DELETE | Admin | Force logout |
| `/api/admin/audit` | GET | Admin | Login history |
| `/api/admin/real-overview` | GET | Admin | All users' real PnL |
| `/api/admin/user-trades/{id}` | GET | Admin | Drill into user's trades |

## Trading Data (Public GET)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/status` | GET | Bot status |
| `/api/tracker/active` | GET | Active paper trades |
| `/api/tracker/closed` | GET | Closed paper trades |
| `/api/tracker/stats` | GET | Paper trading stats |
| `/api/signals` | GET | Recent signals |
| `/api/decision` | GET | Current decision |
| `/api/regime` | GET | Market regime |
| `/api/real/status` | GET | Global real trading status |
| `/api/opportunity-funnel` | GET | Signal pipeline funnel |
| `/api/scanner_stats` | GET | Per-scanner stats |
| `/api/r_metrics` | GET | R-multiple metrics |
| `/api/exit-quality` | GET | Exit quality stats |
| `/api/ai_insights` | GET | SignalLearner insights |
| `/api/monitor_report` | GET | TradeMonitor streak/drawdown |

## BotBrain

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/brain/state` | GET | Memory + optimizer state |
| `/api/brain/matrix` | GET | Setup × regime heatmap |
| `/api/brain/regime-history` | GET | Per-symbol regime timeline |
| `/api/brain/hourly-heatmap` | GET | Hourly performance |
| `/api/brain/sessions` | GET | Daily/weekly summaries |

## Backtester + Analytics

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/api/backtest/run` | POST | User | Run backtest on historical data |
| `/api/backtest/history` | GET | User | Past backtest results |
| `/api/attribution` | GET | User | PnL by scanner/regime/grade |
| `/api/replay/trade/{id}` | GET | User | Replay specific trade |

## Control

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/api/control/pause` | POST | User | Pause trading |
| `/api/control/resume` | POST | User | Resume |
| `/api/real/toggle` | POST | User | Toggle global real |
| `/api/real/cb-reset` | POST | User | Reset global CB |
| `/api/real/lock_75` | POST | User | Lock 75% of position |
| `/api/real/force_flat` | POST | User | Close all real positions |
| `/api/emergency-stop` | POST | User | Kill all trading |

## Pipeline Observability

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/pipeline/overview` | GET | Pipeline summary |
| `/api/pipeline/journey/{trade_id}` | GET | Per-signal trace |
| `/api/pipeline/stage_stats` | GET | Stage performance |
| `/api/pipeline/rdrift` | GET | Real vs paper drift |
| `/api/pipeline/hotfix_stats` | GET | Hotfix veto counts |
| `/api/pipeline/loss_taxonomy` | GET | Loss categorization |
| `/api/agents/status` | GET | All agents health |
| `/api/supervisor/status` | GET | Supervisor status |

## ML

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/ml/family-verdict-matrix` | GET | Per-family ML edge |
| `/api/ml/live-calibration` | GET | Calibration curve |
| `/api/ml/edge-verdict-trend` | GET | Verdict trend over time |
| `/api/ml/health` | GET | Model health per scanner |

## Infrastructure

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/ping` | GET | Health check |
| `/api/infra` | GET | Infra stats |
| `/api/infra/health` | GET | Detailed health |
| `/api/latency` | GET | Latency metrics |
| `/api/grid/status` | GET | Grid bot status |
| `/api/config` | GET/POST | Settings editor (hot-reload) |

**Total: 90+ endpoints**
