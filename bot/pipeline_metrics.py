"""Pipeline observability metrics — Phase 0.

Lightweight aggregator that counts pipeline events with zero hot-path impact.
Reads from existing counters (scalp._funnels, circuit_breaker, real_manager)
and exposes a unified dashboard-ready snapshot.

Exposes:
  - Funnel counts (scanned -> strategy -> paper -> real_qualify -> executed)
  - Rejection leaderboard (top reject reasons across real side)
  - Agent heartbeats (last activity timestamp per component)

Usage from dashboard:
    from bot.pipeline_metrics import get_metrics
    snapshot = get_metrics(strategy=..., real_manager=..., signal_tracker=...,
                           orchestrator=...)

Thread-safety: all mutations are dict operations (GIL-protected in CPython).
No locks needed for simple counter increments.
"""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from typing import Any, Dict, Optional

# ──────────────────────────────────────────────────────────────────────
# In-memory counters (module-level — survives across dashboard requests)
# ──────────────────────────────────────────────────────────────────────

# Real-side qualification: {reason: count} — counts qualification rejections by reason
_real_reject_counter: Counter = Counter()
_real_pass_counter: int = 0

# Executor: {event: count} — entry, exit, anti-slip, emergency etc.
_exec_counter: Counter = Counter()

# Agent heartbeats: {component: last_activity_timestamp}
_heartbeats: Dict[str, float] = {}

# Day-boundary tracking for rolling 24h reset
_day_start: float = time.time()
_TODAY_SEC: int = 86400


def _maybe_reset_daily() -> None:
    """Reset counters at day boundary (24h rolling)."""
    global _day_start
    now = time.time()
    if now - _day_start > _TODAY_SEC:
        _real_reject_counter.clear()
        _exec_counter.clear()
        globals()['_real_pass_counter'] = 0
        _day_start = now


# ──────────────────────────────────────────────────────────────────────
# Record API (called by instrumented code paths)
# ──────────────────────────────────────────────────────────────────────

def record_real_reject(reason: str) -> None:
    """Record a real-side qualification rejection.
    Called from real_manager.smart_qualify() fail paths."""
    _maybe_reset_daily()
    # Normalize reason: strip dynamic values (e.g. '$16.23' -> just the key)
    norm = reason.split(':')[0].strip() if ':' in reason else reason
    norm = norm.split('$')[0].strip() if '$' in norm else norm
    _real_reject_counter[norm[:32]] += 1


def record_real_pass() -> None:
    """Record a real-side qualification pass."""
    _maybe_reset_daily()
    globals()['_real_pass_counter'] = _real_pass_counter + 1


def record_exec_event(event: str) -> None:
    """Record an executor event: bracket_entry, anti_slip_close,
    independent_exit, mirror_exit, emergency_close, etc."""
    _maybe_reset_daily()
    _exec_counter[event[:32]] += 1


def heartbeat(component: str) -> None:
    """Record activity from a component. Called every tick/iteration."""
    _heartbeats[component] = time.time()


# ──────────────────────────────────────────────────────────────────────
# Snapshot API (called by dashboard endpoint)
# ──────────────────────────────────────────────────────────────────────

def get_snapshot(
    strategy: Optional[Any] = None,
    real_manager: Optional[Any] = None,
    signal_tracker: Optional[Any] = None,
    orchestrator: Optional[Any] = None,
) -> Dict[str, Any]:
    """Build a complete pipeline snapshot.

    Reads from existing counters on passed-in components to avoid
    duplicating state. Returns a dashboard-ready dict.
    """
    _maybe_reset_daily()
    now = time.time()

    # ── Funnel: aggregate from scalp._funnels ──
    funnel = {
        'scanned': 0,
        'tier_valid': 0,
        'tier_near_miss': 0,
        'rejected_strategy': 0,
        'blocked_regime': 0,
        'paper_emitted': 0,
        'real_qualified': _real_pass_counter,
        'real_rejected': sum(_real_reject_counter.values()),
        'real_executed': _exec_counter.get('bracket_entry', 0),
        'real_anti_slip_rejected': _exec_counter.get('anti_slip_close', 0),
    }

    if strategy is not None:
        scalp = getattr(strategy, '_scalp', None) or strategy
        funnels = getattr(scalp, '_funnels', None)
        if funnels:
            for sym_data in funnels.values():
                if not isinstance(sym_data, dict):
                    continue
                funnel['scanned'] += sym_data.get('scanned', 0)
                funnel['tier_valid'] += sym_data.get('valid', 0)
                funnel['tier_near_miss'] += sym_data.get('near_miss', 0)
                funnel['rejected_strategy'] += sym_data.get('rejected', 0)
                funnel['blocked_regime'] += sym_data.get('blocked_regime', 0)
                funnel['paper_emitted'] += sym_data.get('premium', 0) + sym_data.get('valid', 0)

    # ── Rejection leaderboard ──
    reject_top = [
        {'reason': r, 'count': c}
        for r, c in _real_reject_counter.most_common(8)
    ]

    # ── Circuit breaker state ──
    cb_state = {}
    if real_manager is not None:
        cb = getattr(real_manager, 'circuit_breaker', None)
        if cb is not None:
            cb_state = {
                'is_tripped': getattr(cb, 'is_tripped', False),
                'consecutive_losses': getattr(cb, 'consecutive_losses', 0),
                'daily_pnl': round(getattr(cb, 'daily_pnl', 0.0), 2),
                'daily_loss_limit': getattr(cb, 'daily_loss_limit', 15.0),
                'trip_reason': getattr(cb, 'trip_reason', ''),
            }

    # ── Agent heartbeats ──
    agents = {}
    for comp, ts in _heartbeats.items():
        age = now - ts
        status = 'ok' if age < 10 else ('stale' if age < 60 else 'dead')
        agents[comp] = {
            'last_seen_sec': round(age, 1),
            'status': status,
        }

    # Auto-derive a few heartbeats from orchestrator state
    if orchestrator is not None:
        # Signal tracker activity inferred from active count change
        try:
            active = getattr(signal_tracker, 'active_count', 0) if signal_tracker else 0
            agents.setdefault('signal_tracker', {
                'last_seen_sec': 0,
                'status': 'ok' if active >= 0 else 'unknown',
                'active_trades': active,
            })
        except Exception:
            pass

        # Real manager
        try:
            if real_manager is not None:
                opens = len(getattr(real_manager, 'real_trades', {}) or {})
                enabled = getattr(real_manager, 'enabled', False)
                agents.setdefault('real_manager', {
                    'last_seen_sec': 0,
                    'status': 'ok' if enabled else 'disabled',
                    'open_real_trades': opens,
                })
        except Exception:
            pass

    return {
        'funnel': funnel,
        'rejections': {
            'top': reject_top,
            'total_today': sum(_real_reject_counter.values()),
            'passes_today': _real_pass_counter,
        },
        'execution': dict(_exec_counter),
        'circuit_breaker': cb_state,
        'agents': agents,
        'generated_at': now,
        'uptime_sec': round(now - _day_start, 0),
    }
