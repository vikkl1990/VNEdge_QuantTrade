"""
UserRealManager — Per-user real trading execution for VN Edge.

Each user gets their own manager instance with:
- Their own Delta exchange API keys (decrypted from DB)
- Their own risk limits (leverage, daily loss, position size)
- Their own circuit breaker (independent from other users)
- Their own trade monitoring loops

Paper trading is SHARED (global signal engine). Real trading is PER-USER.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# Phase 5.8 (2026-04-25) — Unified dead-signal guard.
# Replaces the quick_kill / no_proof_of_life / early_kill / zombie_kill
# cascade with a single fee-floor + ATR-grace + patience function. See
# docs/EXIT_GUARD_REFACTOR_5_8.md for the full design.
from execution.exit_guards import should_kill_dead_signal


# ─────────────────────────────────────────────────────────────────────────
# Phase 2 — Shadow-of-Shadow forward test exit configurations.
# When PHASE2_SOS_ENABLED env var is true AND user is in shadow_live mode,
# every paper signal spawns N virtual trades — one per config — each with
# its own _monitor_trade task. PnL per config emerges from REAL price
# evolution (no approximation). After 24-48h, leaderboard via
# scripts/phase2_leaderboard.py reveals which exit profile preserves
# the most paper edge.
# See: docs/PHASE2_SHADOW_OF_SHADOW_DESIGN.md
# Each config dict:
#   id            — unique identifier (string)
#   max_age_sec   — hard time-decay close threshold
#   trail_trigger — peak_mfe_r required to engage BE+lock
#   trail_lock    — fraction of peak to lock as new SL
#   dead_kill_R   — UNIFIED_KILL_CURRENT_R override (None = disabled)
#   stall_kill_R  — STALL_CURRENT_R override (None = disabled)
#   tp_R          — exit at +N×R if peak reaches it (None = no TP)
PHASE2_EXIT_CONFIGS = [
    {"id": "primary",          "max_age_sec":  600, "trail_trigger": 0.5, "trail_lock": 0.80,
     "dead_kill_R": -0.10, "stall_kill_R": -0.05, "tp_R": None},
    {"id": "v1_5min_tight",    "max_age_sec":  300, "trail_trigger": 0.5, "trail_lock": 0.80,
     "dead_kill_R": -0.10, "stall_kill_R":  None, "tp_R": None},
    {"id": "v2_10min_no_kill", "max_age_sec":  600, "trail_trigger": 0.5, "trail_lock": 0.80,
     "dead_kill_R":  None, "stall_kill_R":  None, "tp_R": None},
    {"id": "v3_30min_paper",   "max_age_sec": 1800, "trail_trigger": 0.3, "trail_lock": 0.50,
     "dead_kill_R":  None, "stall_kill_R":  None, "tp_R": None},
    {"id": "v4_60min_unrest",  "max_age_sec": 3600, "trail_trigger": 0.7, "trail_lock": 0.80,
     "dead_kill_R":  None, "stall_kill_R":  None, "tp_R": 2.0},
    # 2026-04-28 Path 2 — `v6_tp_15R` from exit_variant_backtest.
    # Backtest signal: hard TP at +1.5R produced +5.2% Net uplift vs primary
    # (1101 vs 1047 over 714 historical signals) AND better PF (1.79 vs 1.75)
    # AND better MaxDD (-$23 vs -$25). Strict ship rule was ≥10%, but variant
    # is strictly Pareto-better on PF + MaxDD with positive Net delta — worth
    # forward-validating. tp_R=1.5 caps the ~52 trades that would otherwise
    # reverse past peak. Same kill/trail config as primary; only TP cap differs.
    {"id": "v6_tp_15R",        "max_age_sec":  600, "trail_trigger": 0.5, "trail_lock": 0.80,
     "dead_kill_R": -0.10, "stall_kill_R": -0.05, "tp_R": 1.5},
    # 2026-04-28 — v7_scratch_02R: SCRATCH-PROFIT variant.
    # Empirical loss audit (last 6h, 18 losing primary trades): peak_mfe_r
    # distribution = 0.00–0.25R for every single losing trade. None reached
    # the 0.5R trail engagement. Pattern: enter → small adverse → drift back
    # near entry → time-out at -$1.50 (mostly fees) at 10min cap.
    # Hypothesis: lower trail_trigger to 0.20R (catches the 0.20-0.25R peaks
    # before they retrace) AND keep trail_lock 0.80 (lock 0.16R = small
    # scratch profit ≈ +$0.10-0.30 net after fees). Captures the regime
    # backtest engine doesn't model well but loss-attribution clearly shows.
    # If forward Phase 2 leaderboard ranks v7 above primary, promote.
    {"id": "v7_scratch_02R",   "max_age_sec":  600, "trail_trigger": 0.2, "trail_lock": 0.80,
     "dead_kill_R": -0.10, "stall_kill_R": -0.05, "tp_R": None},
]

logger = logging.getLogger("execution.user_real")


# Phase 5.20-A1 (2026-04-25) — UUID-based client_order_id helper.
# Audit found prior `int(time.time()*1000) % 10**10` collides on bursts
# of orders <100ms apart (Delta silently rejects duplicates → trade dropped).
# Uses uuid4 hex truncated to 30 chars (Delta limit is 32).
def _new_coid(prefix: str = "vn") -> str:
    """Generate guaranteed-unique client_order_id (max 32 chars).

    Format: {prefix}_{14-char-hex}  (default 17 chars total)
    """
    return f"{prefix[:6]}_{uuid.uuid4().hex[:24]}"[:32]


# Phase 5.20-FIX3 (2026-04-25) — single source of truth for current phase tag.
# Audit found two hardcoded phase strings ("5.5.3" and "5.12") that were
# wildly out of date — every trade for hours had wrong phase metadata,
# corrupting cohort analysis. Now bump this single constant per release.
CURRENT_PHASE = "5.8"


@dataclass
class UserCircuitBreaker:
    """Per-user circuit breaker — independent from global CB."""
    daily_loss_limit: float = 25.0
    consecutive_losses: int = 0
    daily_loss_usd: float = 0.0
    trade_count_today: int = 0
    total_pnl: float = 0.0
    last_reset_date: str = ""

    @property
    def is_tripped(self) -> bool:
        return (
            self.consecutive_losses >= 3
            or self.daily_loss_usd <= -self.daily_loss_limit
        )

    def record_trade(self, pnl: float):
        if pnl > 0:
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1
        self.daily_loss_usd += pnl
        self.total_pnl += pnl
        self.trade_count_today += 1

    def check_daily_reset(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.last_reset_date:
            self.daily_loss_usd = 0.0
            self.trade_count_today = 0
            self.consecutive_losses = 0
            self.last_reset_date = today

    def reset(self):
        self.consecutive_losses = 0
        self.daily_loss_usd = 0.0
        self.trade_count_today = 0


@dataclass
class UserTradeRecord:
    """One open real trade for a user."""
    trade_id: str
    user_id: str
    symbol: str
    side: str
    entry_price: float
    stop_loss: float
    take_profit: float = 0.0
    position_size: int = 0
    margin: float = 0.0
    leverage: int = 1
    opened_at: float = 0.0
    peak_mfe_r: float = 0.0
    highest_price: float = 0.0
    lowest_price: float = 0.0
    initial_risk: float = 0.0
    scanner: str = ""
    grade: str = ""
    trade_type: str = "SCALP"
    # Phase 4.0 (2026-04-22) — paper-parity fields
    product_id: int = 0                    # Delta product id (mode-resolved)
    tick_size: float = 0.01                # for trail SL rounding
    server_stop_id: Optional[int] = None   # server-side stop_loss_order id (Delta)
    fee_type: str = "taker"                # tier used for entry (maker|taker)
    # Phase 4.1 (2026-04-22) — regime + dead_market support
    regime: str = ""                       # market regime at entry (quiet|trending|mean_reversion|…)
    ml_prob: float = 0.0                   # ML probability at entry (for reporting)
    # Phase 4.6 (2026-04-22) — accurate PnL accounting
    contract_size: float = 1.0             # units of underlying per lot (e.g. BTC=0.001, SOL=1.0, SHIB=1000)
    entry_fee_usd: float = 0.0             # fee paid at entry (from Delta commission)
    # Phase 5.3 / T4.1 (2026-04-23) — funding accounting
    # Funding on Delta India fires every 8h (IST 05:30 / 13:30 / 21:30).
    # Long pays positive rate, short collects; sign convention: stored as
    # USD *subtracted* from net for the long (i.e. funding_usd > 0 is a cost).
    # Demo/testnet typically has zero funding; captured here so live flip
    # automatically accounts for it. Computed at close in _close_trade.
    funding_rate_at_entry: float = 0.0     # 8-hour rate snapshot at entry
    funding_usd: float = 0.0               # accrued cost in USD at close


# Phase 4.0 — Trade-type-aware early-kill (paper parity, see signal_tracker.py:53-84)
# Paper proved the right tuning for SCALP/INTRADAY; demo had a flat 0.05R/60s
# which killed profitable trades before they could develop.
_EARLY_KILL = {
    "SCALP":    (60, 0.10),   # 60s, peak < 0.10R
    "INTRADAY": (90, 0.08),   # 90s, peak < 0.08R
    "RUNNER":   (0,  0.0),    # disabled
}

# Phase 4.1 — Paper also SKIPS early_kill on A+/A grade (signal_tracker.py:1746-1748,
# "best signals should not be killed"). Our Phase 4.0 port killed them, which
# in observed 04:22 data closed 6/6 A+ SOL/ETH/BTC signals at −0.03 to −0.51
# without letting any reach trail territory. Compromise: A+/A get a longer
# leash but not a full pass — protects against true duds while letting the
# actual A+ runners develop.
_EARLY_KILL_HIGHGRADE = (120, 0.06)  # (sec, peak_r) for A+/A


class UserRealManager:
    """Per-user real trading execution using the user's own API keys.

    Instantiated by UserRealRegistry when a user has active API keys
    and bot_mode != 'paper'. Each instance manages its own exchange
    connection, circuit breaker, open trades, and monitoring loops.
    """

    def __init__(
        self,
        user_id: str,
        user_email: str,
        user_config: Dict[str, Any],
        delta_client: Any,
        db_pool: Any = None,
    ):
        self.user_id = user_id
        self.user_email = user_email
        self._delta = delta_client
        self._db_pool = db_pool

        # User-specific risk limits
        self.max_leverage = int(user_config.get("max_leverage", 20))
        self.max_daily_loss = float(user_config.get("max_daily_loss_usd", 25))
        self.max_position_notional = float(user_config.get("max_position_notional", 500))
        self.preferred_symbols = list(user_config.get("preferred_symbols") or user_config.get("trading_pairs") or [])
        self.min_confidence = float(user_config.get("min_confidence", 55))
        # Phase 4.5 (2026-04-22) — ml_threshold 0.60 → 0.55.
        # Observed 3 paper trail_profit wins today on B/C-grade signals
        # with ML prob 0.55-0.59. Lowering floor catches them; G7 canary
        # guard enforces rollback if WR degrades at n≥6.
        self.ml_threshold = float(user_config.get("ml_threshold", 0.55))
        self.size_multiplier = float(user_config.get("size_multiplier", 1.0))
        self.max_daily_trades = int(user_config.get("max_daily_trades", 15))
        self.enabled = user_config.get("bot_mode", "paper") != "paper"
        # Phase 5.14 (2026-04-25) — maker patience mode (per-user A/B).
        # Values: 'standard' (current behavior) | 'patient' (5x probe) | 'aggressive' (10x probe)
        # Driven by users.maker_patience_mode column (added in migration 007).
        self.maker_patience_mode = str(
            user_config.get("maker_patience_mode") or "standard"
        ).lower()

        # Phase 6.C Lever 1 — shadow simulated balance override (re-added 2026-04-26
        # after lever3 rollback dropped it; symptom was niranjan sized 5-7× admin
        # because compute_size fell back to real Delta wallet balance per user).
        # If set AND _is_shadow_live, compute_size uses this instead of _cached_balance
        # so all shadow users size off a comparable simulated bankroll.
        _ssb = user_config.get("shadow_simulated_balance")
        try:
            self.shadow_simulated_balance = float(_ssb) if _ssb is not None else None
        except (TypeError, ValueError):
            self.shadow_simulated_balance = None

        # Circuit breaker
        self.cb = UserCircuitBreaker(daily_loss_limit=self.max_daily_loss)

        # Trade tracking
        self.open_trades: Dict[str, UserTradeRecord] = {}
        self.closed_trades: List[Dict] = []
        self._cached_balance: float = 0.0
        # 2026-04-27 — keep strong references to monitor tasks to prevent
        # asyncio garbage-collecting them before they run (Python GC bug
        # asyncio.create_task() doesn't store its own reference; tasks
        # without external refs may be collected mid-flight). Symptom:
        # monitor_trade fires for some new trades but not others →
        # those drift until Agent 9-A's 60min sweep as auto_responder_stuck_60m
        # / PnL=$0. Particularly visible on Phase 2 fan-out where 5
        # tasks spawn within microseconds. Cleared on close_trade.
        # See https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task
        self._monitor_tasks: Dict[str, "asyncio.Task"] = {}
        # Phase 5.20-A2 (2026-04-25) — atomic mutex on open_trades.
        # Audit found concurrent qualify_signal + monitor + close paths
        # mutate this dict without locking. Race scenarios:
        #   - Two near-simultaneous signals both pass duplicate-check
        #     because each reads len(open_trades)<3 before either inserts
        #   - Monitor reads trade just as close removes it → KeyError
        #   - Reconciliation insert races with execute_signal insert
        # Solution: serialize ALL open_trades mutations via this lock.
        # Performance: lock held <100us; signal flow is ~1/sec, no bottleneck.
        self._open_trades_lock: asyncio.Lock = asyncio.Lock()
        # Phase 5.20-A2 — track recent (symbol, side, signal_id_hash) to dedup
        # multi-scanner signals on the same bar. Audit raised theoretical risk;
        # last 7d data showed zero duplicates, but guard is cheap insurance.
        # 60s TTL covers a 5m bar plus margin.
        self._recent_signal_keys: Dict[str, float] = {}  # key → unix_ts

        # Price feed reference (set by registry from orchestrator)
        self._price_feed = None

        # Phase 4.1 — cohort hard-veto state (Lever 2 from edge framework).
        # Blacklist: (symbol, side) cohorts where the last 5 closed real trades
        # in the past 7 days ALL lost. Refreshed from DB every 5 min.
        self._cohort_blacklist: set = set()
        self._cohort_refresh_ts: float = 0.0
        self._cohort_ttl_sec: float = 300.0

        # Phase 5.0 (2026-04-22) — LIVE-MODE safety constants.
        # When mode=live the same Phase 4.x/5.0 logic runs, but we enforce
        # a few extra guards around real money:
        #   - BALANCE_FLOOR_LIVE: skip trade if wallet < this (fees dominate)
        #   - Loud logging: every ENTRY/EXIT emits CRITICAL-level marker
        self._bot_mode = str(user_config.get("bot_mode", "paper"))
        self._is_live = (self._bot_mode == "live")
        # Phase 5.6-B (2026-04-24) — T4.3 SHADOW-LIVE mode.
        # Shadow-live executes the FULL pipeline (qualify → size → monitor →
        # exits) against PRODUCTION prices + L2 orderbook data, but NEVER
        # places real orders on Delta. Every "fill" and "close" is simulated
        # from live L2 top-of-book. This is the mandatory validation gate
        # before flipping bot_mode='live'. Records go to user_trades with
        # trade_type='shadow' and metadata.is_shadow=true.
        self._is_shadow_live = (self._bot_mode == "shadow_live")
        self._live_balance_floor_usd = 20.0   # don't trade live if balance < $20

        # 2026-04-27 CLEAN A/B TEST setup — architect directive:
        #   Paper - as is
        #   Delta - admin shadow_live + niranjan shadow_live, MERGE ALL (same code)
        #   open ALL signals as paper to delta (bypass qualify_signal filter)
        #   Bybit - PAUSE (services stopped)
        # → Both users on STANDARD exit guards (no niranjan treatment).
        # → Both users get every paper signal mirrored to delta_shadow.
        # → Compares paper PnL vs delta_shadow PnL for pure execution friction read.
        # See docs/CLEAN_AB_TEST_20260427.md
        self._relaxed_shadow_exits = False  # disabled for clean test
        self._relaxed_shadow_simulation = False  # disabled for clean test
        if self._is_shadow_live:
            logger.warning(
                "CLEAN_AB_MODE: %s on STANDARD guards (relaxed disabled), "
                "qualify_signal bypassed in shadow_live (every paper signal mirrors)",
                self.user_email,
            )

        # 2026-04-27 CLEAN A/B TEST: Stage 1+2 (relaxed_shadow_simulation)
        # ALSO disabled — both users now run the production exit-guard cascade
        # unchanged. The relaxed-shadow code paths are still in the codebase
        # (gated by self._relaxed_shadow_simulation = False) so we can re-enable
        # them in a controlled re-test later. For now, clean baseline only.
        # (No-op assignment; flag was set False above.)

        # Phase 2 — Shadow-of-Shadow forward test (env-controlled)
        # Set PHASE2_SOS_ENABLED=true on the cryptobot service to spawn 5
        # virtual trades per paper signal (one per EXIT_CONFIG). DB write
        # load: ~100 trades/h vs current ~20 — bounded. Disable by unsetting
        # the env var + restart.
        import os as _os
        self._phase2_sos_enabled = (
            _os.getenv("PHASE2_SOS_ENABLED", "false").lower() == "true"
            and self._is_shadow_live
        )
        if self._phase2_sos_enabled:
            logger.warning(
                "PHASE2_SOS: ENABLED for %s — every paper signal spawns "
                "%d virtual trades (configs=%s)",
                self.user_email, len(PHASE2_EXIT_CONFIGS),
                ",".join(c["id"] for c in PHASE2_EXIT_CONFIGS),
            )

        # Phase 5.3 / T4.4 — live emergency halt cache (refreshed every 30s
        # via _is_live_halted). Single SQL UPDATE to users.live_emergency_halt
        # pauses all live trades within one refresh cycle.
        self._live_halt: bool = False
        self._live_halt_ts: float = 0.0
        self._live_halt_ttl: float = 30.0

        if self._is_live:
            # Loudest possible signal that real-money path is armed
            logger.critical(
                "💰 LIVE TRADING ACTIVATED for user=%s email=%s | "
                "lev=%dx loss=$%.0f symbols=%s — REAL MONEY",
                user_id[:8], user_email, self.max_leverage, self.max_daily_loss,
                self.preferred_symbols,
            )

        logger.info(
            "UserRealManager created: user=%s email=%s | lev=%dx loss=$%.0f symbols=%s mode=%s",
            user_id[:8], user_email, self.max_leverage, self.max_daily_loss,
            len(self.preferred_symbols), "ENABLED" if self.enabled else "PAPER",
        )

    # ══════════════════════════════════════════════════════════════
    # QUALIFICATION (user-specific gates)
    # ══════════════════════════════════════════════════════════════

    async def _reconcile_from_exchange(self):
        """Phase 5.0.3 (2026-04-22) — restart-time EXCHANGE-SIDE reconciliation.

        Observed today: two orphan BTC long positions on admin+niranjan
        that were never persisted to DB. Happens when the bot restarts
        AFTER the Delta order filled but BEFORE the DB insert completed.
        DB says flat, exchange has live position, nobody is monitoring.

        Fix: for each preferred_symbol, query Delta directly. If exchange
        shows a non-zero position that we don't have in self.open_trades,
        synthesize a minimal UserTradeRecord and spawn a monitor task.
        SL defaults to 0.65% away from entry in the unfavourable direction
        (matches typical scalp SL). The position is marked with a synthetic
        trade_id and logged LOUDLY so it shows up in audit.
        """
        from exchange.delta_client import PRODUCT_MAP
        is_demo = getattr(self._delta, "mode", "demo") == "demo"
        for sym in self.preferred_symbols:
            info = PRODUCT_MAP.get(sym) or {}
            pid = info.get("demo_id" if is_demo else "prod_id")
            if not pid:
                continue
            try:
                pos = await asyncio.to_thread(self._delta._client.get_position, product_id=pid)
                size = int((pos or {}).get("size", 0) or 0)
                entry = float((pos or {}).get("entry_price", 0) or 0)
            except Exception as e:
                logger.debug("USER %s: exchange recon probe fail for %s: %s",
                             self.user_id[:8], sym, e)
                continue
            if size == 0 or entry <= 0:
                continue
            # Already tracked?
            for t in self.open_trades.values():
                if t.symbol == sym and int(t.position_size) == abs(size):
                    break
            else:
                # Orphan found — synthesise a trade record + monitor
                side = "long" if size > 0 else "short"
                sl = entry * (0.9935 if side == "long" else 1.0065)  # 0.65% SL
                risk = abs(entry - sl)
                _cs_key = "contract_size_demo" if is_demo else "contract_size"
                cs = float(info.get(_cs_key) or info.get("contract_size", 1.0)) or 1.0
                tick = float(info.get("tick_size_demo" if is_demo else "tick_size") or 0.01)
                trade_id = f"u_{self.user_id[:8]}_recovered_{int(time.time()*1000)}"
                trade = UserTradeRecord(
                    trade_id=trade_id,
                    user_id=self.user_id,
                    symbol=sym,
                    side=side,
                    entry_price=entry,
                    stop_loss=sl,
                    take_profit=0,
                    position_size=abs(size),
                    margin=abs(size) * cs * entry / 20.0,  # assume 20x
                    leverage=20,
                    opened_at=time.time() - 60,  # conservative: assume 1m old
                    initial_risk=risk,
                    scanner="exchange_recon",
                    grade="",
                    trade_type="SCALP",
                    product_id=int(pid),
                    tick_size=tick,
                    fee_type="taker",
                    regime="",
                    ml_prob=0.0,
                    contract_size=cs,
                    entry_fee_usd=0.0,
                )
                self.open_trades[trade_id] = trade
                logger.critical(
                    "🚨 EXCHANGE RECON: user=%s %s %s @ %.4f size=%d — orphan found, monitor resumed (SL=%.4f)",
                    self.user_email, sym, side, entry, abs(size), sl,
                )
                # 2026-04-27 — retain task ref to prevent GC-induced orphaning
                self._monitor_tasks[trade_id] = asyncio.create_task(self._monitor_trade(trade_id))
                self._monitor_tasks[trade_id].add_done_callback(
                    lambda _t, _tid=trade_id: self._monitor_tasks.pop(_tid, None)
                )

    async def reconcile_open_trades(self):
        """Phase 4.2 — on manager init, restore in-memory state for any
        user_trades rows still marked 'open' on this user, so the monitor
        loop can resume (trail/kill/pullback/MFE logic). Previously every
        bot restart orphaned open positions: server-side stop still
        protected SL, but the Python-side monitor logic was lost.

        Behaviour:
          - DB row open AND exchange flat → mark DB closed (reason=restart_reconcile_flat)
          - DB row open AND exchange still holds → rebuild UserTradeRecord,
            add to self.open_trades, spawn _monitor_trade task.

        No-op if db_pool is absent. Fails open on any exception (bot still
        runs; orphan stays orphan — worst case matches pre-4.2 behaviour).
        """
        if not self._db_pool:
            return
        try:
            async with self._db_pool.acquire() as conn:
                rows = await conn.fetch(
                    """SELECT id, symbol, side, entry_price, quantity, opened_at,
                              metadata
                       FROM user_trades
                       WHERE user_id=$1 AND trade_type='real' AND status='open'
                       ORDER BY opened_at""",
                    self.user_id,
                )
        except Exception as e:
            logger.warning("USER %s: reconcile query failed: %s", self.user_id[:8], e)
            return

        # 2026-04-26 fix: do NOT early-return if `rows` (real trades) is empty —
        # shadow_live users NEVER have real rows, but they DO have shadow rows
        # that need reconciling further down. Old code returned here and left
        # every restart's shadow trades orphaned.
        if rows:
            logger.info("USER %s: reconciling %d open REAL trade(s) from DB", self.user_id[:8], len(rows))
        from exchange.delta_client import PRODUCT_MAP
        is_demo = getattr(self._delta, "mode", "demo") == "demo"

        for r in rows:
            meta = r["metadata"] or {}
            if isinstance(meta, str):
                try:
                    import json as _json
                    meta = _json.loads(meta)
                except Exception:
                    meta = {}
            sym = r["symbol"]
            side = (r["side"] or "long").lower()
            info = PRODUCT_MAP.get(sym) or {}
            pid = info.get("demo_id" if is_demo else "prod_id")
            if not pid:
                continue

            # Check actual position on exchange (Phase 4.4 — off event loop)
            on_exch_size = 0
            try:
                pos = await asyncio.to_thread(self._delta._client.get_position, product_id=pid)
                on_exch_size = int((pos or {}).get("size", 0) or 0)
            except Exception as e:
                logger.debug("USER %s: pos check fail for %s: %s",
                             self.user_id[:8], sym, e)
                continue

            if on_exch_size == 0:
                # DB says open but exchange is flat → mark closed
                try:
                    async with self._db_pool.acquire() as conn:
                        await conn.execute(
                            """UPDATE user_trades
                               SET status='closed', closed_at=NOW(),
                                   metadata = metadata || '{"exit_reason":"restart_reconcile_flat"}'::jsonb
                               WHERE id=$1""",
                            r["id"],
                        )
                    logger.info("USER %s: DB reconciled %s — was flat on exchange",
                                self.user_id[:8], sym)
                except Exception:
                    pass
                continue

            # Position still open → rebuild record + resume monitor
            try:
                entry = float(r["entry_price"] or 0)
                sl = float(meta.get("stop_loss") or (entry * 0.99))
                tp = float(meta.get("take_profit") or 0)
                init_risk = float(meta.get("initial_risk") or abs(entry - sl))
                trade = UserTradeRecord(
                    trade_id=str(r["id"]),
                    user_id=self.user_id,
                    symbol=sym,
                    side=side,
                    entry_price=entry,
                    stop_loss=sl,
                    take_profit=tp,
                    position_size=abs(on_exch_size),
                    margin=float(meta.get("margin") or 0),
                    leverage=int(meta.get("leverage") or 20),
                    opened_at=r["opened_at"].timestamp() if r["opened_at"] else time.time(),
                    initial_risk=init_risk,
                    scanner=str(meta.get("scanner") or ""),
                    grade=str(meta.get("grade") or ""),
                    trade_type=str(meta.get("trade_type") or "SCALP"),
                    product_id=int(pid),
                    tick_size=float(meta.get("tick_size") or info.get("tick_size_demo" if is_demo else "tick_size") or 0.01),
                    fee_type=str(meta.get("fee_type") or "taker"),
                    regime=str(meta.get("regime") or ""),
                    ml_prob=float(meta.get("ml_prob") or 0),
                    server_stop_id=(int(meta["server_stop_id"]) if meta.get("server_stop_id") else None),
                    # Phase 4.6 — restore contract_size + entry fee
                    contract_size=float(
                        meta.get("contract_size")
                        or info.get("contract_size_demo" if is_demo else "contract_size")
                        or info.get("contract_size", 1.0)
                    ),
                    entry_fee_usd=float(meta.get("entry_fee_usd") or 0),
                )
                self.open_trades[trade.trade_id] = trade
                # 2026-04-27 — retain task ref to prevent GC-induced orphaning
                self._monitor_tasks[trade.trade_id] = asyncio.create_task(self._monitor_trade(trade.trade_id))
                self._monitor_tasks[trade.trade_id].add_done_callback(
                    lambda _t, _tid=trade.trade_id: self._monitor_tasks.pop(_tid, None)
                )
                logger.warning(
                    "USER REAL RECONCILED: %s %s %s | entry=%.4f sl=%.4f lots=%d (resuming monitor)",
                    self.user_email, sym, side, entry, sl, abs(on_exch_size),
                )
            except Exception as e:
                logger.error("USER %s: reconcile rebuild failed for %s: %s",
                             self.user_id[:8], sym, e)

        # ── Phase 2026-04-26 patch: also reconcile SHADOW trades ──
        # Bug uncovered while debugging 18 delta_shadow rows stuck open >3h:
        # the original loop only handled trade_type='real', so every bot
        # restart orphaned every shadow trade's monitor (no _close_shadow
        # ever fired → max-age/SL/TP never triggered → permanent open).
        # Shadow has no exchange position to verify; we trust the DB row
        # and respawn the monitor unconditionally.
        try:
            async with self._db_pool.acquire() as conn:
                shadow_rows = await conn.fetch(
                    """SELECT id, symbol, side, entry_price, quantity, opened_at,
                              metadata
                       FROM user_trades
                       WHERE user_id=$1
                         AND trade_type='shadow'
                         AND exchange='delta_india'
                         AND status='open'
                       ORDER BY opened_at""",
                    self.user_id,
                )
        except Exception as e:
            logger.warning("USER %s: shadow reconcile query failed: %s",
                           self.user_id[:8], e)
            shadow_rows = []

        # Always emit a warning so we can see this fired (root logger is at WARNING)
        logger.warning("USER %s (%s): shadow reconcile — found %d open SHADOW trade(s)",
                       self.user_id[:8], self.user_email, len(shadow_rows))
        if shadow_rows:
            logger.warning("USER %s: reconciling %d open SHADOW trade(s) from DB",
                        self.user_id[:8], len(shadow_rows))
            from exchange.delta_client import PRODUCT_MAP
            is_demo = getattr(self._delta, "mode", "demo") == "demo"
            for r in shadow_rows:
                meta = r["metadata"] or {}
                if isinstance(meta, str):
                    try:
                        import json as _json
                        meta = _json.loads(meta)
                    except Exception:
                        meta = {}
                sym = r["symbol"]
                side = (r["side"] or "long").lower()
                info = PRODUCT_MAP.get(sym) or {}
                pid = info.get("demo_id" if is_demo else "prod_id") or 0
                try:
                    entry = float(r["entry_price"] or 0)
                    qty   = int(float(r["quantity"]) or 0)
                    sl    = float(meta.get("stop_loss")   or (entry * 0.99))
                    tp    = float(meta.get("take_profit") or 0)
                    init_risk = float(meta.get("initial_risk") or abs(entry - sl))
                    trade = UserTradeRecord(
                        trade_id=str(r["id"]),
                        user_id=self.user_id,
                        symbol=sym,
                        side=side,
                        entry_price=entry,
                        stop_loss=sl,
                        take_profit=tp,
                        position_size=abs(qty),
                        margin=float(meta.get("margin") or 0),
                        leverage=int(meta.get("leverage") or 20),
                        opened_at=r["opened_at"].timestamp() if r["opened_at"] else time.time(),
                        initial_risk=init_risk,
                        scanner=str(meta.get("scanner") or ""),
                        grade=str(meta.get("grade") or ""),
                        trade_type=str(meta.get("trade_type") or "SCALP"),
                        product_id=int(pid),
                        tick_size=float(meta.get("tick_size") or info.get("tick_size_demo" if is_demo else "tick_size") or 0.01),
                        fee_type=str(meta.get("fee_type") or "taker"),
                        regime=str(meta.get("regime") or ""),
                        ml_prob=float(meta.get("ml_prob") or meta.get("ml_probability") or 0),
                        server_stop_id=None,  # shadow has no server-side stop
                        contract_size=float(
                            meta.get("contract_size")
                            or info.get("contract_size_demo" if is_demo else "contract_size")
                            or info.get("contract_size", 1.0)
                        ),
                        entry_fee_usd=float(meta.get("entry_fee_usd") or 0),
                    )
                    trade._is_shadow = True   # critical — routes _monitor_trade → _close_shadow
                    # 2026-04-27 — for reconciled trades the in-memory trade_id
                    # IS the DB UUID, so use it directly for the fast close path.
                    trade._db_id = trade.trade_id

                    # 2026-04-27 — Phase 2 fan-out attribute restoration on
                    # restart reconcile. The fan-out path sets
                    # trade._exit_config + trade._is_phase2_virtual as Python
                    # attributes only — the DB only stores
                    # metadata.exit_config_id + is_phase2_virtual. Without
                    # this rebuild, reloaded P2 trades fall back to the
                    # default 600s shadow max_age and get force-closed by
                    # Agent 9-A at 60min as auto_responder_stuck_60m
                    # (PnL=$0), which contaminates the leaderboard and
                    # makes Phase 2 forward DIVERGE from Phase 3 historical.
                    # Discovered when v1_5min_tight (5min cap) had 4 trades
                    # closing at 60min stuck after 4 restarts on 04-27.
                    _is_p2v = (
                        meta.get("is_phase2_virtual") is True
                        or str(meta.get("is_phase2_virtual", "")).lower() == "true"
                    )
                    if _is_p2v:
                        trade._is_phase2_virtual = True
                        cfg_id = str(meta.get("exit_config_id") or "")
                        if cfg_id:
                            for _cfg in PHASE2_EXIT_CONFIGS:
                                if _cfg["id"] == cfg_id:
                                    trade._exit_config = _cfg
                                    break

                    # 2026-04-27 — RACE-FREE PRE-EMPTIVE TIME-DECAY CLOSE.
                    # 19 monitor tasks spawned at the same instant by reconcile
                    # caused an asyncio scheduling race: ~8 won (closed via
                    # time_decay on first tick), ~11 lost (drifted until
                    # Agent 9-A scoop'd them at 60min as auto_responder_stuck_60m
                    # / PnL=$0). Race-free fix: SYNCHRONOUSLY close any trade
                    # that's already past its max_age BEFORE spawning a monitor.
                    # Determine effective max_age: P2 cfg if set, else SHADOW
                    # default (600s for all shadow trade_types). For non-P2
                    # shadow this matches the in-monitor logic 1:1.
                    self.open_trades[trade.trade_id] = trade
                    _age_at_recon = max(
                        0.0,
                        time.time() - float(trade.opened_at or time.time()),
                    )
                    if _is_p2v and getattr(trade, "_exit_config", None):
                        _eff_max = int(trade._exit_config.get("max_age_sec") or 600)
                    else:
                        _eff_max = 600  # shadow default cap
                    if _age_at_recon > _eff_max:
                        # Skip monitor — close immediately at entry price
                        # (PnL≈0; no live price → use entry as worst-case neutral).
                        # Single fixed exit_reason so the filter list stays
                        # simple. Age is recorded in metadata.recon_age_min
                        # via _close_shadow's standard close_meta path
                        # (peak_mfe_r/funding/etc). Filter alongside
                        # auto_responder_stuck_60m + restart_orphan_cleanup.
                        try:
                            await self._close_shadow(
                                trade,
                                trade.entry_price,
                                "reconcile_overaged_close",
                            )
                            logger.warning(
                                "RECON_PREEMPTIVE_CLOSE: %s %s %s | age=%.1fm > max=%ds (skipped monitor race)",
                                self.user_email, sym, side, _age_at_recon/60, _eff_max,
                            )
                            continue  # next row, don't spawn monitor
                        except Exception as _ce:
                            logger.warning(
                                "RECON_PREEMPTIVE_CLOSE_FAIL: %s %s — %s (falling back to monitor)",
                                self.user_email, sym, _ce,
                            )
                            # Fall through to spawn monitor anyway

                    # 2026-04-27 — retain task ref to prevent GC-induced orphaning
                    self._monitor_tasks[trade.trade_id] = asyncio.create_task(self._monitor_trade(trade.trade_id))
                    self._monitor_tasks[trade.trade_id].add_done_callback(
                        lambda _t, _tid=trade.trade_id: self._monitor_tasks.pop(_tid, None)
                    )
                    _p2_tag = ""
                    if _is_p2v:
                        _p2_tag = f" [P2:{cfg_id}{'' if getattr(trade, '_exit_config', None) else ' MISS'}]"
                    logger.warning(
                        "USER SHADOW RECONCILED: %s %s %s | entry=%.4f sl=%.4f qty=%d (resuming monitor)%s",
                        self.user_email, sym, side, entry, sl, abs(qty), _p2_tag,
                    )
                except Exception as e:
                    logger.error("USER %s: shadow reconcile rebuild failed for %s: %s",
                                 self.user_id[:8], sym, e)

    async def _refresh_cohort_blacklist(self):
        """Phase 4.1 — recompute (symbol, side) cohorts that are currently
        losing. Rule: last 5 closed real trades in the same cohort over
        the past 7 days all had pnl ≤ 0 → veto new entries in that cohort.
        Cache for 5 min. Defensive: any error keeps the prior blacklist
        (fail-open; never blocks trading on DB hiccups).

        Phase 5.6-C (2026-04-24) — EXCLUDE CONTAMINATED PHASES.
        Analysis of Apr 24 cohort blacklist showed ALL 4 blocked cohorts
        (BTC long/short, ETH long, SOL short) were built from pre-5.4
        losses: bug-era SL force-closes (5.3–5.3.8), wrong-direction regime
        sizing (pre-5.4-B inversion), DOA signals before quick_kill ship
        (pre-5.4-C). These losses do NOT reflect current signal quality or
        current execution code. Filter the cohort query to only count
        closed trades from Phase 5.4+ so the blacklist measures the
        CURRENT stack's behavior.
        Effect: cohort blacklist rebuilds from fresh data; contaminated
        pre-5.4 trades no longer count toward veto. If a cohort keeps
        losing under the new stack, THOSE trades will legitimately
        blacklist it.
        """
        if not self._db_pool:
            return
        try:
            async with self._db_pool.acquire() as conn:
                # Phase 5.19.1 (2026-04-25): per-user pause override.
                # If users.cohort_blacklist_paused_until is in the future,
                # bypass the blacklist entirely (clears in-memory cache).
                # Use case: parallel maker-mode A/B/C test on admin needs
                # admin to participate in ALL signals for clean per-mode
                # data. Without pause, prior cohort losses bias the test.
                pause_row = await conn.fetchrow(
                    "SELECT cohort_blacklist_paused_until FROM users WHERE id=$1",
                    self.user_id,
                )
                if pause_row and pause_row["cohort_blacklist_paused_until"]:
                    from datetime import datetime, timezone
                    pause_until = pause_row["cohort_blacklist_paused_until"]
                    if pause_until > datetime.now(timezone.utc):
                        if self._cohort_blacklist:
                            logger.warning(
                                "USER %s: cohort blacklist PAUSED until %s — clearing %d cohorts",
                                self.user_id[:8], pause_until.isoformat(),
                                len(self._cohort_blacklist),
                            )
                        self._cohort_blacklist = set()
                        self._cohort_refresh_ts = time.time()
                        return

                rows = await conn.fetch("""
                    SELECT symbol, side, pnl_usd, metadata
                    FROM user_trades
                    WHERE user_id=$1 AND status='closed'
                      AND trade_type IN ('real', 'shadow')
                      AND pnl_usd IS NOT NULL
                      AND opened_at > NOW() - INTERVAL '7 days'
                    ORDER BY opened_at DESC
                    LIMIT 500
                """, self.user_id)
            from collections import defaultdict
            cohorts = defaultdict(list)
            for r in rows:
                # Phase 5.6-C: filter to Phase 5.4+ trades only.
                # metadata is a JSONB; asyncpg returns it as str. Parse.
                _phase = ""
                try:
                    _md = r["metadata"]
                    if isinstance(_md, str):
                        import json as _json
                        _md = _json.loads(_md)
                    _phase = str((_md or {}).get("phase", "")) if isinstance(_md, dict) else ""
                except Exception:
                    _phase = ""
                # Include only Phase 5.4+ trades. Accept any variant "5.4", "5.4-A", "5.5", "5.5.2", "5.6", etc.
                # Reject earlier phases ("5.0", "5.2", "5.3", "5.3.1", "5.3.5", "5.3.8", etc.).
                _phase_major_minor = _phase.split("-")[0]  # strip suffix like "5.4-A"
                if not _phase_major_minor:
                    continue  # no phase tag (older trades) → skip
                # Simple lexical check: a valid post-5.4 phase starts with "5.4", "5.5", "5.6", "5.7"...
                # But NOT "5.0", "5.1", "5.2", "5.3".
                if not (_phase_major_minor.startswith("5.4") or
                        _phase_major_minor.startswith("5.5") or
                        _phase_major_minor.startswith("5.6") or
                        _phase_major_minor.startswith("5.7") or
                        _phase_major_minor.startswith("5.8") or
                        _phase_major_minor.startswith("5.9") or
                        _phase_major_minor.startswith("6.") or
                        _phase_major_minor.startswith("7.")):
                    continue
                key = (r["symbol"], (r["side"] or "").lower())
                if len(cohorts[key]) < 5:
                    cohorts[key].append(float(r["pnl_usd"] or 0))
            # Phase 5.20-B2 (2026-04-25) — explicit min_n=5 (was implicit).
            # Audit raised: cohort can blacklist on N=2 if only 2 closed
            # trades exist in the cohort over 7 days — too noisy. Tightening
            # to require at least 5 same-cohort losses (matches the existing
            # 5-trade window cap above). No behavior change vs current code,
            # but documents the invariant explicitly.
            _COHORT_BLACKLIST_MIN_N = 5
            new_bl = {
                key for key, pnls in cohorts.items()
                if len(pnls) >= _COHORT_BLACKLIST_MIN_N and all(p <= 0 for p in pnls)
            }
            self._cohort_blacklist = new_bl
            self._cohort_refresh_ts = time.time()
            if new_bl:
                logger.warning(
                    "USER %s: cohort blacklist active (post-5.4 only) = %s",
                    self.user_id[:8], sorted(new_bl),
                )
            else:
                logger.info(
                    "USER %s: cohort blacklist empty (post-5.4 data insufficient yet)",
                    self.user_id[:8],
                )
        except Exception as e:
            logger.debug("USER %s: cohort refresh failed: %s", self.user_id[:8], e)

    async def _is_live_halted(self) -> bool:
        """Phase 5.3 / T4.4 — check users.live_emergency_halt with 30s cache.

        Returns True if the user's live trading is emergency-halted. Does
        NOT raise on DB errors — failing-open defaults to NOT halted so
        a transient DB blip doesn't kill all live trading. Operator sets
        halt via:
            UPDATE users SET live_emergency_halt=TRUE WHERE email=...;
        """
        if not self._is_live:
            return False
        if time.time() - self._live_halt_ts < self._live_halt_ttl:
            return self._live_halt
        if not self._db_pool:
            return self._live_halt
        try:
            async with self._db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT live_emergency_halt FROM users WHERE id = $1::uuid",
                    str(self.user_id),
                )
                halted = bool(row and row["live_emergency_halt"])
                if halted and not self._live_halt:
                    # Rising edge — log CRITICAL so ops sees the flip.
                    logger.critical(
                        "🛑 LIVE HALT ACTIVATED user=%s email=%s — all new live trades will be rejected",
                        self.user_id[:8], self.user_email,
                    )
                self._live_halt = halted
                self._live_halt_ts = time.time()
                return halted
        except Exception:
            # Fail-open: return last known state (default False)
            return self._live_halt

    async def qualify_signal(self, signal: dict) -> Tuple[bool, str]:
        """Check if this signal should fire a real trade for THIS user.

        Phase 4.1: now async — cohort blacklist refresh is a DB query
        (cached 5 min) performed before the standard checks.
        """
        symbol = signal.get("symbol", "")
        meta = signal.get("metadata", {}) or {}

        # 1. User enabled?
        if not self.enabled:
            return False, "user_disabled"

        # Phase 5.20-B1 (2026-04-25) — GLOBAL kill switch.
        # Single SQL update halts all trading within 5s. Failsafe for ops.
        if self._db_pool:
            try:
                from execution.kill_switch import is_killed
                if await is_killed(self._db_pool):
                    return False, "kill_switch_engaged"
            except Exception:
                pass  # fail-open: don't block trading on kill_switch import error

        # 1b. Phase 5.0 LIVE SAFETY — minimum balance floor.
        # Real money mode rejects trades when wallet is below $20.
        # Reason: fees are % of notional; below ~$20 equity, even a full
        # winner barely clears fees. Protects micro-accounts from
        # churn-to-zero. Demo mode ignores this gate.
        if self._is_live:
            _bal = self._cached_balance or 0
            if _bal < self._live_balance_floor_usd:
                return False, f"user_live_balance_floor:${_bal:.2f}<${self._live_balance_floor_usd:.0f}"

        # 2. Circuit breaker
        self.cb.check_daily_reset()
        if self.cb.is_tripped:
            return False, f"user_cb_tripped:{self.cb.consecutive_losses}losses/${self.cb.daily_loss_usd:.0f}daily"

        # 3. Daily trade limit
        if self.cb.trade_count_today >= self.max_daily_trades:
            return False, f"user_daily_limit:{self.cb.trade_count_today}/{self.max_daily_trades}"

        # 4. Symbol filter (if user has preferred symbols)
        if self.preferred_symbols and symbol not in self.preferred_symbols:
            return False, f"user_symbol_filter:{symbol}"

        # 4b. Venue-mode support — BUGFIX 2026-04-21: if this user is in demo
        # mode but the symbol has demo_id=0 (not listed on Delta testnet —
        # e.g. LTC, DOT, LINK, most memes), we'd place an order against a
        # zero product_id (error) AND WS wouldn't stream it → the trade
        # would open on REST but never be monitorable or closable. Reject
        # up front so the scanner continues paper-only for this symbol.
        try:
            from exchange.delta_client import PRODUCT_MAP as _PM
            _info = _PM.get(symbol) or {}
            _mode = getattr(self._delta, "mode", "demo")
            _pid_field = "demo_id" if _mode == "demo" else "prod_id"
            if not _info.get(_pid_field):
                return False, f"user_venue_unsupported:{symbol}:{_mode}"
        except Exception:
            pass

        # 5. Confidence floor
        conf = signal.get("confidence", 0)
        if conf < self.min_confidence:
            return False, f"user_conf_floor:{conf}<{self.min_confidence}"

        # 6. Grade filter — Phase 5.3.2 (2026-04-23) — allow grade B with
        # ml>=0.60 compensating floor.
        # T2.2 replay found grade gate rejected 168 paper winners ($489)
        # in last 7 days; most were grade B. Per-trade expectancy on
        # rejected B winners: +$2.91 vs -$2.17 losers. Net edge positive.
        # Gating on ml>=0.60 (vs default 0.55) filters out the weaker
        # half of B signals to protect WR while unlocking volume.
        # A+/A continue to pass freely.
        grade = signal.get("grade", "C")
        if grade not in ("A+", "A", "B"):
            return False, f"user_grade_filter:{grade}"
        if grade == "B":
            _ml_for_b = meta.get("ml_probability")
            if _ml_for_b is not None and float(_ml_for_b) > 0 and float(_ml_for_b) < 0.60:
                return False, f"user_grade_B_ml_floor:{float(_ml_for_b):.3f}<0.60"

        # 7. ML probability floor
        ml_prob = meta.get("ml_probability")
        if ml_prob is not None and float(ml_prob) > 0 and float(ml_prob) < self.ml_threshold:
            return False, f"user_ml_floor:{ml_prob:.3f}<{self.ml_threshold:.2f}"

        # Phase 5.11 (2026-04-24) — FEE-WALL COHORT REJECT (demo/live only).
        # Empirical finding on 150-trade 3-day sample:
        #   grade=A+ AND ml_prob>=0.80 cohort → 20 trades, 1 winner (5% WR),
        #   -$10.51 total, -$0.525 avg/trade.
        # Root cause: A+ signals have the tightest SL distance, making them
        # maximally vulnerable to fee-wall friction on demo/live execution.
        # Paper's frictionless reality LOVES A+ (77.8% WR in paper data);
        # demo's real fills kill them.
        #
        # Phase 5.13 (2026-04-25) — EXPLORE/EXPLOIT OVERRIDE (5%).
        # Quant rigor demand: rule was derived in-sample on 150 trades.
        # Without ongoing OOS validation, the rule will rot silently if the
        # underlying cohort distribution shifts (regime change, new scanner,
        # different time of day). Solution: 5% of would-be rejects get
        # admitted anyway, tagged with explore=true so we can later compute
        # the IF-ADMITTED outcome on the rejected cohort and re-evaluate.
        # Cost: 5% × 20 rejects/3days = ~3 extra "test" trades per 3 days
        # at ~-$0.5 expected each = ~$1.50 every 3 days for ongoing validation.
        # Cheap insurance against rule rot.
        _ml_f = float(ml_prob or 0) if ml_prob is not None else 0.0
        if grade == "A+" and _ml_f >= 0.80:
            # Deterministic-pseudorandom on signal_id for reproducibility +
            # to ensure same signal can't be admitted on one user and
            # rejected on another (consistency for cohort analysis).
            _sig_id = str(signal.get("id") or signal.get("signal_id") or
                          f"{symbol}_{signal.get('side','')}_{int(time.time())}")
            import hashlib
            _h = int(hashlib.md5(_sig_id.encode()).hexdigest()[:8], 16)
            _explore_threshold = 0xFFFFFFFF * 0.05  # 5%
            if _h < _explore_threshold:
                # 5% explore — admit but tag for cohort analysis
                meta["fee_wall_explore"] = True
                meta["fee_wall_rule_decision"] = "would_reject"
                logger.info(
                    "FEE_WALL_EXPLORE user=%s %s grade=%s ml=%.2f — admitting 5%% sample",
                    self.user_id[:8], symbol, grade, _ml_f,
                )
            else:
                return False, f"fee_wall_reject:grade=A+_ml={_ml_f:.2f}"

        # 7b. Phase 5.3 / T4.2 — MAINTENANCE MARGIN GUARD.
        # On 20× isolated leverage, Delta India liquidation fires at ~5%
        # adverse move from entry. A 1R SL that's wider than 4% would
        # trigger liquidation BEFORE our stop, wiping the entire margin
        # instead of losing 1R. Cap sl_pct at 4% to leave ≥1% buffer.
        # Tight ATR trades (0.3-1.5% SL) pass freely; news-spike wide-ATR
        # trades get blocked. Demo testnet doesn't liquidate but we gate
        # here anyway so live flip is a no-op.
        _ep = float(signal.get("entry_price", 0) or 0)
        _sl_sig = float(signal.get("stop_loss", 0) or 0)
        if _ep > 0 and _sl_sig > 0:
            _sl_pct = abs(_ep - _sl_sig) / _ep
            if _sl_pct > 0.04:
                return False, f"user_margin_guard:sl={_sl_pct*100:.2f}%>4%"

        # 7c. Phase 5.3 / T4.4 — LIVE EMERGENCY HALT gate.
        # Demo path ignores. Live path checks users.live_emergency_halt
        # (refreshed every 30s via DB query). A single SQL UPDATE to this
        # column pauses all live trades for a user within one qualify cycle.
        if self._is_live and await self._is_live_halted():
            return False, "user_live_emergency_halt"

        # Phase 5.20-A3 (2026-04-25) — MULTI-SCANNER DEDUP GUARD.
        # Audit risk: structure_bounce + bb_squeeze can fire on same bar
        # for same (symbol, side) → two trades = duplicate fee bleed.
        # Last 7d data showed 0 duplicates — but cheap insurance.
        # Key: (symbol, side, 60s-bucket of bar timestamp). 60s TTL.
        _now_ts = time.time()
        # Garbage-collect stale entries first
        self._recent_signal_keys = {
            k: t for k, t in self._recent_signal_keys.items()
            if _now_ts - t < 60.0
        }
        _bucket = int(_now_ts // 60) * 60  # 60s bucket boundary
        _dedup_key = f"{symbol}|{(signal.get('side') or '').lower()}|{_bucket}"
        if _dedup_key in self._recent_signal_keys:
            return False, f"multi_scanner_dedup:{_dedup_key.split('|')[2]}"
        self._recent_signal_keys[_dedup_key] = _now_ts

        # 8. Max open positions (raised 3 → 5 on 2026-04-27 per architect
        # directive — clean A/B test allows higher concurrency to capture
        # more samples per hour without ladder-decay penalty)
        if len(self.open_trades) >= 5:
            return False, f"user_max_open:{len(self.open_trades)}/5"

        # 9. Duplicate symbol — Phase 5.3 / T2.3, raised to 3 in Phase 5.3.1.
        # Paper logs 5-10 concurrent same-symbol trail_profits on strong
        # moves (Apr 23 data: 38 BTC shorts in 24h = 1 every 38 min).
        # Phase 5.3 raised 1 → 2. Phase 5.3.1 raises 2 → 3 after data
        # showed cap=2 was still 5× below paper's implicit concurrency.
        # Combined risk on 3 concurrent same-side entries (50% sized on
        # 2nd and 3rd via ladder decay): 1R + 0.5R + 0.5R = 2R max-loss
        # if all SLs hit simultaneously. Acceptable for the expected
        # +$X lift on rollover capture. G7 canary enforces auto-rollback.
        # Opposite-side same-symbol still blocked (hedge = noise).
        _side_sig = (signal.get("side", "long") or "long").lower()
        _same_sym_same_side = sum(
            1 for t in self.open_trades.values()
            if t.symbol == symbol and (t.side or "").lower() == _side_sig
        )
        _same_sym_opp_side = sum(
            1 for t in self.open_trades.values()
            if t.symbol == symbol and (t.side or "").lower() != _side_sig
        )
        if _same_sym_opp_side > 0:
            return False, f"user_duplicate_hedge:{symbol}"
        if _same_sym_same_side >= 3:
            return False, f"user_duplicate:{symbol}(3/3)"
        # Tag ladder entries so compute_size can apply 50% decay
        if _same_sym_same_side >= 1:
            signal["_is_ladder_entry"] = True

        # 9b. Phase 5.4 (2026-04-24) — REGIME HARD-REJECT.
        # 48h cohort analysis: mean_reversion regime produced 10 losses
        # totaling -$6.47 with ZERO meaningful wins. Average peak 0.01R
        # (pure DOA). The regime label predicts "prices will revert" but
        # doesn't time the entry — signals fire mid-range and die.
        # Hard-reject preserves the 3-4 other regimes where edge exists.
        _rg_hr = (meta.get("regime", "") or "").lower()
        if _rg_hr == "mean_reversion":
            return False, f"user_regime_skip:mean_reversion"

        # 10. Cohort hard-veto (Phase 4.1, Lever 2)
        # Refreshes blacklist from DB every 5 min. Blocks (symbol, side)
        # cohorts whose last 5 closed trades in 7 days ALL lost — the
        # cheapest WR lift available (~+$2/day/account per framework).
        if time.time() - self._cohort_refresh_ts > self._cohort_ttl_sec:
            await self._refresh_cohort_blacklist()
        side = (signal.get("side", "long") or "long").lower()
        if (symbol, side) in self._cohort_blacklist:
            return False, f"user_cohort_veto:{symbol}_{side}_last5_all_lost"

        return True, "qualified"

    # ══════════════════════════════════════════════════════════════
    # SIZING (user-specific)
    # ══════════════════════════════════════════════════════════════

    def compute_size(self, signal: dict) -> Tuple[float, int, int]:
        """Compute margin, leverage, lots for THIS user.

        Returns (margin_usd, leverage, lots).
        """
        meta = signal.get("metadata", {}) or {}
        entry_price = signal.get("entry_price", 0)
        symbol = signal.get("symbol", "")

        # Get balance — for shadow_live, prefer simulated bankroll so all
        # shadow users size off a comparable A/B base (re-added 2026-04-26;
        # symptom: niranjan was sized 5-7× admin because we used real wallet).
        _is_sl = getattr(self, "_is_shadow_live", False)
        _ssb   = getattr(self, "shadow_simulated_balance", None)
        if _is_sl and _ssb is not None:
            balance = float(_ssb)
            _bal_src = "shadow_sim"
        else:
            balance = self._cached_balance or 100.0
            _bal_src = "cached" if self._cached_balance else "default100"
        # one-line trace so we can confirm wiring in logs
        try:
            logger.warning("SIZE_BAL: user=%s bot_mode=%s _is_sl=%s ssb=%s cached=%s → using=%.2f (%s)",
                        self.user_email, getattr(self, "_bot_mode", "?"),
                        _is_sl, _ssb, self._cached_balance, balance, _bal_src)
        except Exception:
            pass
        usable = balance * 0.85  # 15% reserve

        # Grade-based margin
        grade = signal.get("grade", "C")
        if grade == "A+":
            base_margin = min(75, usable * 0.20)
        elif grade == "A":
            base_margin = min(65, usable * 0.15)
        elif grade == "B":
            base_margin = min(55, usable * 0.12)
        else:
            base_margin = min(45, usable * 0.10)

        # Phase 4.1 — conviction sizing (Lever 5 from edge framework).
        # Scale margin by ML confidence: boost when model is very sure,
        # cut when marginal. Range 0.80× (ml=0.50) → 1.30× (ml=0.90+).
        # Compounds with grade: A+ @ ml=0.85 gets ~1.2× more than A @ ml=0.65,
        # routing capital to higher-EV signals without new risk at the floor.
        ml_prob = float(meta.get("ml_probability", 0.5) or 0.5)
        if ml_prob >= 0.50:
            conviction = max(0.80, min(1.30, 0.80 + (ml_prob - 0.50) * 1.25))
        else:
            conviction = 0.80

        # Phase 4.2 — grade C size reduction (Phase 5.0 notes: grade C now
        # blocked at qualify_signal, so grade_mult effectively always 1.0
        # for signals that reach this code path; keep the logic in case
        # grade filter loosens again).
        grade_mult = 0.70 if grade == "C" else 1.0

        # Phase 5.4 (2026-04-24) — REGIME SIZING REBALANCE.
        # Phase 5.0 sized UP trending/breakout/high_vol on the theory
        # "strong regime = more edge." 48h cohort data proved the opposite:
        #   - high_volatility × A+ × time_decay_60m: 4 losses @ -$2.72 avg = -$10.88 ← biggest cohort loss
        #   - breakout × no_momentum:                4 losses @ -$2.03 avg = -$8.11
        #   - trending_down × no_proof_of_life:      2 losses @ -$0.85 avg = -$1.71
        # Strong regime = bigger moves BOTH directions. Sizing up just
        # amplifies losses on the 60%+ of signals that die immediately.
        # New sizing: neutral-to-reduced on "strong" regimes, modest cut
        # on chop. Regime becomes a small tilt, not a conviction bet.
        _regime = str(meta.get("regime", "") or "").lower()
        if _regime in ("trending", "trending_up", "trending_down"):
            regime_mult = 0.90   # was 1.30 — trending but signals still die
        elif _regime == "breakout":
            regime_mult = 0.70   # was 1.30 — $8+ lost per breakout cohort in 48h
        elif _regime == "high_volatility":
            regime_mult = 0.70   # was 1.10 — biggest single cohort loss
        elif _regime in ("sideways", "ranging", "quiet", "low_liquidity"):
            regime_mult = 0.50   # was 0.60 — chop is chop, slightly tighter
        elif _regime == "mean_reversion":
            regime_mult = 0.50   # rejected at qualify_signal anyway; safe fallback
        else:
            regime_mult = 0.80

        # Phase 5.3 / T2.3 — LADDER ENTRY size decay.
        # When qualify_signal tagged this as a 2nd-leg entry on an existing
        # same-symbol-same-side trade, cap sizing at 50% so the combined
        # exposure stays within the single-entry risk envelope (roughly).
        # Loss scenario: both stops hit together → 1R + 0.5R = 1.5R loss,
        # vs 1R on a single entry. Accepted because 2nd entry is AFTER the
        # 1st has shown favor, so base_margin + 50% concentrates size when
        # the setup is validated.
        ladder_mult = 0.50 if signal.get("_is_ladder_entry") else 1.0

        margin = base_margin * self.size_multiplier * conviction * grade_mult * regime_mult * ladder_mult

        # Smart floor/ceiling.
        # Floor: $10 — lowers fee drag pressure on marginal trades.
        # Ceiling: Phase 4.5 (2026-04-22) — raised 15% → 22% of balance.
        # Reason: current edge (Phase 4.1 cohort: +$0.26/trade) implies
        # Kelly allocation of ~1-2% bankroll risk per trade. At 20× leverage
        # and 0.65% SL, that maps to ~22% of balance as margin. Previous
        # 15% cap was under-sized by ~40% vs Kelly. Three-consecutive-loss
        # CB still halts at ~9% drawdown even at 22% sizing.
        # NO-DEGRADE check: canary G7 compares Phase 4.5 cohort to Phase 4.1.
        # Bigger size means bigger $ swings but identical R-expectancy —
        # guardrails catch any WR regression, not size.
        # 2026-04-26 fix: ceiling MUST use the same balance source as base
        # (shadow_simulated_balance for shadow_live, else cached/wallet).
        # Previously ceiling always read _cached_balance → admin ceilinged at
        # $10 (real wallet ~$45) while niranjan ceilinged at $22 (wallet ~$100),
        # which inverted the intended A/B (admin sim_bal=1000 vs niranjan 443).
        _ceiling_bal = balance  # already resolved above (shadow_sim or cached)
        _ceiling = max(_ceiling_bal * 0.22, 10.0)  # never less than floor itself
        margin = max(10.0, min(margin, _ceiling))

        # Leverage (capped by user's max, final hard cap 50x to avoid abuse)
        leverage = min(self.max_leverage, 50)

        # Lots — BUGFIX 2026-04-21: Delta's lot size is denominated in
        # contract_size units of the underlying (e.g. BTC 1 lot = 0.001 BTC,
        # PEPE 1 lot = 1000 PEPE). Previous `int(notional / entry_price)`
        # under-sized BTC/ETH by contract_size factor (1 lot instead of ~6
        # for $560 notional at $87k BTC) and over-sized memes by 1000x.
        # Correct: lots = notional / (entry_price * contract_size).
        #
        # 2026-04-22: mode-aware lookup — testnet has different
        # contract_size for some products (DOGE: 100/lot demo vs 1/lot prod).
        # Falls back to prod contract_size if no demo override is set.
        try:
            from exchange.delta_client import PRODUCT_MAP as _PM
            _info = _PM.get(symbol, {}) or {}
            _is_demo = getattr(self._delta, "mode", "demo") == "demo"
            _cs_key = "contract_size_demo" if _is_demo else "contract_size"
            contract_size = float(
                _info.get(_cs_key) or _info.get("contract_size", 1.0)
            ) or 1.0
        except Exception:
            contract_size = 1.0

        notional = margin * leverage
        if entry_price > 0 and contract_size > 0:
            lots = max(1, int(notional / (entry_price * contract_size)))
        else:
            lots = 1

        return round(margin, 2), leverage, lots

    # ══════════════════════════════════════════════════════════════
    # EXECUTION
    # ══════════════════════════════════════════════════════════════

    async def execute_signal(self, signal: dict) -> Optional[Dict]:
        """Execute a real trade for this user from a qualified paper signal.

        Called by UserRealRegistry.broadcast_signal() for each active user.
        Returns trade dict or None if skipped/failed.
        """
        symbol = signal.get("symbol", "")

        # 1. Qualify (Phase 4.1: now async — cohort blacklist DB check)
        # Phase 5.0.2 — upgrade to info-level so rejections are VISIBLE in
        # journald (were silent at debug, hiding why signals don't fill).
        # Bug 3d (2026-04-27): info-level was STILL invisible at our WARNING
        # log root level, so rejection histograms were impossible. Promote
        # to warning + write JSONL row to storage/qualify_rejections.jsonl
        # for offline analysis. Today's data showed 31% paper→shadow
        # conversion (85/123 rejected silently in 24h).
        #
        # 2026-04-27 CLEAN A/B TEST: bypass qualify_signal entirely for
        # shadow_live mode — every paper signal becomes a delta_shadow trade.
        # Reason: pure paper-vs-shadow execution-friction comparison without
        # the qualify gates muddying the signal pool. Live trades still
        # qualify normally (when bot_mode='live').
        # 2026-04-27 update: max_open=5 cap STILL enforced in bypass mode
        # (safety guard — prevents unbounded concurrency from confounding
        # the test with sizing/risk side-effects).
        if getattr(self, "_is_shadow_live", False):
            if len(self.open_trades) >= 5:
                qualified, reason = False, f"user_max_open:{len(self.open_trades)}/5"
            else:
                qualified, reason = True, "shadow_clean_test_bypass"
        else:
            qualified, reason = await self.qualify_signal(signal)
        if not qualified:
            logger.warning("QUALIFY_REJECT user=%s sym=%s side=%s reason=%s",
                           self.user_email or self.user_id[:8], symbol,
                           str(signal.get("side", "?")).lower(), reason)
            # Append to JSONL (best-effort; never block trading on log fail)
            try:
                import json as _json
                import datetime as _dt
                import pathlib as _pl
                _rec = {
                    "ts": _dt.datetime.utcnow().isoformat() + "Z",
                    "user_email": self.user_email or "",
                    "user_id": self.user_id[:8],
                    "symbol": symbol,
                    "side": str(signal.get("side", "")).lower(),
                    "grade": str((signal.get("metadata") or {}).get("grade", "")),
                    "scanner": str((signal.get("metadata") or {}).get("scanner", "")),
                    "regime": str((signal.get("metadata") or {}).get("regime", "")),
                    "ml_prob": float((signal.get("metadata") or {}).get("ml_probability", 0) or 0),
                    "conf": float(signal.get("confidence", 0) or 0),
                    "reason": reason,
                    "reason_class": (reason.split(":")[0] if ":" in reason else reason),
                }
                _path = _pl.Path("/home/opc/crypto-trading-bot/storage/qualify_rejections.jsonl")
                _path.parent.mkdir(parents=True, exist_ok=True)
                with _path.open("a") as _f:
                    _f.write(_json.dumps(_rec) + "\n")
            except Exception:
                pass  # never block on log write failure
            return None

        # 2. Size
        margin, leverage, lots = self.compute_size(signal)
        if lots <= 0:
            return None

        # 3. Execute on user's exchange
        meta = signal.get("metadata", {}) or {}
        side_str = (signal.get("side", "long") or "long").lower()
        order_side = "buy" if side_str == "long" else "sell"
        entry_price = signal.get("entry_price", 0)
        sl = signal.get("stop_loss", 0)
        tp = 0
        tps = signal.get("take_profits", [])
        if tps and isinstance(tps[0], (int, float)):
            tp = float(tps[0])

        try:
            # Refresh balance if never fetched — sizing is based on this.
            # Without this, compute_size() falls back to $100 estimate which
            # under-sizes (or over-sizes) the trade vs user's real equity.
            if not self._cached_balance:
                await self.refresh_balance()

            # Connect to user's exchange (sync — one-time, minor cost)
            if hasattr(self._delta, 'connect'):
                self._delta.connect()

            # Phase 4.4 — all subsequent HTTP calls go through
            # asyncio.to_thread so they don't stall the event loop.
            # Previous direct sync calls burned 300-800ms per call on the
            # main loop; with WS+candle streams pushing 7+ msgs/sec per
            # symbol, that starvation caused ticks, MFE updates, and monitor
            # checks to bunch up.

            # Resolve product_id + tick_size via mode-aware lookup.
            # BUGFIX 2026-04-21: PRODUCT_MAP entries are NESTED DICTS (with
            # demo_id/prod_id/tick_size/contract_size), not flat ints. Previously
            # this code passed the whole dict to Delta's create_order which the
            # SDK silently serialized as invalid JSON → every real trade failed
            # with the exception caught at line 364 and dropped. No trade ever
            # actually reached the exchange via UserRealManager.
            from exchange.delta_client import PRODUCT_MAP
            product_info = PRODUCT_MAP.get(symbol) or {}
            is_demo = getattr(self._delta, "mode", "demo") == "demo"
            product_id = product_info.get("demo_id" if is_demo else "prod_id")
            if not product_id:
                logger.warning(
                    "USER %s: no %s product_id for %s (unsupported on this venue)",
                    self.user_id[:8], "demo" if is_demo else "prod", symbol,
                )
                return None
            tick_size = float(
                product_info.get("tick_size_demo" if is_demo else "tick_size") or 0.01
            )
            # Phase 4.6 — capture contract_size for accurate PnL accounting.
            # Notional = lots × contract_size × price. Our prior PnL formula
            # used margin × leverage as notional, which over-stated by ~25%
            # on floor-capped small trades. True contract_size fixes it.
            _cs_key = "contract_size_demo" if is_demo else "contract_size"
            trade_contract_size = float(
                product_info.get(_cs_key) or product_info.get("contract_size", 1.0)
            ) or 1.0

            # Phase 5.6-B / T4.3 (2026-04-24) — SHADOW-LIVE BRANCH.
            # If bot_mode='shadow_live', skip all Delta calls and simulate
            # the fill from production L2 top-of-book. Full pipeline
            # (qualify → size → monitor → exit) runs; no real orders.
            # This is the mandatory validation gate before bot_mode='live'.
            if self._is_shadow_live:
                # Phase 2 — Shadow-of-Shadow forward test
                if getattr(self, "_phase2_sos_enabled", False):
                    results = []
                    for cfg in PHASE2_EXIT_CONFIGS:
                        r = await self._execute_shadow(
                            signal, symbol, side_str, order_side,
                            entry_price, sl, tp, margin, leverage, lots,
                            product_id, tick_size, trade_contract_size, meta,
                            exit_config=cfg,
                        )
                        if r:
                            results.append(r)
                    return results[0] if results else None
                return await self._execute_shadow(
                    signal, symbol, side_str, order_side,
                    entry_price, sl, tp, margin, leverage, lots,
                    product_id, tick_size, trade_contract_size, meta,
                )

            # Set leverage (Phase 4.4 — off event loop)
            try:
                await asyncio.to_thread(
                    self._delta._client.set_leverage,
                    {"product_id": product_id, "leverage": str(leverage)},
                )
            except Exception:
                pass

            # Round helper — Delta rejects off-tick orders with 400.
            def _round_to_tick(px: float, tick: float) -> float:
                if tick <= 0:
                    return round(px, 4)
                return round(round(px / tick) * tick, 10)

            def _px_str(px: float) -> str:
                return f"{_round_to_tick(px, tick_size):.10f}".rstrip("0").rstrip(".")

            # ── Phase 4.0 entry execution (paper parity) ─────────────────
            # Paper assumes zero-slip fill at signal_price. Previous code
            # placed IOC at signal ± 15bps → cost 0.19R per trade on 0.8% SL,
            # killing 75% of signals at early_kill before they could breathe.
            #
            # New pattern:
            #   Attempt 1 — post_only maker limit AT signal_price (GTC).
            #     Delta rejects if it would cross spread; otherwise rests on
            #     the book. We inspect state=closed (immediate cross-fill) OR
            #     cancel after a short probe and go to fallback.
            #   Attempt 2 — IOC limit at signal ± 5 bps (was 15). Ensures
            #     we don't miss fast moves while keeping slip bounded.
            #   If both miss, the signal is dropped (no worse than IOC today).
            fill_price = 0
            entry_exec_mode = ""
            entry_fee_usd = 0.0  # Phase 4.6 — pulled from Delta's `commission` field

            # Phase 5.3 / T2.1 (2026-04-23) — 3-TIER AGGRESSIVE-MAKER ENTRY.
            # Replaces previous [post_only @ signal_price → IOC @ ±3bps].
            # Paper vs demo forensic: IOC 3bps fallback eats 3bps of slip AND
            # pays taker (0.059% with GST). Combined cost per entry ~0.089%
            # on top of gross. Over 15 trades/day × 2 accounts = ~$0.15/day bleed.
            # New ladder (matching existing EXIT pattern at line 1390+):
            #   Tier 1: post_only @ best_bid+1tk (buy) / best_ask-1tk (sell) → 250ms probe
            #   Tier 2: post_only @ best_bid+2tk / best_ask-2tk → 200ms probe
            #   Tier 3: market order (explicit taker) — last resort
            # Needs L2 (bid/ask) from delta_ws; falls back to signal_price
            # limit if bid/ask unavailable (cold start, missing symbol).
            _bid = 0.0
            _ask = 0.0
            try:
                _dws = getattr(self._price_feed, "_delta_ws", None)
                if _dws is not None:
                    _bid = float((_dws.bids or {}).get(symbol, 0) or 0)
                    _ask = float((_dws.asks or {}).get(symbol, 0) or 0)
            except Exception:
                pass
            _tk = tick_size or 0.01

            async def _try_maker(px: float, tag: str, probe_ms: int) -> bool:
                """Place post_only limit at px. Returns True if filled.

                Batch B #5 telemetry (2026-04-26): logs structured MAKER_PROBE
                line per attempt for the 7d shadow validation window. Format:
                  MAKER_PROBE: u=<id8> sym=<sym> tag=<tag> side=<side> px=<px>
                              bid=<bid> ask=<ask> sp_tk=<spread> at_bid=<bool>
                              probe=<ms> filled=<bool> fill_px=<px> state=<s>
                              oid=<oid> [err=<exc>]
                Grep with: journalctl -u cryptobot | grep MAKER_PROBE
                """
                nonlocal fill_price, entry_exec_mode, entry_fee_usd
                # Telemetry pre-snapshot (closure-captured _bid/_ask/_tk)
                _t_bid_snap = _bid
                _t_ask_snap = _ask
                _t_sp_tk = _spread_ticks if '_spread_ticks' in dir() or _ask > _bid else 0
                try:
                    _t_sp_tk = max(0, round((_t_ask_snap - _t_bid_snap) / _tk)) if (_t_ask_snap > _t_bid_snap > 0 and _tk > 0) else 0
                except Exception:
                    _t_sp_tk = 0
                _t_at_bid = (order_side == "buy" and _t_bid_snap > 0 and px <= _t_bid_snap) or \
                            (order_side == "sell" and _t_ask_snap > 0 and px >= _t_ask_snap)

                _t_filled = False
                _t_fill_px = 0.0
                _t_state = ""
                _t_oid = ""
                _t_err = ""
                try:
                    # Phase 5.9-A: client_order_id for idempotent retries.
                    # Phase 5.20-A1: UUID-based COID (was timestamp%10^10 — collision risk)
                    _coid_me = _new_coid("vn_eme")
                    params = {
                        "product_id": product_id,
                        "size": lots,
                        "side": order_side,
                        "order_type": "limit_order",
                        "limit_price": _px_str(px),
                        "time_in_force": "gtc",
                        "post_only": "true",
                        "reduce_only": "false",
                        "client_order_id": _coid_me,
                    }
                    resp = await asyncio.to_thread(
                        self._delta._client.create_order, params,
                    )
                    resp = resp if isinstance(resp, dict) else {}
                    state = str(resp.get("state", "")).lower()
                    oid = resp.get("id")
                    fill = float(resp.get("average_fill_price", 0) or 0)
                    _t_state = state
                    _t_oid = str(oid or "")
                    # Immediate cross-fill (rare at aggressive-maker price)
                    if state == "closed" and fill > 0:
                        fill_price = fill
                        entry_exec_mode = tag
                        entry_fee_usd = abs(float(resp.get("paid_commission") or resp.get("commission") or 0))
                        _t_filled = True
                        _t_fill_px = fill
                        return True
                    # Rest briefly, then cancel
                    if probe_ms > 0:
                        await asyncio.sleep(probe_ms / 1000.0)
                    if oid:
                        try:
                            cancel_resp = await asyncio.to_thread(
                                self._delta._client.cancel_order,
                                product_id=product_id, order_id=oid,
                            )
                            cancel_resp = cancel_resp if isinstance(cancel_resp, dict) else {}
                            # If cancel reports filled/closed, we got lucky
                            c_state = str(cancel_resp.get("state", "")).lower()
                            c_fill = float(cancel_resp.get("average_fill_price", 0) or 0)
                            if c_state == "closed" and c_fill > 0:
                                fill_price = c_fill
                                entry_exec_mode = tag
                                entry_fee_usd = abs(float(cancel_resp.get("paid_commission") or cancel_resp.get("commission") or 0))
                                _t_filled = True
                                _t_fill_px = c_fill
                                _t_state = "closed_on_cancel"
                                return True
                        except Exception as cex:
                            _t_err = "cancel:" + str(cex)[:80]
                except Exception as exc:
                    _t_err = str(exc)[:120]
                    logger.debug("USER %s: maker tier %s miss: %s",
                                 self.user_id[:8], tag, exc)
                finally:
                    # Single structured telemetry line per attempt — fire & forget
                    try:
                        logger.info(
                            "MAKER_PROBE: u=%s sym=%s tag=%s side=%s px=%s "
                            "bid=%s ask=%s sp_tk=%s at_bid=%s probe=%s "
                            "filled=%s fill_px=%s state=%s oid=%s%s",
                            self.user_id[:8], symbol, tag, order_side, px,
                            _t_bid_snap, _t_ask_snap, _t_sp_tk, int(_t_at_bid), probe_ms,
                            int(_t_filled), _t_fill_px, _t_state, _t_oid[:16],
                            (" err=" + _t_err) if _t_err else "",
                        )
                    except Exception:
                        pass
                return False

            # Use L2 only when bid/ask are valid and spread ≥ 2 ticks
            _spread_ticks = 0
            if _bid > 0 and _ask > 0 and _ask > _bid:
                _spread_ticks = round((_ask - _bid) / _tk)

            # Phase 5.14 + 5.19 (2026-04-25) — MAKER MODE SELECTOR.
            # Modes:
            #   'standard'   → 500/350ms probe, 1x bp offset (legacy default)
            #   'patient'    → 2500/1750ms probe, 2.5x bp offset (5.14)
            #   'aggressive' → legacy alias for 'patient' (kept for back-compat)
            #   'l2_aware'   → walk L2 book to first level with depth >= our_size (5.19)
            #   'multimode'  → per-trade A/B/C: signal_id hash → standard|patient|l2_aware
            #
            # Per-trade randomization (multimode) gives us 3-arm parallel testing:
            # same user, same admission, same regime → clean attribution. Tag
            # `maker_mode_used` in metadata for post-hoc per-mode aggregation.
            from execution.maker_modes import (
                select_mode_for_signal, MODE_NAME, MODE_L2_AWARE,
                compute_l2_aware_price, parse_l2_book_from_ws,
            )
            _patience_mode = getattr(self, "maker_patience_mode", "standard")
            _signal_id_for_mode = str(
                signal.get("id") or signal.get("signal_id")
                or f"{symbol}_{order_side}_{int(time.time())}"
            )
            _mode_id, _mode_cfg = select_mode_for_signal(_patience_mode, _signal_id_for_mode)
            _probe_t1_ms = _mode_cfg.probe_t1_ms
            _probe_t2_ms = _mode_cfg.probe_t2_ms
            _offset_mult = _mode_cfg.offset_mult
            _use_l2_depth = _mode_cfg.use_l2_depth
            _mode_name = _mode_cfg.mode_name

            # Phase 5.5-N1 (2026-04-24) — PERCENTAGE-BASED MAKER OFFSET.
            # Old tick-based: _bid + 1 * tick_size (0.0001 on SOL = 0.0001%)
            # causes post_only rejection on any ambient price movement
            # → 100% fallthrough to market_taker on low-tick symbols.
            # New percentage-based: 1bp / 2bp of price scales universally.
            # SOL ~$86: 1bp = 0.0086, rounded to 86 ticks → real offset.
            # BTC ~$78k: 1bp = $7.80, = 15.6 ticks at 0.5 tick → real offset.
            # ETH ~$2330: 1bp = $0.23, = 4.6 ticks at 0.05 → real offset.
            # Universal: whether tick is 0.0001 or 0.5, the offset is
            # meaningful against intra-tick price movement.
            # Phase 5.5.2 (2026-04-24) — SYMBOL-SPECIFIC OFFSETS + EXTENDED PROBES.
            # Apr 24 observation: 7+ ETH/BTC entries under 5.5 / 5.5.1 all fell
            # to market_taker despite the 1bp + clamp. Root cause on tight-
            # spread symbols: clamp falls back to ask-1tk (same as old tick-
            # based), and 250ms probe is too short for the order to rest +
            # get filled. Symbol-specific offsets give alts wider room (they
            # have wider real spreads), and doubled probe windows give orders
            # more time to rest on the book before we cancel.
            # 2026-04-26 Batch A #6: SOL bumped 2.0 → 3.0 bp.
            # PROD-L2 probe shows SOL fill rate 23% vs BTC 60% / ETH 47% — root cause
            # is wider SOL spread (9-11 ticks) means our 2bp lands inside spread but
            # NOT at top of bid; market needs to come down to us, slow to fill.
            # 3bp gets closer to ask without crossing on most spreads. Validate in
            # 7d shadow window before further tuning.
            _symbol_offset_bp_map = {
                "SOL/USDT": 3.0, "SHIB/USDT": 2.5, "DOGE/USDT": 2.0,
                "PEPE/USDT": 2.5, "BONK/USDT": 2.5,
                "ETH/USDT": 1.5, "BTC/USDT": 1.0,
                "XRP/USDT": 1.5, "ADA/USDT": 2.0,
            }
            _sym_bp = _symbol_offset_bp_map.get(symbol, 1.5)  # default 1.5bp
            # Phase 5.14: scale by patience mode multiplier
            _sym_bp_eff = _sym_bp * _offset_mult
            _bid_offset_pct = _sym_bp_eff / 10000.0
            _bid_offset_pct_t2 = (_sym_bp_eff * 2.0) / 10000.0
            if _bid > 0 and _ask > 0 and _ask > _bid:
                # Phase 5.19: L2-aware tier 1 placement walks the book.
                # Find first price level with cumulative depth >= our_size,
                # post_only AT that level (joining existing depth instead of
                # creating a new top-of-book that's likely to be skipped).
                if _use_l2_depth:
                    _bids_lvls, _asks_lvls = parse_l2_book_from_ws(_dws, symbol)
                    # Fallback if L2 cache empty: degrade to bp-based offset
                    if _bids_lvls and _asks_lvls:
                        _fallback_px = _bid + _tk if order_side == "buy" else _ask - _tk
                        _px1, _l2_tag = compute_l2_aware_price(
                            side=order_side,
                            bids=_bids_lvls, asks=_asks_lvls,
                            our_size=float(lots),
                            fallback_px=_fallback_px,
                            tick_size=_tk,
                        )
                        _tag1 = f"maker_l2_t1_{_l2_tag}"
                    else:
                        # L2 unavailable — degrade to bp-offset placement
                        if order_side == "buy":
                            _raw_px1 = min(_bid * (1.0 + _bid_offset_pct), _ask - _tk)
                        else:
                            _raw_px1 = max(_ask * (1.0 - _bid_offset_pct), _bid + _tk)
                        _px1 = round(round(_raw_px1 / _tk) * _tk, 10)
                        _tag1 = f"maker_l2_t1_no_book"
                else:
                    # --- Tier 1: bp-offset placement (standard / patient) ---
                    # Phase 5.5.1 clamp preserved — never cross opposite side.
                    if order_side == "buy":
                        _raw_px1 = _bid * (1.0 + _bid_offset_pct)
                        _raw_px1 = min(_raw_px1, _ask - _tk)  # never cross ask
                    else:
                        _raw_px1 = _ask * (1.0 - _bid_offset_pct)
                        _raw_px1 = max(_raw_px1, _bid + _tk)  # never cross bid
                    _px1 = round(round(_raw_px1 / _tk) * _tk, 10)
                    _tag1 = f"maker_aggr_t1_{_mode_name}"

                # Multi-arm metadata: log which mode was used at decision time.
                # This lets us aggregate per-mode performance later.
                signal.setdefault("metadata", {})["maker_mode_used"] = _mode_name
                signal["metadata"]["maker_mode_id"] = _mode_id
                await _try_maker(_px1, _tag1, _probe_t1_ms)

                # --- Tier 2: deeper placement (if tier 1 missed) ---
                if fill_price <= 0:
                    if _use_l2_depth:
                        # Tier 2 L2 placement: target 2× depth (more aggressive
                        # = deeper inside book). If we couldn't fill at 1× depth
                        # at our_size, jump to a level that has 2× our_size.
                        _bids_lvls, _asks_lvls = parse_l2_book_from_ws(_dws, symbol)
                        if _bids_lvls and _asks_lvls:
                            _fallback_px = _bid + _tk if order_side == "buy" else _ask - _tk
                            _px2, _l2_tag2 = compute_l2_aware_price(
                                side=order_side,
                                bids=_bids_lvls, asks=_asks_lvls,
                                our_size=float(lots) * 2.0,
                                fallback_px=_fallback_px,
                                tick_size=_tk,
                            )
                            _tag2 = f"maker_l2_t2_{_l2_tag2}"
                        else:
                            # L2 unavailable — fall back to 2× bp offset
                            if order_side == "buy":
                                _raw_px2 = min(_bid * (1.0 + _bid_offset_pct_t2), _ask - _tk)
                            else:
                                _raw_px2 = max(_ask * (1.0 - _bid_offset_pct_t2), _bid + _tk)
                            _px2 = round(round(_raw_px2 / _tk) * _tk, 10)
                            _tag2 = "maker_l2_t2_no_book"
                    else:
                        # bp-offset tier 2 (standard / patient)
                        if order_side == "buy":
                            _raw_px2 = _bid * (1.0 + _bid_offset_pct_t2)
                            _raw_px2 = min(_raw_px2, _ask - _tk)
                        else:
                            _raw_px2 = _ask * (1.0 - _bid_offset_pct_t2)
                            _raw_px2 = max(_raw_px2, _bid + _tk)
                        _px2 = round(round(_raw_px2 / _tk) * _tk, 10)
                        _tag2 = f"maker_aggr_t2_{_mode_name}"
                    await _try_maker(_px2, _tag2, _probe_t2_ms)
            elif entry_price > 0:
                # Bid/ask unavailable (cold WS cache) — try post_only at signal
                # as safety path (equivalent to old Phase 4.1 Attempt 1).
                await _try_maker(entry_price, "maker", 200)

            # --- Tier 3: market order (explicit taker) ---
            # Reserved for the case where all maker attempts missed. Uses
            # market_order to let Delta's matching engine fill at best
            # available — same economics as old IOC 3bps but with no
            # silent limit-cap. Known cost: full taker fee + whatever
            # slippage the book gives us.
            if fill_price <= 0:
                try:
                    # Phase 5.9-A: client_order_id for idempotent retries.
                    _coid_te = _new_coid("vn_ete")
                    mkt_params = {
                        "product_id": product_id,
                        "size": lots,
                        "side": order_side,
                        "order_type": "market_order",
                        "time_in_force": "ioc",
                        "reduce_only": "false",
                        "client_order_id": _coid_te,
                    }
                    mkt_resp = await asyncio.to_thread(
                        self._delta._client.create_order, mkt_params,
                    )
                    mkt_resp = mkt_resp if isinstance(mkt_resp, dict) else {}
                    f = float(mkt_resp.get("average_fill_price", 0) or 0)
                    if f <= 0:
                        f = float(mkt_resp.get("price", 0) or 0)
                    if f > 0:
                        fill_price = f
                        entry_exec_mode = "market_taker"
                        entry_fee_usd = abs(float(mkt_resp.get("paid_commission") or mkt_resp.get("commission") or 0))
                        if entry_fee_usd <= 0:
                            entry_fee_usd = fill_price * lots * trade_contract_size * 0.0005
                except Exception as mkt_exc:
                    logger.error("USER %s: market fallback failed: %s",
                                 self.user_id[:8], mkt_exc)

            if fill_price <= 0:
                logger.info("USER %s: %s not filled", self.user_id[:8], symbol)
                return None

            # 4. Recalculate SL from fill price
            if fill_price != entry_price and entry_price > 0:
                sl_shift = fill_price - entry_price
                sl = sl + sl_shift

            # 5. Record trade
            trade_id = f"u_{self.user_id[:8]}_{int(time.time()*1000)}"
            initial_risk = abs(fill_price - sl) if sl > 0 else fill_price * 0.01

            # Phase 5.3 / T4.1 — snapshot 8h funding rate at entry so _close_trade
            # can compute accurate funding cost. One-shot, fail-silent.
            _funding_rate_snap = 0.0
            try:
                _fr_info = await asyncio.to_thread(
                    getattr(self._delta, "get_funding_rate", lambda s: None),
                    symbol,
                )
                if _fr_info and isinstance(_fr_info, dict):
                    _funding_rate_snap = float(_fr_info.get("funding_rate", 0) or 0)
            except Exception:
                _funding_rate_snap = 0.0

            trade = UserTradeRecord(
                trade_id=trade_id,
                user_id=self.user_id,
                symbol=symbol,
                side=side_str,
                entry_price=fill_price,
                stop_loss=sl,
                take_profit=tp,
                position_size=lots,
                margin=margin,
                leverage=leverage,
                opened_at=time.time(),
                initial_risk=initial_risk,
                scanner=meta.get("scanner", meta.get("setup_type", "")),
                grade=signal.get("grade", ""),
                trade_type=meta.get("trade_type", "SCALP"),
                product_id=int(product_id),
                tick_size=tick_size,
                fee_type=("maker" if entry_exec_mode == "maker" else "taker"),
                regime=meta.get("regime", ""),
                ml_prob=float(meta.get("ml_probability", 0) or 0),
                # Phase 4.6 — accurate PnL fields
                contract_size=trade_contract_size,
                entry_fee_usd=float(entry_fee_usd),
                # Phase 5.3 / T4.1 — funding rate snapshot at entry
                funding_rate_at_entry=float(_funding_rate_snap),
            )
            # Phase 5.19 — tag the trade with which maker mode was used so we
            # can later aggregate per-mode performance. Set as ad-hoc attrs
            # to avoid touching the dataclass schema (persisted via close_meta).
            trade.maker_mode_used = _mode_name
            trade.maker_mode_id   = _mode_id
            trade.entry_exec_mode = entry_exec_mode or ""
            self.open_trades[trade_id] = trade

            # 6. Phase 4.0 — Server-side stop_loss safety net.
            #    Places a reduce_only stop_market on Delta at the initial SL.
            #    This protects against monitor failure (process crash, WS lag)
            #    with bounded exchange-level slippage vs 10+bps reactive market.
            #    The monitor still owns the trail/kill/pullback exits; when it
            #    decides to close, it cancels this order first.
            try:
                sl_side = "sell" if order_side == "buy" else "buy"
                # Phase 5.9-A (2026-04-24) — stop_trigger_method=mark_price.
                # Delta's default trigger is last_traded_price, which is noisy
                # top-of-book jitter. On testnet this caused 965+
                # immediate_execution_stop_order rejections in 7 min on one
                # SOL short (2026-04-24 10:33-10:40). mark_price is smoothed
                # (index-adjusted) — rejection rate drops dramatically.
                # client_order_id: idempotency key for safe retries.
                _coid_si = _new_coid("vn_si")  # initial SL — UUID-based
                stop_params = {
                    "product_id": product_id,
                    "size": lots,
                    "side": sl_side,
                    "order_type": "market_order",
                    "stop_order_type": "stop_loss_order",
                    "stop_price": _px_str(sl),
                    "stop_trigger_method": "mark_price",
                    "reduce_only": "true",
                    "client_order_id": _coid_si,
                }
                stop_resp = await asyncio.to_thread(
                    self._delta._client.create_order, stop_params,
                )
                stop_resp = stop_resp if isinstance(stop_resp, dict) else {}
                sid = stop_resp.get("id")
                if sid:
                    trade.server_stop_id = int(sid)
                else:
                    # Phase T3.1 (2026-04-24) — TWO-PHASE COMMIT with retry + rollback.
                    # Initial SL creation failed. The entry fill is ON the
                    # books but the position is UNPROTECTED. Previously we
                    # logged a warning and continued — which left a naked
                    # entry vulnerable to monitor death or WS lag.
                    # New behavior:
                    #   1. Retry 2× with widened SL (add 0.3% buffer) in case
                    #      immediate_execution jitter rejected the initial.
                    #   2. If all retries fail → EMERGENCY CLOSE at market.
                    #      Better to take a small fee-wall loss than carry
                    #      naked exposure.
                    _t3_retries_used = 0
                    _sl_base = float(sl)
                    for _t3_attempt in range(2):
                        _t3_retries_used += 1
                        _t3_buf = _sl_base * 0.003 * (_t3_attempt + 1)  # 0.3% then 0.6%
                        _t3_sl = _sl_base + _t3_buf if sl_side == "buy" else _sl_base - _t3_buf
                        # Keep protective side: for short (sl_side=buy) widen UP; for long widen DOWN
                        _t3_coid = _new_coid(f"vn_t3r{_t3_attempt}")
                        try:
                            _t3_resp = await asyncio.to_thread(
                                self._delta._client.create_order,
                                {
                                    "product_id": product_id,
                                    "size": lots,
                                    "side": sl_side,
                                    "order_type": "market_order",
                                    "stop_order_type": "stop_loss_order",
                                    "stop_price": _px_str(_t3_sl),
                                    "stop_trigger_method": "mark_price",
                                    "reduce_only": "true",
                                    "client_order_id": _t3_coid,
                                },
                            )
                            _t3_resp = _t3_resp if isinstance(_t3_resp, dict) else {}
                            _t3_sid = _t3_resp.get("id")
                            if _t3_sid:
                                trade.server_stop_id = int(_t3_sid)
                                trade.stop_loss = _t3_sl  # update local to match server
                                logger.warning(
                                    "T3.1 SL_RECOVERED user=%s %s attempt=%d sl=%.5f sid=%s",
                                    self.user_id[:8], symbol, _t3_attempt + 1, _t3_sl, _t3_sid,
                                )
                                sid = _t3_sid
                                break
                        except Exception as _t3_exc:
                            logger.debug("T3.1 retry %d exc: %s", _t3_attempt + 1, _t3_exc)
                        await asyncio.sleep(0.05)

                    if not trade.server_stop_id:
                        # All retries exhausted → EMERGENCY CLOSE.
                        logger.critical(
                            "T3.1 ROLLBACK: user=%s %s SL unplaceable after %d retries — "
                            "closing naked position at market",
                            self.user_id[:8], symbol, _t3_retries_used,
                        )
                        try:
                            _rollback_side = "sell" if order_side == "buy" else "buy"
                            _rollback_coid = _new_coid("vn_rb")
                            _rollback_resp = await asyncio.to_thread(
                                self._delta._client.create_order,
                                {
                                    "product_id": product_id,
                                    "size": lots,
                                    "side": _rollback_side,
                                    "order_type": "market_order",
                                    "reduce_only": "true",
                                    "client_order_id": _rollback_coid,
                                },
                            )
                            _rollback_resp = _rollback_resp if isinstance(_rollback_resp, dict) else {}
                            _rb_fill = float(_rollback_resp.get("average_fill_price", 0) or 0)
                            logger.critical(
                                "T3.1 ROLLBACK CLOSED: user=%s %s fill=%.5f coid=%s",
                                self.user_id[:8], symbol, _rb_fill, _rollback_coid[:16],
                            )
                            # Remove from open_trades so monitor doesn't chase a non-existent position.
                            self.open_trades.pop(trade_id, None)
                            # Mark metadata for post-mortem.
                            trade.metadata = trade.metadata or {}
                            trade.metadata["t3_rollback"] = True
                            trade.metadata["t3_retries"] = _t3_retries_used
                            return None  # Signal caller the entry did not commit.
                        except Exception as _rb_exc:
                            # Rollback close ALSO failed — worst case, naked position.
                            # Log loudly; operator must manually intervene.
                            logger.critical(
                                "T3.1 ROLLBACK FAILED: user=%s %s %s — "
                                "NAKED POSITION, MANUAL CLOSE REQUIRED",
                                self.user_id[:8], symbol, _rb_exc,
                            )
            except Exception as stop_exc:
                logger.warning(
                    "USER %s: server-side SL placement failed for %s (%s) — "
                    "monitor will handle exits unassisted",
                    self.user_id[:8], symbol, stop_exc,
                )

            # Phase 5.0 — CRITICAL-level loud marker when LIVE (real $$$).
            _mode_tag = "💰 LIVE" if self._is_live else "DEMO"
            _log_fn = logger.critical if self._is_live else logger.warning
            _log_fn(
                "%s USER REAL ENTRY: %s %s %s | fill=%.4f sl=%.4f | margin=$%.2f lots=%d lev=%dx | "
                "grade=%s ml=%.2f regime=%s | exec=%s srv_sl=%s",
                _mode_tag, self.user_email, symbol, side_str, fill_price, sl, margin, lots, leverage,
                trade.grade or "?", trade.ml_prob or 0, trade.regime or "?",
                entry_exec_mode or "?", trade.server_stop_id or "none",
            )

            # 7. Record to DB
            if self._db_pool:
                try:
                    await self._record_trade_db(trade, "open")
                except Exception as e:
                    logger.error("USER %s: DB record failed: %s", self.user_id[:8], e)

            # 8. Start independent monitoring
            # 2026-04-27 — retain task ref to prevent GC-induced orphaning
            self._monitor_tasks[trade_id] = asyncio.create_task(self._monitor_trade(trade_id))
            self._monitor_tasks[trade_id].add_done_callback(
                lambda _t, _tid=trade_id: self._monitor_tasks.pop(_tid, None)
            )

            return {"trade_id": trade_id, "fill_price": fill_price,
                    "symbol": symbol, "exec_mode": entry_exec_mode}

        except Exception as e:
            logger.error("USER %s: execution failed for %s: %s", self.user_id[:8], symbol, e)
            return None

    # ══════════════════════════════════════════════════════════════
    # SHADOW-LIVE EXECUTION (T4.3 — Phase 5.6-B)
    # ══════════════════════════════════════════════════════════════

    async def _execute_shadow(self, signal, symbol, side_str, order_side,
                              entry_price, sl, tp, margin, leverage, lots,
                              product_id, tick_size, trade_contract_size, meta,
                              exit_config=None):
        """T4.3 Shadow-live execution — NO Delta calls.

        Simulates a taker fill from current production L2 top-of-book:
          - BUY hits best_ask (worst-case taker)
          - SELL hits best_bid
        Applies 0.059% × GST taker fee.
        Creates a UserTradeRecord flagged _is_shadow=True so monitor and
        _close_trade skip all Delta interactions.
        Writes to user_trades with trade_type='shadow'.

        Phase 2 (2026-04-27): if `exit_config` is supplied, attaches it to
        trade._exit_config for the monitor loop to consult, and persists
        the config_id + summary to metadata for later leaderboard analysis.
        """
        # Read production L2 from WS cache
        _dws = getattr(self._price_feed, "_delta_ws", None)
        book = _dws.l2_orderbook.get(symbol) if _dws else None
        if not book or not book.get("bids") or not book.get("asks"):
            logger.warning(
                "SHADOW_SKIP user=%s %s — no L2 data yet",
                self.user_id[:8], symbol,
            )
            return None

        # Taker fill simulation (conservative — real maker rate will be better)
        if order_side == "buy":
            shadow_fill = float(book["asks"][0][0])
        else:
            shadow_fill = float(book["bids"][0][0])

        # Recalc SL from shadow fill (preserve R-distance)
        if shadow_fill != entry_price and entry_price > 0:
            sl = sl + (shadow_fill - entry_price)

        # Fee: 0.05% × 1.18 GST = 0.059% taker
        notional = shadow_fill * lots * trade_contract_size
        shadow_entry_fee = notional * 0.00059

        # Snapshot funding (fail-safe)
        _funding_rate_snap = 0.0
        try:
            _fr_info = await asyncio.to_thread(
                getattr(self._delta, "get_funding_rate", lambda s: None),
                symbol,
            )
            if _fr_info and isinstance(_fr_info, dict):
                _funding_rate_snap = float(_fr_info.get("funding_rate", 0) or 0)
        except Exception:
            pass

        # Build trade record. Include exit_config_id in trade_id when
        # phase2 sweep is active so 5 simultaneous virtual trades for the
        # same signal get unique IDs (otherwise time.time()*1000 collisions
        # can occur within the same ms).
        _cfg_suffix = f"_{exit_config['id']}" if exit_config else ""
        trade_id = f"shadow_{self.user_id[:8]}_{int(time.time() * 1000)}{_cfg_suffix}"
        initial_risk = abs(shadow_fill - sl) if sl > 0 else shadow_fill * 0.01
        trade = UserTradeRecord(
            trade_id=trade_id,
            user_id=self.user_id,
            symbol=symbol,
            side=side_str,
            entry_price=shadow_fill,
            stop_loss=sl,
            take_profit=tp,
            position_size=lots,
            margin=margin,
            leverage=leverage,
            opened_at=time.time(),
            initial_risk=initial_risk,
            scanner=meta.get("scanner", meta.get("setup_type", "")),
            grade=signal.get("grade", ""),
            trade_type=meta.get("trade_type", "SCALP"),
            product_id=int(product_id),
            tick_size=tick_size,
            fee_type="taker",
            regime=meta.get("regime", ""),
            ml_prob=float(meta.get("ml_probability", 0) or 0),
            contract_size=trade_contract_size,
            entry_fee_usd=float(shadow_entry_fee),
            funding_rate_at_entry=float(_funding_rate_snap),
        )
        # Shadow flag — monitor/close check this to skip Delta interactions
        trade._is_shadow = True
        trade.server_stop_id = None  # no Delta safety net needed

        # Phase 2 — attach exit config for monitor loop to consult
        if exit_config is not None:
            trade._exit_config = exit_config
            trade._is_phase2_virtual = True

        self.open_trades[trade_id] = trade

        # Persist to DB with trade_type='shadow'.
        # Phase 2: stamp config metadata so leaderboard SQL can group by it.
        if exit_config is not None:
            try:
                if not isinstance(meta, dict):
                    meta = {}
                meta["exit_config_id"] = exit_config["id"]
                meta["exit_config_summary"] = (
                    f"max_age={exit_config['max_age_sec']}s "
                    f"trail={exit_config['trail_trigger']}R/{exit_config['trail_lock']:.0%} "
                    f"dead_kill={exit_config['dead_kill_R']} "
                    f"tp_R={exit_config['tp_R']}"
                )
                meta["is_phase2_virtual"] = True
            except Exception:
                pass

        await self._record_trade_db(
            trade, status="open", reason="",
            exit_price=0.0, pnl_usd=0.0, fees_usd=0.0, gross_pnl=0.0,
        )

        logger.warning(
            "🌓 SHADOW ENTRY: %s %s %s | fill=%.4f sl=%.4f | margin=$%.2f lots=%d lev=%dx | "
            "grade=%s ml=%.2f regime=%s | L2: bid=%.4f@%d ask=%.4f@%d spread=%.4f",
            self.user_email, symbol, side_str,
            shadow_fill, sl, margin, lots, leverage,
            signal.get("grade", ""), float(meta.get("ml_probability", 0) or 0),
            meta.get("regime", ""),
            float(book["bids"][0][0]), int(book["bids"][0][1]),
            float(book["asks"][0][0]), int(book["asks"][0][1]),
            float(book["asks"][0][0]) - float(book["bids"][0][0]),
        )

        # Spawn monitor — existing _monitor_trade logic handles shadow naturally
        # (server_stop_id=None means no Delta SL updates; _close_trade branches on _is_shadow)
        # 2026-04-27 — retain task ref to prevent GC-induced orphaning
        self._monitor_tasks[trade_id] = asyncio.create_task(self._monitor_trade(trade_id))
        self._monitor_tasks[trade_id].add_done_callback(
            lambda _t, _tid=trade_id: self._monitor_tasks.pop(_tid, None)
        )

        return {
            "trade_id": trade_id,
            "fill_price": shadow_fill,
            "symbol": symbol,
            "exec_mode": "shadow_taker",
            "is_shadow": True,
        }

    # ══════════════════════════════════════════════════════════════
    # MONITORING (per-trade, independent from paper)
    # ══════════════════════════════════════════════════════════════

    async def _monitor_trade(self, trade_id: str):
        """Independent 500ms monitoring loop for a user's real trade.

        Checks SL, trail, time decay, early kill — same logic as global
        real manager but using THIS user's settings.
        """
        # Track first-tick + no-price counters so we can fail-closed instead
        # of dead-locking on a permanently broken price feed (root cause of
        # 18 stuck shadow trades on 2026-04-26: WS only feeding 3 symbols
        # while REST polling failed auth → monitors looped on price=0 forever).
        _no_price_streak = 0
        _no_price_warned = False
        try:
            while True:
                trade = self.open_trades.get(trade_id)
                if not trade:
                    break

                # Get current price
                price = 0
                if self._price_feed:
                    prices = getattr(self._price_feed, '_ws_prices', {}) or {}
                    price = prices.get(trade.symbol, 0)

                if price <= 0:
                    _no_price_streak += 1
                    # Warn once after 60s of no price so the gap is visible
                    if _no_price_streak == 60 and not _no_price_warned:
                        _no_price_warned = True
                        logger.warning(
                            "MONITOR_NO_PRICE: %s %s shadow=%s — 60s without price tick "
                            "(_price_feed=%s, _ws_prices keys=%d). Will force-close at age>2×max_age.",
                            self.user_email, trade.symbol,
                            getattr(trade, "_is_shadow", False),
                            type(self._price_feed).__name__ if self._price_feed else "None",
                            len(getattr(self._price_feed, '_ws_prices', {}) or {})
                                if self._price_feed else 0,
                        )
                    # Failsafe: if we've gone >2× max_age (default 30 min for SCALP)
                    # without ANY price tick AND _no_price_streak shows the feed is
                    # genuinely dead, mark the trade closed at entry (zero-PnL) so
                    # the BOOK doesn't grow unbounded.
                    age_sec = max(0, time.time() - float(trade.opened_at or 0))
                    _t_type = (getattr(trade, "trade_type", "SCALP") or "SCALP").upper()
                    # 2026-04-27 — matched to primary max_age tightening
                    # (see line ~2296 comment). Failsafe fires at 2× max_age
                    # = 1200s (20min) for SCALP when no price for 60s+ —
                    # gives some buffer beyond the primary 10min cap.
                    _max_age_for_type = 600 if _t_type == "SCALP" else 3600
                    if (_no_price_streak > 60 and age_sec > _max_age_for_type * 2):
                        logger.warning(
                            "MONITOR_FORCE_CLOSE: %s %s — no price for %ds, age=%dm, "
                            "force-closing at entry (no_price_orphan_kill)",
                            self.user_email, trade.symbol, _no_price_streak, age_sec / 60,
                        )
                        if getattr(trade, "_is_shadow", False):
                            try:
                                await self._close_shadow(trade, trade.entry_price,
                                                         "no_price_orphan_kill")
                            except Exception as _e:
                                logger.error("force-close shadow failed: %s", _e)
                        # Drop the trade from in-memory set so loop exits next iter
                        self.open_trades.pop(trade.trade_id, None)
                        break
                    await asyncio.sleep(1)
                    continue
                # Reset streak as soon as a real price comes in
                _no_price_streak = 0

                side = trade.side.lower()
                entry = trade.entry_price
                sl = trade.stop_loss
                risk = trade.initial_risk or abs(entry - sl) or entry * 0.01

                # Current R
                if side == "long":
                    current_r = (price - entry) / risk if risk > 0 else 0
                else:
                    current_r = (entry - price) / risk if risk > 0 else 0

                # Update MFE from SPOT tick
                if current_r > trade.peak_mfe_r:
                    trade.peak_mfe_r = current_r

                # Phase 4.3 (2026-04-22) — Candle-high MFE patch.
                # Observed today: REST polling fallback produces 5-10s spot
                # gaps. Paper's signal_tracker catches intraband spikes
                # via candle highs; our demo monitor was spot-only → miss
                # every ~16s spike → trail logic never triggers.
                #
                # ⚠️ NO-DEGRADE GUARD: a candle's `high` is the high since
                # the BAR started, not since OUR TRADE started. If we enter
                # mid-bar after a pre-entry pump, using bar.high would
                # credit phantom favor → fake BE lock → exit at fake
                # trail_profit. That's a silent regression invisible to the
                # canary (looks like wins). Only trust candles whose
                # bar_start >= trade.opened_at (bar fully post-entry), or
                # whose current close has already cleared entry in our
                # direction (so the high is definitionally post-entry too).
                #
                # Two sources of candle data (preference order):
                #   1. DeltaWebSocket native (self._price_feed._delta_ws.candles_1m)
                #   2. signal_tracker._recent_candles (REST fallback)
                # Use whichever has the higher bar_r. Can only RAISE
                # peak_mfe_r, never lower.
                try:
                    _bar_high = 0.0
                    _bar_low = 1e12
                    _t_open = float(trade.opened_at or 0)

                    _dws = getattr(self._price_feed, "_delta_ws", None)
                    if _dws is not None:
                        _wbar = getattr(_dws, "candles_1m", {}).get(trade.symbol)
                        if _wbar:
                            # bar_start is microseconds epoch (Delta convention)
                            _bs = float(_wbar.get("bar_start", 0) or 0) / 1_000_000.0
                            _bh = float(_wbar.get("high", 0) or 0)
                            _bl = float(_wbar.get("low", 0) or 0)
                            _bc = float(_wbar.get("close", 0) or 0)
                            # Trust fully-post-entry bars freely.
                            if _bs >= _t_open and _bs > 0:
                                _bar_high = max(_bar_high, _bh)
                                if _bl > 0: _bar_low = min(_bar_low, _bl)
                            # For a mid-bar entry: only use high if the
                            # live close is STILL above entry in our favour
                            # (monotonic-favour proxy — high reached AFTER
                            # entry in the bar's unfolding).
                            elif _bs > 0 and _bh > 0:
                                if side == "long" and _bc > entry and _bh > entry:
                                    _bar_high = max(_bar_high, _bh)
                                elif side != "long" and _bc < entry and _bl > 0 and _bl < entry:
                                    _bar_low = min(_bar_low, _bl)

                    _tracker = getattr(self._price_feed, "_signal_tracker", None)
                    _cache = getattr(_tracker, "_recent_candles", None) if _tracker else None
                    if _cache is not None:
                        _cdf = _cache.get(trade.symbol)
                        if _cdf is not None and len(_cdf) >= 1:
                            _last = _cdf.iloc[-1]
                            # Best-effort timestamp check on REST candles;
                            # DataFrame index is typically bar_close. Parse
                            # defensively — if we can't confirm post-entry,
                            # fall back to the monotonic-favour proxy.
                            _rest_bar_ts = 0.0
                            try:
                                _idx = _cdf.index[-1]
                                if hasattr(_idx, "timestamp"):
                                    _rest_bar_ts = float(_idx.timestamp())
                            except Exception:
                                pass
                            _rh = float(_last["high"])
                            _rl = float(_last["low"]) if "low" in _last else 0.0
                            _rc = float(_last["close"]) if "close" in _last else 0.0
                            if _rest_bar_ts >= _t_open:
                                _bar_high = max(_bar_high, _rh)
                                if _rl > 0: _bar_low = min(_bar_low, _rl)
                            else:
                                # mid-bar guard same as WS path
                                if side == "long" and _rc > entry and _rh > entry:
                                    _bar_high = max(_bar_high, _rh)
                                elif side != "long" and _rc < entry and _rl > 0 and _rl < entry:
                                    _bar_low = min(_bar_low, _rl)

                    if risk > 0 and _bar_high > 0:
                        if side == "long":
                            _bar_r = (_bar_high - entry) / risk
                        else:
                            _bar_r = (entry - _bar_low) / risk if _bar_low < 1e12 else 0
                        if _bar_r > trade.peak_mfe_r:
                            trade.peak_mfe_r = _bar_r

                    # Phase 5.2 (2026-04-23) — TICK-LEVEL MFE override.
                    # delta_ws.tick_highs/tick_lows updates on every ticker
                    # tick (~10 Hz on liquid pairs). The 1m candle high lags
                    # because Delta's candlestick channel updates at 1-2 Hz
                    # and the OHLC fields only refresh on the bar-update
                    # message, not on every internal tick.
                    # Guard: only trust tick window if window_start >=
                    # trade.opened_at (post-entry-only) — same logic as
                    # the candle bar_start guard above.
                    if _dws is not None and risk > 0:
                        _tws = float(getattr(_dws, "tick_window_start", {}).get(trade.symbol, 0))
                        if _tws >= _t_open and _tws > 0:
                            _th = float(getattr(_dws, "tick_highs", {}).get(trade.symbol, 0) or 0)
                            _tl = float(getattr(_dws, "tick_lows", {}).get(trade.symbol, 0) or 0)
                            if side == "long" and _th > entry:
                                _tick_r = (_th - entry) / risk
                                if _tick_r > trade.peak_mfe_r:
                                    trade.peak_mfe_r = _tick_r
                            elif side != "long" and _tl > 0 and _tl < entry:
                                _tick_r = (entry - _tl) / risk
                                if _tick_r > trade.peak_mfe_r:
                                    trade.peak_mfe_r = _tick_r
                except Exception:
                    pass  # no candle source yet → fall back to spot

                # Trade age
                age_sec = time.time() - trade.opened_at if trade.opened_at > 0 else 0

                # ── EXIT CHECKS (Phase 5.8 — unified dead-signal guard) ─

                # 0. UNIFIED DEAD-SIGNAL GUARD (Phase 5.8, 2026-04-25).
                # Replaces the four-stage cascade quick_kill / no_proof_of_life
                # / early_kill / zombie_kill with a single fee-floor + ATR-grace
                # + patience function. Pre-5.8 the four cuts collectively fired
                # before round-trip taker fees (~10bp ~ 0.15R on 0.65% SL) had
                # any chance to be recovered → 12% shadow WR / -$12.27 net /
                # 64% of losses were pure fees over 24h. The new guard never
                # decapitates a trade inside its grace window and only kills
                # past patience if peak<fee_floor AND current<-0.10R, OR if
                # the trade has stalled for 15+ minutes with peak<0.20R.
                # See docs/EXIT_GUARD_REFACTOR_5_8.md for design + math.
                # Phase 2 — config-aware dead-kill bypass.
                # If this is a phase2 virtual trade AND the config says
                # dead_kill_R is None (e.g. v2/v3/v4), SKIP the unified guard.
                _ph2_cfg = getattr(trade, "_exit_config", None)
                _ph2_skip_kill = _ph2_cfg is not None and _ph2_cfg.get("dead_kill_R") is None

                if not _ph2_skip_kill:
                    _kill_reason = should_kill_dead_signal(
                        age_sec=age_sec,
                        current_r=current_r,
                        peak_mfe_r=trade.peak_mfe_r,
                        grade=trade.grade,
                        entry=trade.entry_price,
                        sl=trade.stop_loss,
                        trade_type=trade.trade_type,
                        regime=trade.regime,
                        # FIX 1+2 (2026-04-26): per-user A/B for relaxed shadow exits
                        relaxed_shadow=getattr(self, "_relaxed_shadow_exits", False),
                    )
                    if _kill_reason is not None:
                        await self._close_trade(trade, price, _kill_reason)
                        break

                # Phase 2 — TP target by R-multiple (only fires if config has tp_R)
                if _ph2_cfg is not None and _ph2_cfg.get("tp_R") is not None:
                    if trade.peak_mfe_r >= _ph2_cfg["tp_R"]:
                        await self._close_trade(trade, price, f"phase2_tp_hit_{_ph2_cfg['tp_R']}R")
                        break

                # 1. SL hit. Label as trail_profit when SL is above (long) or
                #    below (short) entry — that means BE/trail has moved it
                #    into profit territory (paper labels this trail_profit).
                sl_hit = (side == "long" and price <= sl and sl > 0) or \
                         (side != "long" and price >= sl and sl > 0)
                if sl_hit:
                    in_profit = (
                        (side == "long" and sl > entry) or
                        (side != "long" and sl < entry)
                    )
                    reason = "trail_profit" if in_profit else "sl_hit"
                    await self._close_trade(trade, price, reason)
                    break

                # 3. Dead market (Phase 4.1 — paper signal_tracker:1760-1765).
                #    Age ≥ 180s, in quiet/mean-reversion regime, never made
                #    0.08R of favor, and currently underwater ≥0.10R. Paper
                #    proved this catches true duds earlier than early_kill
                #    (observed 2026-04-22: paper exited SOL long @ -0.11R
                #    dead_market while our demo held → bigger loss).
                _rg = (trade.regime or "").lower()
                if age_sec >= 180 and \
                   _rg in ("quiet", "low_liquidity", "mean_reversion", "") and \
                   trade.peak_mfe_r < 0.08 and current_r < -0.10:
                    await self._close_trade(trade, price, "dead_market")
                    break

                # 3b. Phase 5.8 (2026-04-25) — zombie_kill removed; replaced
                # by stalled_after_15min returned by should_kill_dead_signal()
                # at line 0 above. The new threshold (900s, peak<0.20R,
                # current<-0.05R) is wider than the prior zombie_kill (600s,
                # peak<0.10R) but pairs with the new grace window so duds
                # get equal protection without decapitating fee-floor losers.

                # 4. No-momentum — Phase 5.3.3 (2026-04-23) — REGIME-AWARE.
                #    Phase 4.1 baseline: RUNNER at 10min with peak<0.20R = dead.
                #    Apr 23 data: 2 BTC RUNNER shorts exited no_momentum at
                #    peak 0.16R (just shy of 0.20R) after 10min → paper's
                #    same signals caught the rollover at 12-15min. Same
                #    philosophy as 5.3.1 proof-of-life: in trending/breakout/
                #    high_vol regimes the chop-to-drop cycle runs longer,
                #    so give RUNNERs more room (900s at 0.15R instead of
                #    600s at 0.20R). Quiet/sideways stays tight to avoid
                #    bleeding on true duds.
                _rg_nm = (trade.regime or "").lower()
                if _rg_nm in ("trending", "trending_up", "trending_down", "breakout", "high_volatility"):
                    _nm_sec, _nm_mfe = 900, 0.15
                else:
                    _nm_sec, _nm_mfe = 600, 0.20
                if (trade.trade_type or "").upper() == "RUNNER" and \
                   age_sec >= _nm_sec and trade.peak_mfe_r < _nm_mfe:
                    await self._close_trade(trade, price, "no_momentum")
                    break

                # 5. MFE pullback close (paper signal_tracker:2383).
                #    After reaching ≥0.30R favor, if current R drops below
                #    40% of peak, lock in the remaining profit. Captures
                #    trail profit that would otherwise bleed back to BE.
                if trade.peak_mfe_r >= 0.30 and current_r <= trade.peak_mfe_r * 0.40:
                    await self._close_trade(trade, price, "mfe_pullback")
                    break

                # 5b. EXHAUSTION exits — Phase 5.2 (2026-04-23).
                # Ported from paper signal_tracker:1776-1806. Paper data
                # last 48h: 7 winners exited via exhaustion (avg +$10.80,
                # peak 0.79R). Demo had ZERO of these because the exit
                # reason wasn't wired → trail_profit instead caps at 40%
                # pullback from peak (loses the wick spike).
                # Gate at peak_mfe_r >= 0.30 (paper uses current_r > 0.5
                # but our scale is smaller — 0.30R captures the same
                # signal at our notional size).
                # Two patterns:
                #   (a) WICK: opposite-side wick > 60% of body (reversal)
                #   (b) SHRINK: 3 consecutive bodies shrinking (momentum dying)
                # Both require fully-formed bars (REST cache only — WS
                # candles_1m has the LIVE-forming bar which by definition
                # has no "previous" to compare).
                if age_sec >= 60 and trade.peak_mfe_r >= 0.30 and current_r > 0:
                    try:
                        _tracker_x = getattr(self._price_feed, "_signal_tracker", None)
                        _cache_x = getattr(_tracker_x, "_recent_candles", None) if _tracker_x else None
                        _cdf_x = _cache_x.get(trade.symbol) if _cache_x is not None else None
                        if _cdf_x is not None and len(_cdf_x) >= 3:
                            _last_x = _cdf_x.iloc[-1]
                            _last3 = _cdf_x.iloc[-3:]
                            _body = abs(float(_last_x["close"]) - float(_last_x["open"]))
                            _range = float(_last_x["high"]) - float(_last_x["low"])
                            # (a) wick check
                            if _range > 0 and _body > 0:
                                if side == "long":
                                    _upper_wick = float(_last_x["high"]) - max(
                                        float(_last_x["close"]), float(_last_x["open"])
                                    )
                                    if _upper_wick > _body * 0.6:
                                        await self._close_trade(trade, price, "exhaustion_wick")
                                        break
                                else:
                                    _lower_wick = min(
                                        float(_last_x["close"]), float(_last_x["open"])
                                    ) - float(_last_x["low"])
                                    if _lower_wick > _body * 0.6:
                                        await self._close_trade(trade, price, "exhaustion_wick")
                                        break
                            # (b) shrink check — 3 consecutive shrinking bodies
                            _bodies = [
                                abs(float(r["close"]) - float(r["open"]))
                                for _, r in _last3.iterrows()
                            ]
                            if len(_bodies) >= 3 and _bodies[0] > _bodies[1] > _bodies[2]:
                                await self._close_trade(trade, price, "exhaustion_shrink")
                                break
                    except Exception:
                        pass  # missing candles → no exhaustion check, fall through

                # 6. Time decay
                # 2026-04-27 — SCALP max_age tightened 1800s → 600s based on
                # counterfactual_exit_sweep.py findings on 39 trades:
                #   max_age 30min (was): -$10.07 net, baseline
                #   max_age 15min:       -$9.61 net (Δ +$0.46)
                #   max_age 10min:       -$7.81 net (Δ +$2.26) ← chosen
                #   max_age  5min:       -$5.10 net (Δ +$4.97) — best but riskier
                # Strategy ceiling per peak_mfe_r distribution: ~0.5R; no 1R+
                # peaks observed in 24h. So holding past ~10min mostly accumulates
                # losses via dead_signal_unified + stalled_after_15min.
                # Chose 600s (10min) as middle ground: most of the gain (+$2.26)
                # while preserving winners that peak at 5-10min (12 trades in
                # 24h peaked at 0.3-0.5R — keeping room for those).
                # See storage/exit_sweep/sweep_*.md for full data.
                # Phase 2 — config-aware max_age override
                _ph2_cfg_t = getattr(trade, "_exit_config", None)
                if _ph2_cfg_t is not None and _ph2_cfg_t.get("max_age_sec"):
                    max_age = int(_ph2_cfg_t["max_age_sec"])
                else:
                    # 2026-04-27 fix — earlier today saw 6 trades stuck at
                    # 60min hitting Agent 9-A force-close. Root cause: those
                    # were INTRADAY/RUNNER trade_type which had max_age=3600s
                    # (1h). For SHADOW trades specifically, tighten ALL
                    # trade_types to the same 600s cap as SCALP — strategy
                    # ceiling per peak_mfe_r distribution is ~0.5R regardless
                    # of trade_type label, so longer holds just accumulate
                    # losses via dead_signal_unified. Live trades unchanged.
                    if getattr(trade, "_is_shadow", False):
                        max_age = 600  # all shadow trade_types
                    else:
                        max_age = 600 if (trade.trade_type or "").upper() == "SCALP" else 3600
                if age_sec > max_age:
                    await self._close_trade(trade, price, f"time_decay_{int(age_sec/60)}m")
                    break

                # 5. Trail — MFE-based breakeven + lock tiers + chandelier
                #    (paper signal_tracker:1306-1327 + 2316-2325).
                _sl_before_trail = trade.stop_loss  # 5.3.4-DIAG: lifecycle snapshot
                sl_changed = False

                # Phase 5.3.5 (2026-04-23) — INVALID-STOP GUARD.
                # Root cause diagnosed via 5.3.4-DIAG: when BE+fee_buffer
                # or lock_pct tries to move SL past current market price,
                # the re-placed server stop is invalid (BUY stop below
                # market for short, or SELL stop above market for long)
                # and Delta rejects it with:
                #   error.code="immediate_execution_stop_order"
                # After rejection our server_stop_id is None → local
                # monitor then sees market >= local sl → forced close
                # at market = premature exit at fees-eaten NET.
                # Guard: require new SL to stay on the protective side
                # of current market by at least 2 ticks. Rejects any
                # trail move that would place an invalid stop; keeps
                # previous SL (which is by definition still valid).
                _tick = trade.tick_size or 0.01
                # Phase 5.3.7 (2026-04-23) — WIDER SAFETY BUFFER + RETRY.
                # 5.3.5's 2-tick buffer was too narrow for fast-moving tape
                # (e.g. SOL peak 1.14R exit showed Delta rejecting stops
                # that the local guard had just validated because market
                # had moved 100-200ms later during the API round-trip).
                # Wider guard: max(2 ticks, 0.05% of price) — accounts for
                # typical intra-API-call price movement without being
                # excessively conservative on slow tape.
                _sl_safety = max(_tick * 2, price * 0.0005)

                def _sl_stays_valid(proposed_sl: float) -> bool:
                    if side == "long":
                        # Sell stop must be BELOW market (+ buffer)
                        return proposed_sl < price - _sl_safety
                    else:
                        # Buy stop must be ABOVE market (+ buffer)
                        return proposed_sl > price + _sl_safety

                # Phase 5.3.6 (2026-04-23) — RAISE BE LOCK THRESHOLD 0.20R → 0.30R.
                # Previous threshold activated lock at 0.20R × 60% = 0.12R lock,
                # which produced ~$0.35 gross on small notional ($400) — less
                # than the $0.49 round-trip fee wall. Result: every peak-0.20R
                # trade force-closed fee-negative ("scraped win or small loss").
                # Phase 5.3.5 validated the SL trail mechanics work; this
                # raises the fee-wall clearance threshold so locks only activate
                # when peak is deep enough that the locked profit covers fees.
                # Trades peaking 0.20-0.29R now run to original SL or time_decay —
                # accepting slightly more -1R losses in exchange for fewer
                # fee-drag-negative closes. Expected improvement: avg winner
                # 3-5× larger, WR drops -5 to -10pp, NET/trade flips positive.
                # Phase 5.5-N3 (2026-04-24) — DYNAMIC BE-LOCK FLOOR.
                # 5.3.6 used static 0.30R threshold for ALL trades. But the real
                # break-even threshold depends on the FEE WALL relative to the
                # trade's R-distance. Tight SL (0.3% wide) → fees are bigger
                # fraction of R → need deeper peak to clear. Wide SL (1.5%) →
                # fees are smaller fraction → can lock at smaller peak.
                # Formula: fee_wall_r = (2 * 0.059%) * entry_price / initial_risk
                # Then: min_lock_r = max(0.30, fee_wall_r * 1.30)  (30% cushion)
                # On SOL @ $86, SL 0.79 → fee_wall_r = 0.118% × 86 / 0.79 = 0.128R
                #   → min_lock_r = max(0.30, 0.166) = 0.30R (no change)
                # On BTC @ $78k, SL 500 → fee_wall_r = 0.118% × 78000 / 500 = 0.184R
                #   → min_lock_r = max(0.30, 0.239) = 0.30R (no change)
                # On ETH @ $2330, SL 15 → fee_wall_r = 0.118% × 2330 / 15 = 0.183R
                #   → min_lock_r = max(0.30, 0.238) = 0.30R (no change)
                # On a TIGHT-SL trade (SOL 0.30 risk) → fee_wall_r = 0.34R
                #   → min_lock_r = max(0.30, 0.44) = 0.44R (TIGHTER → wait for deeper peak)
                # Self-tunes per trade without changing typical case.
                # Stage 1 (2026-04-26): shadow simulation fidelity — match
                # paper's gates by removing Delta-API safety constraints when
                # running on shadow trades for users in the relaxed-sim A/B.
                # Live/admin/control trades take the EXISTING guarded path.
                _is_relaxed_sim = (
                    getattr(self, "_relaxed_shadow_simulation", False)
                    and getattr(trade, "_is_shadow", False)
                )

                if _is_relaxed_sim:
                    # Shadow path: paper-aligned static thresholds, no API guards
                    _min_lock_r = 0.30   # was: max(0.30, fee_wall_r * 1.30)
                    _age_gate = 0        # was: 15 (Delta API anti-race)
                    def _sl_check(proposed_sl):  # was: _sl_stays_valid (5 bps from market)
                        return True
                else:
                    # Live/standard path: KEEP all existing guards (correct for live)
                    _fee_wall_r = (2 * 0.00059 * entry) / risk if risk > 0 else 0.30
                    _min_lock_r = max(0.30, _fee_wall_r * 1.30)
                    _age_gate = 15
                    _sl_check = _sl_stays_valid

                # Track for diagnostic
                if not hasattr(trade, "_min_lock_r_used"):
                    trade._min_lock_r_used = _min_lock_r

                if trade.peak_mfe_r >= _min_lock_r and age_sec > _age_gate:
                    fee_buffer = entry * 0.004  # 0.4% cushion covers 2×0.05% fees + slippage
                    if side == "long":
                        be_sl = entry + fee_buffer
                        if be_sl > trade.stop_loss and _sl_check(be_sl):
                            trade.stop_loss = be_sl
                            sl_changed = True
                            trade._breakeven_set = True   # Stage 2: paper-style flag
                    else:
                        be_sl = entry - fee_buffer
                        if be_sl < trade.stop_loss and _sl_check(be_sl):
                            trade.stop_loss = be_sl
                            sl_changed = True
                            trade._breakeven_set = True   # Stage 2: paper-style flag

                    # Primary lock_pct (paper tiers — note non-monotonic
                    # 0.3R=0.75 is intentional: aggressive early lock then
                    # chandelier takes over at 0.4R+).
                    # Phase 5.3.6 removed 0.2R tier (0.60) — now dead code
                    # since outer gate starts at 0.30R.
                    lock_pct = 0.0
                    if   trade.peak_mfe_r >= 1.0: lock_pct = 0.55
                    elif trade.peak_mfe_r >= 0.7: lock_pct = 0.50
                    elif trade.peak_mfe_r >= 0.5: lock_pct = 0.45
                    elif trade.peak_mfe_r >= 0.4: lock_pct = 0.40
                    elif trade.peak_mfe_r >= 0.3: lock_pct = 0.75

                    if lock_pct > 0:
                        lock_dist = risk * trade.peak_mfe_r * lock_pct
                        new_sl = entry + lock_dist if side == "long" else entry - lock_dist
                        if ((side == "long" and new_sl > trade.stop_loss) or \
                            (side != "long" and new_sl < trade.stop_loss)) and \
                           _sl_check(new_sl):
                            trade.stop_loss = new_sl
                            sl_changed = True

                    # Chandelier trail at 0.4R+ — tighter lock on bigger
                    # moves so retracements bank profit instead of bleeding
                    # back to lock_pct level (paper line 2316).
                    if trade.peak_mfe_r >= 0.4:
                        if   trade.peak_mfe_r >= 1.0: ch_lock = 0.85
                        elif trade.peak_mfe_r >= 0.7: ch_lock = 0.80
                        elif trade.peak_mfe_r >= 0.5: ch_lock = 0.70
                        else:                          ch_lock = 0.60
                        ch_dist = risk * trade.peak_mfe_r * ch_lock
                        ch_sl = entry + ch_dist if side == "long" else entry - ch_dist
                        if ((side == "long" and ch_sl > trade.stop_loss) or \
                            (side != "long" and ch_sl < trade.stop_loss)) and \
                           _sl_check(ch_sl):
                            trade.stop_loss = ch_sl
                            sl_changed = True

                # 6. If SL moved, update server-side stop order so the
                #    safety net tracks the trail (otherwise it stays at
                #    initial SL and the monitor's tighter SL is the only
                #    protection if we die between ticks).
                # Phase 5.6-F (2026-04-24) — SL REVERT COOLDOWN.
                # Observed Apr 24 10:33-10:40: niranjan SOL short peak 1.05R
                # — trail fired 965 create attempts in 7 min, EVERY one
                # rejected with immediate_execution_stop_order. Trade exited
                # via exhaustion_wick at -$0.517 instead of locking +$0.90.
                # Without cooldown, we hammer Delta 2×/sec forever on any
                # trade where market is stuck in a range that rejects every
                # proposed SL. Cooldown: after a SL_REVERT, suppress further
                # update attempts on this trade for 30s. OLD server stop
                # still protects (never cancelled). Resumed after cooldown
                # to catch later favorable moves.
                if sl_changed and trade.server_stop_id:
                    _revert_cooldown_s = 30.0
                    _last_revert_ts = getattr(trade, "_last_sl_revert_ts", 0.0)
                    if _last_revert_ts > 0:
                        _since_revert = time.monotonic() - _last_revert_ts
                        if _since_revert < _revert_cooldown_s:
                            # Cooldown active — skip update, restore local SL
                            # to match what server still holds. No log (would
                            # spam). Diagnostic counter.
                            trade.stop_loss = _sl_before_trail
                            sl_changed = False
                            if not hasattr(trade, "_sl_cooldown_skips"):
                                trade._sl_cooldown_skips = 0
                            trade._sl_cooldown_skips += 1

                if sl_changed and trade.server_stop_id:
                    # Phase 5.9-B (2026-04-24) — TRY EDIT-IN-PLACE FIRST.
                    # Delta supports PUT /v2/orders which modifies stop_price
                    # atomically on the existing server stop. Benefits:
                    #   - no cancel → no race window where position is naked
                    #   - half the API weight (5 vs 10 per trail update)
                    #   - simpler semantics: either succeed or fallback
                    # If edit fails (order was filled, cancelled, or rejected
                    # by immediate_execution), fall through to the legacy
                    # create-new + cancel-old flow (Phase 5.3.8 + 5.9-A retry).
                    _edit_ok = False
                    _tick_for_edit = trade.tick_size or 0.01
                    _rounded_sl = round(round(trade.stop_loss / _tick_for_edit) * _tick_for_edit, 10)
                    _new_sl_str = f"{_rounded_sl:.10f}".rstrip("0").rstrip(".")
                    try:
                        _edit_resp = await asyncio.to_thread(
                            self._delta._client.edit_order,
                            order_id=trade.server_stop_id,
                            product_id=trade.product_id,
                            stop_price=_rounded_sl,
                        )
                        _edit_err = _edit_resp.get("error") if isinstance(_edit_resp, dict) else "not_dict"
                        if _edit_resp and not _edit_err:
                            _edit_ok = True
                    except Exception as _edit_exc:
                        logger.debug("SL_EDIT_EXC user=%s %s: %s",
                                     self.user_id[:8], trade.symbol, _edit_exc)

                    if _edit_ok:
                        # Diagnostic counter
                        if not hasattr(trade, "_sl_edit_success_count"):
                            trade._sl_edit_success_count = 0
                        trade._sl_edit_success_count += 1
                        logger.info(
                            "SL_EDIT_OK user=%s %s | new_sl=%s order_id=%s peak_r=%.3f",
                            self.user_id[:8], trade.symbol, _new_sl_str,
                            trade.server_stop_id, trade.peak_mfe_r,
                        )
                        # Skip the legacy create+cancel block below.
                        sl_changed = False  # prevent re-entry
                        # Early exit via dummy guard: wrap legacy block in elif-false
                        # so we don't need another indentation level.

                # Phase 5.9-B fallback: legacy create-new + cancel-old flow.
                # Only runs if edit failed OR initial stop didn't exist OR
                # sl_changed already cleared (edit succeeded).
                if sl_changed and trade.server_stop_id:
                    # Phase 5.3.8 (2026-04-23) — CREATE-FIRST, CANCEL-SECOND.
                    # 5.3.7 cancelled old stop BEFORE trying to create new one.
                    # When Delta's fast-tape race rejected BOTH the primary
                    # and retry create calls, position was left completely
                    # unprotected on Delta — local monitor then fired on the
                    # stale in-memory SL with 100-500ms lag → slippage
                    # catastrophe (observed 17:10 SOL: intended lock 85.34,
                    # actual exit 85.58 = -$1.09 combined, should have been
                    # +$1.03).
                    # New flow:
                    #   1. SAVE prev state (SL + server_stop_id)
                    #   2. Try CREATE new (with retry fallback)
                    #   3. If new succeeds → cancel old (both reduce_only, safe overlap)
                    #   4. If new fails → REVERT local SL, keep old server stop alive
                    # Worst case under 5.3.8: trade continues with PREVIOUS
                    # (still-valid) protective stop — trail fails silently,
                    # retried on next tick with possibly-different market.
                    _better = (trade.stop_loss > _sl_before_trail) if side == "long" else (trade.stop_loss < _sl_before_trail)
                    _prev_server_stop_id = trade.server_stop_id
                    logger.warning(
                        "SL_LIFECYCLE user=%s %s %s | prev_sl=%.5f new_sl=%.5f market=%.5f better=%s peak_r=%.3f old_stop_id=%s",
                        self.user_id[:8], trade.symbol, side, _sl_before_trail,
                        trade.stop_loss, price, _better, trade.peak_mfe_r, trade.server_stop_id,
                    )

                    def _tick_str(px):
                        t = trade.tick_size or 0.01
                        return f"{round(round(px/t)*t, 10):.10f}".rstrip("0").rstrip(".")

                    async def _try_create(stop_px: float, tag: str):
                        # Phase 5.9-A: mark_price trigger (kills 80%+ of
                        # immediate_execution rejections) + idempotent COID.
                        _coid_st = _new_coid("vn_st")  # SL trail — UUID-based
                        try:
                            resp = await asyncio.to_thread(
                                self._delta._client.create_order,
                                {
                                    "product_id": trade.product_id,
                                    "size": trade.position_size,
                                    "side": "sell" if side == "long" else "buy",
                                    "order_type": "market_order",
                                    "stop_order_type": "stop_loss_order",
                                    "stop_price": _tick_str(stop_px),
                                    "stop_trigger_method": "mark_price",
                                    "reduce_only": "true",
                                    "client_order_id": _coid_st,
                                },
                            )
                            resp = resp if isinstance(resp, dict) else {}
                            _sid = resp.get("id")
                            _st = resp.get("state", "?")
                            logger.warning(
                                "SL_CREATE%s user=%s %s | stop_price=%.5f market=%.5f new_id=%s state=%s",
                                tag, self.user_id[:8], trade.symbol, stop_px, price, _sid, _st,
                            )
                            return int(_sid) if _sid else None
                        except Exception as _exc:
                            logger.warning(
                                "SL_CREATE%s_FAIL user=%s %s stop=%.5f market=%.5f: %s",
                                tag, self.user_id[:8], trade.symbol, stop_px, price, _exc,
                            )
                            return None

                    # Step 1: Try to place NEW stop with iterative retry.
                    # Phase 5.5-N2 (2026-04-24) — ITERATIVE RETRY WITH FRESH MARKET.
                    # 5.3.7's single retry used the SAME stale cached market that
                    # caused the primary failure → still failed on fast-tape races
                    # (observed SOL peak 1.14R: SL_CREATE + SL_CREATE_RETRY both failed,
                    # trade silent-failed via 5.3.8 SL_REVERT, missed +$1.54 profit).
                    # Fix: 3 retry attempts with FRESHLY-READ market each time + widening buffer.
                    # Each attempt re-reads price from delta_ws, lets market settle 50ms
                    # between attempts, and widens the safety buffer (0.05% → 0.10% → 0.15%).
                    # This catches even the fastest-moving tape; total worst-case ~200ms
                    # of additional latency vs the old single-retry path.
                    _retries_used = 0
                    _new_sid = await _try_create(trade.stop_loss, "")
                    if not _new_sid:
                        _dws_n2 = getattr(self._price_feed, "_delta_ws", None)
                        for _attempt in range(3):
                            _retries_used = _attempt + 1
                            # Phase 5.6-E (2026-04-24) — use BID/ASK not last-trade.
                            # Delta evaluates stops against current top-of-book, not
                            # last-trade price. _dws.prices updates only on ticker
                            # messages (~1/sec) while bid/ask update on every tick
                            # message. Observed Apr 24 09:52 SOL peak 0.77R retry
                            # loop: prices cache stayed at 85.183 across 15+ retries
                            # while actual market was 85.22-85.35. Using the relevant
                            # side of book gives fresh data:
                            #   LONG  sell-stop: must be BELOW market → use BID
                            #   SHORT buy-stop:  must be ABOVE market → use ASK
                            # Phase 5.6-F (2026-04-24) — read BOTH sides for spread.
                            _fresh_market = price  # fallback
                            _spread_px = 0.0
                            if _dws_n2 is not None:
                                _bid_px = float(getattr(_dws_n2, "bids", {}).get(trade.symbol, 0) or 0)
                                _ask_px = float(getattr(_dws_n2, "asks", {}).get(trade.symbol, 0) or 0)
                                if _bid_px > 0 and _ask_px > 0 and _ask_px > _bid_px:
                                    _spread_px = _ask_px - _bid_px
                                if side == "long":
                                    _fp = _bid_px
                                else:
                                    _fp = _ask_px
                                if _fp > 0:
                                    _fresh_market = _fp
                                # Fallback to last-trade prices if bid/ask empty
                                elif float(getattr(_dws_n2, "prices", {}).get(trade.symbol, 0) or 0) > 0:
                                    _fresh_market = float(_dws_n2.prices[trade.symbol])
                            # Phase 5.6-F buffer: SPREAD-AWARE. Observed 10:33-10:40
                            # niranjan SOL: 0.05%→0.15% buffer couldn't clear
                            # immediate_execution rejection across 965 attempts over
                            # 7 min (testnet spread 10-25bp + ask drift during
                            # API round-trip > our max 15bp cushion).
                            # New: base = max(2× spread_bp, 20bp); scale 1×/2×/3×.
                            # Caps at 60bp minimum, or 3× spread if wider. On SOL
                            # $86 with 10bp spread: buf 20/40/60bp = $0.17/$0.34/$0.52
                            # which clears testnet ask drift comfortably.
                            if _fresh_market > 0 and _spread_px > 0:
                                _spread_bp = (_spread_px / _fresh_market) * 10000.0
                            else:
                                _spread_bp = 0.0
                            _base_bp = max(20.0, 2.0 * _spread_bp)
                            _buf_pct = (_base_bp * (_attempt + 1)) / 10000.0
                            _retry_buf = max(_tick * 5, _fresh_market * _buf_pct)
                            if side == "long":
                                _adj_sl = _fresh_market - _retry_buf
                            else:
                                _adj_sl = _fresh_market + _retry_buf
                            _adj_sl = round(round(_adj_sl / _tick) * _tick, 10)
                            _retry_sid = await _try_create(_adj_sl, f"_RETRY_{_attempt+1}")
                            if _retry_sid:
                                trade.stop_loss = _adj_sl
                                _new_sid = _retry_sid
                                break
                            await asyncio.sleep(0.05)  # 50ms settle between attempts
                    # Diagnostic marker for multi-diagnosis (Phase 5.5-DIAG)
                    if _retries_used > 0:
                        if not hasattr(trade, "_sl_retry_attempts_total"):
                            trade._sl_retry_attempts_total = 0
                        trade._sl_retry_attempts_total += _retries_used

                    # Step 2: Handle the outcome
                    if _new_sid:
                        # New stop placed → cancel OLD (both are reduce_only,
                        # safe during the ~50-300ms overlap; NEW is tighter for
                        # shorts so triggers first if both would fire).
                        try:
                            await asyncio.to_thread(
                                self._delta._client.cancel_order,
                                product_id=trade.product_id,
                                order_id=_prev_server_stop_id,
                            )
                            logger.warning("SL_CANCEL_OK user=%s %s", self.user_id[:8], trade.symbol)
                        except Exception as _cxe:
                            # Old stop may have been filled during overlap (NEW triggered first)
                            # or otherwise unavailable — not fatal, position is closed or has
                            # NEW stop protecting it. Log for visibility.
                            logger.warning(
                                "SL_CANCEL_FAIL user=%s %s old_id=%s: %s",
                                self.user_id[:8], trade.symbol, _prev_server_stop_id, _cxe,
                            )
                        trade.server_stop_id = int(_new_sid)
                    else:
                        # Both create attempts failed → REVERT local SL to
                        # previous value so local sl_hit detection doesn't
                        # fire on the Delta-rejected level. OLD server stop
                        # is still alive on Delta (we never cancelled it).
                        trade.stop_loss = _sl_before_trail
                        # Phase 5.6-F: record revert timestamp + counter for
                        # cooldown + diagnostic. Next trail attempt within 30s
                        # will be skipped (see top of server-stop block).
                        trade._last_sl_revert_ts = time.monotonic()
                        if not hasattr(trade, "_sl_revert_count"):
                            trade._sl_revert_count = 0
                        trade._sl_revert_count += 1
                        logger.warning(
                            "SL_REVERT user=%s %s — both creates failed, restored prev_sl=%.5f old_id=%s revert_n=%d",
                            self.user_id[:8], trade.symbol, _sl_before_trail, _prev_server_stop_id, trade._sl_revert_count,
                        )

                await asyncio.sleep(0.5)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("USER %s: monitor error for %s: %s", self.user_id[:8], trade_id, e)

    async def _close_shadow(self, trade: UserTradeRecord, exit_price: float, reason: str):
        """T4.3 Shadow-live close — NO Delta calls.

        Simulates taker exit fill from current L2 top-of-book:
          - LONG close (sell) hits best_bid
          - SHORT close (buy) hits best_ask
        Computes gross/fees/funding/NET exactly like real close path,
        applies post-hoc label correction, persists to DB as shadow trade.
        """
        try:
            _dws = getattr(self._price_feed, "_delta_ws", None)
            book = _dws.l2_orderbook.get(trade.symbol) if _dws else None

            # Stage 2 (2026-04-26): if relaxed_shadow_simulation is on AND the
            # trade has a paper-style breakeven_set flag (locked SL into profit
            # zone) AND reason is trail-related AND SL is in profit zone:
            # exit at locked SL price (matches what a server-side stop on
            # Delta/Bybit would actually fill at, ±1 tick). The L2-worst-case
            # fallback over-states slippage on locked stops by 3-5 bps,
            # masking the bot's true edge in shadow data.
            _is_relaxed_sim = (
                getattr(self, "_relaxed_shadow_simulation", False)
                and getattr(trade, "_breakeven_set", False)
            )
            _trail_close = reason in ("trail_profit", "sl_hit")
            _sl_in_profit = (
                (trade.side == "long"  and trade.stop_loss > trade.entry_price) or
                (trade.side == "short" and trade.stop_loss < trade.entry_price)
            )

            if _is_relaxed_sim and _trail_close and _sl_in_profit:
                # Honor the locked SL — server-side stop fills at trigger price
                actual_exit = float(trade.stop_loss)
                logger.info(
                    "SHADOW_LOCKED_SL_EXIT: %s %s @ %.5f (locked SL, was L2 worst-case)",
                    self.user_email, trade.symbol, actual_exit,
                )
            elif book and book.get("bids") and book.get("asks"):
                # Standard path: L2 top-of-book worst-case (taker fill simulation)
                if trade.side == "long":
                    actual_exit = float(book["bids"][0][0])  # sell hits bid
                else:
                    actual_exit = float(book["asks"][0][0])  # buy hits ask
            else:
                # No L2 cached → fall back to monitor's price argument
                actual_exit = float(exit_price)

            _cs = float(trade.contract_size or 1.0)
            if trade.side == "long":
                gross_pnl = (actual_exit - trade.entry_price) * trade.position_size * _cs
            else:
                gross_pnl = (trade.entry_price - actual_exit) * trade.position_size * _cs

            # Taker exit fee
            exit_fee_usd = actual_exit * trade.position_size * _cs * 0.00059
            total_fees = float(trade.entry_fee_usd or 0) + exit_fee_usd

            # 2026-04-27 Path A — MAKER COUNTERFACTUAL.
            # Shadow always simulates as taker (by design: no L2 walk to
            # determine if a post_only would have rested + filled). But the
            # operator needs to know: "if patient maker mode WERE working,
            # what would PnL look like?" — to validate that the maker fix
            # is worth the engineering effort + to calibrate Phase 2
            # leaderboard's predictive power for live execution.
            #
            # Computation: Delta India fee schedule
            #   taker = 0.059% per side (used above)
            #   maker = 0.024% per side (Delta INR maker rate)
            # Counterfactual savings PER trade if BOTH sides filled as
            # maker = (taker_fee - maker_fee) × 2 sides ≈ 0.07% of notional.
            # On $30 margin × 20× leverage = $600 notional → $0.42/trade.
            # Computed over a representative fill rate: 50% (patient
            # mode estimate). Realized only when Path B real pilot
            # measures the actual rate — then this calibrates.
            try:
                _maker_rate = 0.00024     # Delta India maker
                _taker_rate = 0.00059     # Delta India taker (used above)
                _entry_notional = trade.entry_price * trade.position_size * _cs
                _exit_notional  = actual_exit * trade.position_size * _cs
                # Per-side savings if filled as maker instead of taker
                _entry_save = _entry_notional * (_taker_rate - _maker_rate)
                _exit_save  = _exit_notional  * (_taker_rate - _maker_rate)
                # Counterfactual @ 100% maker fill (both sides)
                _cf_maker_savings_100pct = _entry_save + _exit_save
                # Counterfactual @ 50% blended fill rate (patient mode est)
                _cf_maker_savings_50pct  = _cf_maker_savings_100pct * 0.5
                # Annotate trade for the close meta block to persist.
                trade._cf_maker_savings_100pct = round(_cf_maker_savings_100pct, 4)
                trade._cf_maker_savings_50pct  = round(_cf_maker_savings_50pct,  4)
                trade._cf_net_at_50pct_maker   = round(net_pnl + _cf_maker_savings_50pct, 4) \
                                                 if 'net_pnl' in dir() else None
            except Exception:
                trade._cf_maker_savings_100pct = 0.0
                trade._cf_maker_savings_50pct  = 0.0

            # Funding cost (typically zero on testnet; will matter on real prod)
            funding_usd = 0.0
            try:
                _hours_held = max(0.0, (time.time() - trade.opened_at) / 3600.0)
                if _hours_held > 0 and trade.entry_price > 0:
                    _fr = float(trade.funding_rate_at_entry or 0)
                    if _fr != 0.0:
                        _notional = trade.entry_price * trade.position_size * _cs
                        _periods = _hours_held / 8.0
                        _raw = _notional * _fr * _periods
                        funding_usd = _raw if trade.side == "long" else -_raw
            except Exception:
                pass
            trade.funding_usd = round(funding_usd, 6)

            net_pnl = gross_pnl - total_fees - funding_usd

            # Post-hoc label correction (Phase 5.3.1)
            if reason in ("trail_profit", "sl_hit"):
                if net_pnl > 0 and trade.peak_mfe_r >= 0.50:
                    reason = "partial_win"
                elif net_pnl > 0:
                    reason = "trail_profit"
                else:
                    reason = "sl_hit"

            _notional_in = trade.entry_price * trade.position_size * _cs
            pnl_pct = (net_pnl / _notional_in) if _notional_in > 0 else 0

            logger.warning(
                "🌓 SHADOW EXIT: %s %s %s | entry=%.4f exit=%.4f | gross=$%.3f fees=$%.3f fund=$%.3f NET=$%.3f | %s | mfe=%.2fR",
                self.user_email, trade.symbol, trade.side,
                trade.entry_price, actual_exit,
                gross_pnl, total_fees, funding_usd, net_pnl,
                reason, trade.peak_mfe_r,
            )

            # Persist close to DB
            await self._record_trade_db(
                trade, status="closed", reason=reason,
                exit_price=actual_exit, pnl_usd=net_pnl,
                fees_usd=total_fees, gross_pnl=gross_pnl,
            )

            # Cleanup (same as real close)
            self.closed_trades.append({
                "trade_id": trade.trade_id,
                "user_id": trade.user_id,
                "symbol": trade.symbol,
                "side": trade.side,
                "entry_price": trade.entry_price,
                "exit_price": actual_exit,
                "pnl_usd": round(net_pnl, 4),
                "gross_pnl_usd": round(gross_pnl, 4),
                "fees_usd": round(total_fees, 4),
                "funding_usd": round(funding_usd, 6),
                "pnl_pct": round(pnl_pct * 100, 4),
                "exit_reason": reason,
                "peak_mfe_r": round(trade.peak_mfe_r, 4),
                "margin": trade.margin,
                "leverage": trade.leverage,
                "contract_size": _cs,
                "scanner": trade.scanner,
                "grade": trade.grade,
                "duration_sec": time.time() - trade.opened_at,
                "exec_mode": "shadow_taker",
                "is_shadow": True,
            })
            # Cap ring
            if len(self.closed_trades) > 100:
                self.closed_trades = self.closed_trades[-100:]

            # Remove from open
            if trade.trade_id in self.open_trades:
                del self.open_trades[trade.trade_id]

            # CB accounting — shadow losses still count (for realistic daily-loss modeling)
            self.cb.trade_count_today += 1
            if net_pnl < 0:
                self.cb.daily_loss_usd += abs(net_pnl)
                self.cb.consecutive_losses += 1
            else:
                self.cb.consecutive_losses = 0

        except Exception as e:
            logger.error("USER %s: shadow close error for %s: %s",
                         self.user_id[:8], trade.trade_id, e)

    # ══════════════════════════════════════════════════════════════
    # G3 (2026-04-26) — PUBLIC: close-position-at-market helper.
    # Used by orchestrator's kill_switch_close_open listener
    # (per docs/KILL_SWITCH_CLOSE_OPEN_LISTENER_v1.md).
    # ══════════════════════════════════════════════════════════════
    async def close_position_at_market(self, trade_id: str, reason: str = "force_flat") -> dict:
        """Force-close a SINGLE position at market for this user.

        Returns: {"ok": bool, "trade_id": str, "exit_price": float|None,
                  "pnl_usd": float|None, "error": str|None}
        Safe for both real and shadow trades.
        Idempotent — safe to call on already-closed trades (returns ok=True, error=None, exit_price=None).
        """
        out = {"ok": False, "trade_id": trade_id, "exit_price": None,
               "pnl_usd": None, "error": None}
        trade = self.open_trades.get(trade_id)
        if not trade:
            out["ok"] = True   # already-closed is success from caller's POV
            out["error"] = "not_in_open_trades"
            return out

        # 1. Determine current price
        cur_px = 0.0
        try:
            # Prefer WS top-of-book if available; else use entry as conservative fallback
            from execution.user_registry import get_price_for_symbol
            cur_px = float(get_price_for_symbol(trade.symbol) or 0)
        except Exception:
            pass
        if cur_px <= 0 and self._delta and hasattr(self._delta, "get_last_price"):
            try:
                cur_px = float(await asyncio.to_thread(self._delta.get_last_price, trade.symbol) or 0)
            except Exception:
                pass
        if cur_px <= 0:
            cur_px = float(trade.entry_price or 0)  # fallback (no PnL)

        # 2. For shadow trades: skip exchange, go straight to _close_trade
        if getattr(trade, "_is_shadow", False):
            try:
                await self._close_trade(trade, cur_px, reason)
                out["ok"] = True
                out["exit_price"] = cur_px
                return out
            except Exception as e:
                out["error"] = "shadow_close_error:" + str(e)[:120]
                return out

        # 3. For real trades: send market_order ioc reduce_only to Delta
        try:
            from exchange.delta_client import PRODUCT_MAP
            product_info = PRODUCT_MAP.get(trade.symbol) or {}
            is_demo = getattr(self._delta, "mode", "demo") == "demo"
            product_id = product_info.get("demo_id" if is_demo else "prod_id")
            if not product_id:
                out["error"] = f"no_product_id_for_{trade.symbol}"
                return out

            close_side = "sell" if str(trade.side).lower() == "long" else "buy"
            params = {
                "product_id": product_id,
                "size": int(trade.position_size),
                "side": close_side,
                "order_type": "market_order",
                "time_in_force": "ioc",
                "reduce_only": "true",
                "client_order_id": _new_coid("vn_kfc"),  # kill-flat-close
            }
            resp = await asyncio.to_thread(self._delta._client.create_order, params)
            resp = resp if isinstance(resp, dict) else {}
            fill = float(resp.get("average_fill_price", 0) or 0) or cur_px

            # Stamp + persist via _close_trade
            await self._close_trade(trade, fill, reason)
            out["ok"] = True
            out["exit_price"] = fill
            return out
        except Exception as e:
            out["error"] = "real_close_error:" + str(e)[:120]
            logger.error(
                "USER %s: close_position_at_market(%s) failed: %s",
                self.user_id[:8], trade_id, e, exc_info=True
            )
            return out

    async def _close_trade(self, trade: UserTradeRecord, exit_price: float, reason: str):
        """Close a user's real trade on their exchange."""
        try:
            # Phase 5.6-B / T4.3 (2026-04-24) — SHADOW-LIVE CLOSE BRANCH.
            # Shadow trades never placed real Delta orders at entry, so close
            # must simulate exit fill from L2 top-of-book (taker worst-case)
            # and skip all Delta cancel/close calls.
            if getattr(trade, "_is_shadow", False):
                return await self._close_shadow(trade, exit_price, reason)

            # Mode-aware product_id resolution (see execute_signal for rationale).
            from exchange.delta_client import PRODUCT_MAP
            product_info = PRODUCT_MAP.get(trade.symbol) or {}
            is_demo = getattr(self._delta, "mode", "demo") == "demo"
            product_id = product_info.get("demo_id" if is_demo else "prod_id")
            if not product_id:
                logger.error(
                    "USER %s: cannot close %s — no %s product_id",
                    self.user_id[:8], trade.symbol, "demo" if is_demo else "prod",
                )
                return
            close_side = "sell" if trade.side == "long" else "buy"

            # Phase 4.0 — Cancel the server-side safety-net stop FIRST so it
            # doesn't double-fire after our reduce_only market close.
            # Phase 4.4 — off event loop.
            if trade.server_stop_id:
                try:
                    await asyncio.to_thread(
                        self._delta._client.cancel_order,
                        product_id=product_id,
                        order_id=trade.server_stop_id,
                    )
                except Exception:
                    pass  # ok if already cancelled / filled
                trade.server_stop_id = None

            # Phase 5.0 (2026-04-22) — TIERED EXIT EXECUTION
            # Try a post_only limit at mid-price first (maker fee 0.02%),
            # wait up to 300ms, fall back to market (taker 0.05%) if unfilled.
            # Observed Phase 4.6 baseline: every reduce_only market close
            # slips 5-15bps off the trail SL level. Mid-limit recovers most
            # of that on ~30-50% of exits (when book is stable).
            actual_exit = float(exit_price)
            exit_fee_usd = 0.0
            exit_exec_mode = ""

            # Pull current best bid/ask from WS orderbook state (Phase 4.3 WS).
            # Phase 5.0.1 (2026-04-22) — aggressive-maker exit.
            # Previous mid-price limit fell through to taker on both 16:23
            # ETH shorts (observed 0/2 maker). Mid sits deep inside the
            # spread and rarely gets crossed within 300ms on small moves.
            # Aggressive maker: sit exactly 1 tick inside the top-of-book
            # on the ATTACKING side → becomes a resting maker at the
            # most-likely-to-fill price. Any market taker sweep touches
            # us first.
            _bid = 0.0
            _ask = 0.0
            try:
                _dws = getattr(self._price_feed, "_delta_ws", None)
                if _dws is not None:
                    _bid = float((_dws.bids or {}).get(trade.symbol, 0) or 0)
                    _ask = float((_dws.asks or {}).get(trade.symbol, 0) or 0)
            except Exception:
                pass
            _spread_ticks = 0
            _t = trade.tick_size or 0.01
            if _bid > 0 and _ask > 0 and _ask > _bid:
                _spread_ticks = round((_ask - _bid) / _t)

            # --- Attempt 1: post_only aggressive-maker limit ---
            _limit_oid = None
            # Need spread ≥ 2 ticks for aggressive-maker to be valid
            # (otherwise 1-tick-inside would cross the book).
            if _bid > 0 and _ask > 0 and _spread_ticks >= 2:
                try:
                    if close_side == "sell":
                        # Sell limit just above best_bid = top of ask book
                        _limit = _bid + _t
                    else:
                        # Buy limit just below best_ask = top of bid book
                        _limit = _ask - _t
                    _limit = round(round(_limit / _t) * _t, 10)
                    _lim_str = f"{_limit:.10f}".rstrip("0").rstrip(".")
                    # Phase 5.9-A: client_order_id for idempotent close.
                    _coid_mc = _new_coid("vn_mc")
                    limit_close_resp = await asyncio.to_thread(
                        self._delta._client.create_order,
                        {
                            "product_id": product_id,
                            "size": trade.position_size,
                            "side": close_side,
                            "order_type": "limit_order",
                            "limit_price": _lim_str,
                            "time_in_force": "gtc",
                            "post_only": "true",
                            "reduce_only": "true",
                            "client_order_id": _coid_mc,
                        },
                    )
                    limit_close_resp = limit_close_resp if isinstance(limit_close_resp, dict) else {}
                    _lc_state = str(limit_close_resp.get("state", "")).lower()
                    _lc_fill  = float(limit_close_resp.get("average_fill_price", 0) or 0)
                    _limit_oid = limit_close_resp.get("id")
                    if _lc_state == "closed" and _lc_fill > 0:
                        actual_exit = _lc_fill
                        exit_fee_usd = abs(float(
                            limit_close_resp.get("paid_commission") or
                            limit_close_resp.get("commission") or 0
                        ))
                        exit_exec_mode = "maker"
                    else:
                        # Wait up to 300ms for passive fill
                        for _ in range(3):  # 3 × 100ms = 300ms
                            await asyncio.sleep(0.1)
                            # No efficient WS fill check; if state flipped,
                            # the next query via cancel_order will tell us.
                except Exception:
                    _limit_oid = None

                # Cancel if still resting
                if _limit_oid and exit_exec_mode != "maker":
                    try:
                        await asyncio.to_thread(
                            self._delta._client.cancel_order,
                            product_id=product_id, order_id=_limit_oid,
                        )
                    except Exception:
                        pass

            # --- Attempt 2: market reduce_only (taker fallback) ---
            if exit_exec_mode != "maker":
                try:
                    # Phase 5.9-A: client_order_id for idempotent market close.
                    _coid_tc = _new_coid("vn_tc")
                    close_resp = await asyncio.to_thread(
                        self._delta._client.create_order,
                        {
                            "product_id": product_id,
                            "size": trade.position_size,
                            "side": close_side,
                            "order_type": "market_order",
                            "reduce_only": "true",
                            "client_order_id": _coid_tc,
                        },
                    )
                    close_resp = close_resp if isinstance(close_resp, dict) else {}
                    _ae = float(close_resp.get("average_fill_price") or close_resp.get("price") or 0)
                    if _ae > 0:
                        actual_exit = _ae
                    exit_fee_usd = abs(float(close_resp.get("paid_commission") or close_resp.get("commission") or 0))
                    exit_exec_mode = "taker"
                except Exception as e:
                    logger.error("USER %s: close order failed: %s %s — %s",
                                self.user_id[:8], trade.symbol, reason, e)

            # Phase 4.6 — ACCURATE PnL using real notional + fees.
            # OLD bug: pnl = pnl_pct × margin × leverage, over-stated by
            # ~25% on floor-capped trades AND never subtracted fees.
            # NEW: gross = (exit - entry) × lots × contract_size (real units);
            # net = gross − entry_fee − exit_fee. Delta's `commission` is the
            # primary source; fall back to 0.05% taker estimate if missing.
            _cs = float(trade.contract_size or 1.0)
            if trade.side == "long":
                gross_pnl = (actual_exit - trade.entry_price) * trade.position_size * _cs
            else:
                gross_pnl = (trade.entry_price - actual_exit) * trade.position_size * _cs

            # Fallback fee estimate if Delta didn't return commission
            if exit_fee_usd <= 0:
                exit_fee_usd = actual_exit * trade.position_size * _cs * 0.0005
            total_fees = float(trade.entry_fee_usd or 0) + exit_fee_usd

            # Phase 5.3 / T4.1 — FUNDING ACCOUNTING.
            # Phase 5.12.2 (2026-04-25) — MULTI-EVENT FUNDING RESET.
            # Old: linear interpolation of single entry-snapshot rate across full
            # hold (under-counts trades that span multiple 8h funding events
            # where rate changed). New: re-query funding rate at trade close
            # and use AVERAGE of (entry_rate, exit_rate). For trades spanning
            # 1 funding event: same as before. For trades spanning 2-3 events:
            # better approximation than linear-on-entry-rate-only.
            # Caveat: still imperfect — true accounting needs rate history per
            # period boundary. But for typical <24h hold, average is sufficient.
            # Sign: LONG pays positive rate (cost), short collects.
            funding_usd = 0.0
            try:
                _hours_held = max(0.0, (time.time() - trade.opened_at) / 3600.0)
                if _hours_held > 0 and trade.entry_price > 0:
                    _fr_entry = float(trade.funding_rate_at_entry or 0)
                    _fr_exit  = 0.0
                    # Always probe at close for current rate (cheap REST call)
                    if hasattr(self._delta, "get_funding_rate"):
                        try:
                            _fr_info_x = await asyncio.to_thread(
                                getattr(self._delta, "get_funding_rate", lambda s: None),
                                trade.symbol,
                            )
                            if _fr_info_x and isinstance(_fr_info_x, dict):
                                _fr_exit = float(_fr_info_x.get("funding_rate", 0) or 0)
                        except Exception:
                            _fr_exit = 0.0
                    # If entry snapshot missing, fall back to exit (better than 0)
                    if _fr_entry == 0.0:
                        _fr_entry = _fr_exit
                    # Average rate across hold (multi-event approximation)
                    _events_spanned = max(1, int(_hours_held // 8) + 1)
                    if _events_spanned >= 2 and _fr_exit != 0.0:
                        _fr_eff = (_fr_entry + _fr_exit) / 2.0
                    else:
                        _fr_eff = _fr_entry
                    _notional = trade.entry_price * trade.position_size * _cs
                    _periods = _hours_held / 8.0
                    _raw_funding = _notional * _fr_eff * _periods
                    # Long pays positive rate; short receives (negate)
                    funding_usd = _raw_funding if trade.side == "long" else -_raw_funding
            except Exception:
                funding_usd = 0.0
            trade.funding_usd = round(funding_usd, 6)

            net_pnl = gross_pnl - total_fees - funding_usd

            # Phase 5.3.1 (2026-04-23) — POST-HOC label correction.
            # Paper's signal_tracker.py:1205 uses `pnl_pct > 0.0` (STRICT)
            # to decide trail_profit vs stop_loss. Our Apr 23 bug:
            # SOL short @ 85.5210 exit @ 85.5220 labeled trail_profit -$0.28
            # because SL was trailed briefly into profit territory (85.515)
            # then price spiked back up past SL to 85.5220 at fill time.
            # The pre-close reason said trail_profit (correct decision);
            # the post-fill outcome was a loss (honest label should be sl_hit).
            # Also ports paper's `partial_win` concept: big-peak wins that
            # banked via trail vs small-peak wins are differentiated so
            # the stats tell a cleaner story.
            if reason in ("trail_profit", "sl_hit"):
                if net_pnl > 0 and trade.peak_mfe_r >= 0.50:
                    reason = "partial_win"
                elif net_pnl > 0:
                    reason = "trail_profit"
                else:
                    reason = "sl_hit"

            # pnl_pct for reporting — based on entry cost (notional)
            _notional_in = trade.entry_price * trade.position_size * _cs
            pnl_pct = (net_pnl / _notional_in) if _notional_in > 0 else 0
            pnl_usd = net_pnl  # primary reported value is NET

            # Phase 5.0 — CRITICAL loud marker on live exits (real $$$ realised).
            _mode_tag = "💰 LIVE" if self._is_live else "DEMO"
            _log_fn = logger.critical if self._is_live else logger.warning
            _log_fn(
                "%s USER REAL EXIT: %s %s %s | entry=%.4f exit=%.4f | gross=$%.3f fees=$%.3f fund=$%.3f NET=$%.3f | %s | mfe=%.2fR exit_mode=%s",
                _mode_tag, self.user_email, trade.symbol, trade.side,
                trade.entry_price, actual_exit,
                gross_pnl, total_fees, funding_usd, net_pnl,
                reason, trade.peak_mfe_r, exit_exec_mode or "?",
            )

            # Record
            closed = {
                "trade_id": trade.trade_id,
                "user_id": trade.user_id,
                "symbol": trade.symbol,
                "side": trade.side,
                "entry_price": trade.entry_price,
                "exit_price": actual_exit,
                "pnl_usd": round(net_pnl, 4),
                "gross_pnl_usd": round(gross_pnl, 4),
                "fees_usd": round(total_fees, 4),
                "funding_usd": round(funding_usd, 6),
                "pnl_pct": round(pnl_pct * 100, 4),
                "exit_reason": reason,
                "peak_mfe_r": round(trade.peak_mfe_r, 4),
                "margin": trade.margin,
                "leverage": trade.leverage,
                "contract_size": _cs,
                "scanner": trade.scanner,
                "grade": trade.grade,
                "duration_sec": time.time() - trade.opened_at,
                "closed_at": datetime.now(timezone.utc).isoformat(),
            }
            self.closed_trades.append(closed)
            if len(self.closed_trades) > 500:
                self.closed_trades = self.closed_trades[-500:]

            # Update CB
            self.cb.record_trade(pnl_usd)

            # Remove from open
            self.open_trades.pop(trade.trade_id, None)

            # Record to DB (Phase 4.6 — pass net_pnl, gross_pnl, fees, actual_exit)
            if self._db_pool:
                try:
                    await self._record_trade_db(
                        trade, "closed",
                        exit_price=actual_exit,
                        pnl_usd=net_pnl,
                        reason=reason,
                        gross_pnl=gross_pnl,
                        fees_usd=total_fees,
                    )
                except Exception as e:
                    logger.error("USER %s: DB close record failed: %s", self.user_id[:8], e)

        except Exception as e:
            logger.error("USER %s: close trade error: %s", self.user_id[:8], e)

    # ══════════════════════════════════════════════════════════════
    # DB PERSISTENCE
    # ══════════════════════════════════════════════════════════════

    async def _record_trade_db(self, trade: UserTradeRecord, status: str,
                                exit_price: float = 0, pnl_usd: float = 0,
                                reason: str = "",
                                gross_pnl: float = 0, fees_usd: float = 0):
        """Record trade open/close to user_trades table.

        Phase 4.6 (2026-04-22) — accurate PnL:
          pnl_usd  = NET P&L (gross − fees)
          metadata.gross_pnl_usd, fees_usd, contract_size for audit trail.
          fees_usd column at top level for analytics queries.
        """
        if not self._db_pool:
            return
        try:
            import json as _json
            # Phase 4.2 — include all fields needed to rebuild a
            # UserTradeRecord on restart reconciliation.
            _open_meta_dict = {
                "scanner": trade.scanner,
                "grade": trade.grade,
                "leverage": trade.leverage,
                "phase": CURRENT_PHASE,  # was hardcoded '5.5.3' — see Phase 5.20-FIX3
                "fee_type": trade.fee_type,
                "server_stop_id": trade.server_stop_id,
                # Reconciliation-required fields
                "stop_loss": trade.stop_loss,
                "take_profit": trade.take_profit,
                "margin": trade.margin,
                "trade_type": trade.trade_type,
                "regime": trade.regime,
                "ml_prob": trade.ml_prob,
                "initial_risk": trade.initial_risk,
                "tick_size": trade.tick_size,
                "product_id": trade.product_id,
                # Phase 4.6 — PnL accuracy fields
                "contract_size": trade.contract_size,
                "entry_fee_usd": round(float(trade.entry_fee_usd or 0), 6),
                # G2 fix (2026-04-26): persist entry_exec_mode + maker_mode at OPEN
                # so per-mode performance attribution survives even if trade is
                # never closed normally (e.g. force-flat by kill_switch). Was
                # previously only written in close_meta — leading to 98.6% of
                # 30d trades showing entry_exec_mode='(empty)' in DB.
                "entry_exec_mode": getattr(trade, "entry_exec_mode", "") or "",
                "maker_mode_used": getattr(trade, "maker_mode_used", "") or "",
                "maker_mode_id": int(getattr(trade, "maker_mode_id", -1) or -1),
            }
            # Phase 2 — Shadow-of-Shadow attribution (2026-04-27 fix)
            # _record_trade_db was building open_meta from a hardcoded field
            # list that ignored the Phase 2 fan-out's mutated meta dict.
            # Result: 10 fan-out shadow trades created at 07:06:35 UTC with
            # ZERO is_phase2_virtual / exit_config_id metadata → leaderboard
            # SQL `WHERE metadata->>'is_phase2_virtual'='true'` matched 0
            # rows, blocking Phase 2 verdict. Read attribution off the
            # trade attribute set by _execute_shadow at fan-out time.
            if getattr(trade, "_is_phase2_virtual", False):
                _open_meta_dict["is_phase2_virtual"] = True
                _ec = getattr(trade, "_exit_config", None) or {}
                if _ec.get("id"):
                    _open_meta_dict["exit_config_id"] = _ec["id"]
                    _open_meta_dict["exit_config_summary"] = (
                        f"max_age={_ec.get('max_age_sec', '?')}s "
                        f"trail={_ec.get('trail_trigger', '?')}R/"
                        f"{_ec.get('trail_lock', 0):.0%} "
                        f"dead_kill={_ec.get('dead_kill_R')} "
                        f"tp_R={_ec.get('tp_R')}"
                    )
            open_meta = _json.dumps(_open_meta_dict)
            # Phase 5.12 (2026-04-24) — Persist funding accounting for Sharpe/backtest.
            # Funding was already computed (lines ~2510-2542) but only lived in-memory
            # on UserTradeRecord. Persisting enables correct risk-adjusted return
            # analysis across historical trades (was previously lost at close).
            # Single-snapshot linear interpolation is the current model; multi-event
            # reset (re-snapshotting rate at each 8h boundary) is a future enhancement.
            _funding_usd = float(getattr(trade, "funding_usd", 0) or 0)
            _fr_entry = float(getattr(trade, "funding_rate_at_entry", 0) or 0)
            try:
                _hours_held = max(0.0, (time.time() - trade.opened_at) / 3600.0)
            except Exception:
                _hours_held = 0.0

            close_meta = _json.dumps({
                "exit_reason": reason,
                "peak_mfe_r": round(trade.peak_mfe_r, 4),
                "phase": CURRENT_PHASE,  # was hardcoded '5.12' — see Phase 5.20-FIX3
                # Phase 4.6 — gross & fees for audit; pnl_usd (col) = net
                "gross_pnl_usd": round(float(gross_pnl), 4),
                "net_pnl_usd": round(float(pnl_usd), 4),
                "fees_usd": round(float(fees_usd), 4),
                # Phase 5.12 — funding persistence (for Sharpe/backtest)
                "funding_usd": round(_funding_usd, 6),
                "funding_rate_at_entry": round(_fr_entry, 8),
                "funding_hours_held": round(_hours_held, 3),
                "funding_events_spanned": int(_hours_held // 8.0),  # rough count of 8h boundaries crossed
                # Phase 5.19 — maker mode used at entry (for A/B/C attribution)
                "maker_mode_used": getattr(trade, "maker_mode_used", "") or "",
                "maker_mode_id": int(getattr(trade, "maker_mode_id", -1)),
                "entry_exec_mode": getattr(trade, "entry_exec_mode", "") or "",
                # Phase 5.5-DIAG — per-feature attribution markers
                "n2_sl_retries": int(getattr(trade, "_sl_retry_attempts_total", 0)),
                "n3_min_lock_r": round(float(getattr(trade, "_min_lock_r_used", 0.30)), 3),
                # Aggregate fee % of gross — quick-glance economics
                "fee_pct_of_gross": round(
                    abs(fees_usd) / max(abs(gross_pnl), 0.001) * 100, 1
                ),
                # Total cost (fees + funding) — what paper doesn't see
                "total_cost_usd": round(float(fees_usd) + _funding_usd, 4),
                # 2026-04-27 Path A — maker counterfactual.
                # cf_maker_savings_*: $ saved per trade if maker filled.
                # cf_net_at_50pct_maker = pnl + 50% × counterfactual savings.
                # Use the LAST set values from _close_shadow's calculation
                # block (set on the trade obj). Real fills compute these to
                # 0 since taker→maker isn't applicable for live (live tracks
                # actual fee_type per side).
                "cf_maker_savings_100pct": float(
                    getattr(trade, "_cf_maker_savings_100pct", 0.0) or 0.0
                ),
                "cf_maker_savings_50pct": float(
                    getattr(trade, "_cf_maker_savings_50pct", 0.0) or 0.0
                ),
                "cf_net_at_50pct_maker": (
                    None if getattr(trade, "_cf_net_at_50pct_maker", None) is None
                    else float(trade._cf_net_at_50pct_maker)
                ),
            })

            # Phase 5.6-B — shadow trades tagged trade_type='shadow' so dashboards
            # and analytics can cleanly separate them from real/demo execution.
            _trade_type = "shadow" if getattr(trade, "_is_shadow", False) else "real"

            # G4 fix (2026-04-26): build real signal_data jsonb (was hardcoded '{}')
            # Includes signal_price (intended entry from strategy) + actual entry_price
            # so slippage = abs(entry_price - signal_price) / signal_price * 10000 (bps)
            # is computable downstream by analytics.
            _signal_data = _json.dumps({
                "signal_price": float(getattr(trade, "_signal_price", 0) or 0) or trade.entry_price,
                "entry_price": trade.entry_price,
                "signal_id": getattr(trade, "_signal_id", "") or "",
                "scanner": trade.scanner,
                "regime": trade.regime,
                "side": trade.side,
            })

            async with self._db_pool.acquire() as conn:
                if status == "open":
                    # 2026-04-27 — capture the DB-generated UUID via RETURNING id
                    # and stamp it onto trade._db_id. Without this, the close
                    # path's UPDATE has no way to identify the SPECIFIC row,
                    # falls back to a (user_id, symbol, latest opened_at)
                    # subquery, and silently mis-attributes (or misses) closes
                    # for Phase 2 fan-out where 10 trades share the same
                    # (user_id, symbol, opened_at) tuple. Symptom: SHADOW EXIT
                    # log fires correctly but DB rows stay status='open' until
                    # Agent 9-A's 60min sweep marks them auto_responder_stuck_60m.
                    row = await conn.fetchrow("""
                        INSERT INTO user_trades (id, user_id, trade_type, symbol, side,
                            entry_price, quantity, status, signal_data, metadata)
                        VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6, 'open',
                            $7::jsonb, $8::jsonb)
                        RETURNING id::text
                    """, self.user_id, _trade_type, trade.symbol, trade.side,
                        trade.entry_price, float(trade.position_size),
                        _signal_data, open_meta)
                    if row and row.get("id"):
                        trade._db_id = row["id"]
                else:
                    # 2026-04-27 — close by trade._db_id (set on INSERT or by
                    # reconcile rebuild). Falls back to legacy subquery only
                    # when _db_id is missing (pre-fix trades from before this
                    # patch shipped + reconciled trades that didn't capture
                    # the id properly). Logs which path was used so we can
                    # see if the fallback is still firing in production.
                    _db_id = getattr(trade, "_db_id", None)
                    if _db_id:
                        result = await conn.execute("""
                            UPDATE user_trades SET
                                status='closed',
                                exit_price=$1,
                                pnl_usd=$2,
                                fees_usd=$3,
                                closed_at=NOW(),
                                metadata=metadata || $4::jsonb
                            WHERE id = $5::uuid
                        """, exit_price, pnl_usd, float(fees_usd), close_meta,
                            _db_id)
                        # asyncpg execute returns 'UPDATE 1' or 'UPDATE 0'
                        if result and result.endswith(" 0"):
                            logger.warning(
                                "USER %s: close UPDATE matched 0 rows for trade_id=%s db_id=%s — row likely already closed",
                                self.user_id[:8], trade.trade_id, _db_id,
                            )
                    else:
                        # Legacy fallback — kept for backwards compat. Phase 2
                        # fan-out trades created BEFORE this fix don't have
                        # _db_id set; this path is misattribution-prone but
                        # better than silently dropping the close. Log loudly.
                        logger.warning(
                            "USER %s: close FALLBACK (no _db_id) for trade_id=%s symbol=%s — "
                            "subquery may mis-attribute on Phase 2 fan-out",
                            self.user_id[:8], trade.trade_id, trade.symbol,
                        )
                        await conn.execute("""
                            UPDATE user_trades SET
                                status='closed',
                                exit_price=$1,
                                pnl_usd=$2,
                                fees_usd=$3,
                                closed_at=NOW(),
                                metadata=metadata || $4::jsonb
                            WHERE id = (
                                SELECT id FROM user_trades
                                WHERE user_id=$5 AND symbol=$6 AND status='open'
                                ORDER BY opened_at DESC
                                LIMIT 1
                            )
                        """, exit_price, pnl_usd, float(fees_usd), close_meta,
                            self.user_id, trade.symbol)
        except Exception as e:
            logger.error("USER %s: DB error: %s", self.user_id[:8], e)

    # ══════════════════════════════════════════════════════════════
    # STATUS (for dashboard API)
    # ══════════════════════════════════════════════════════════════

    def get_status(self) -> Dict[str, Any]:
        """Return user's real trading status for dashboard."""
        self.cb.check_daily_reset()
        return {
            "user_id": self.user_id,
            "email": self.user_email,
            "enabled": self.enabled,
            "balance": self._cached_balance,
            "open_count": len(self.open_trades),
            "open_positions": [
                {
                    "trade_id": t.trade_id,
                    "symbol": t.symbol,
                    "side": t.side,
                    "entry_price": t.entry_price,
                    "stop_loss": t.stop_loss,
                    "margin": t.margin,
                    "leverage": t.leverage,
                    "scanner": t.scanner,
                    "grade": t.grade,
                    "peak_mfe_r": t.peak_mfe_r,
                    "duration_sec": time.time() - t.opened_at,
                }
                for t in self.open_trades.values()
            ],
            "circuit_breaker": {
                "is_tripped": self.cb.is_tripped,
                "consecutive_losses": self.cb.consecutive_losses,
                "daily_loss_usd": round(self.cb.daily_loss_usd, 2),
                "trade_count_today": self.cb.trade_count_today,
                "total_pnl": round(self.cb.total_pnl, 2),
            },
            "closed_trades": self.closed_trades[-20:],
            "config": {
                "max_leverage": self.max_leverage,
                "max_daily_loss": self.max_daily_loss,
                "max_daily_trades": self.max_daily_trades,
                "preferred_symbols": self.preferred_symbols,
                "min_confidence": self.min_confidence,
                "ml_threshold": self.ml_threshold,
                "size_multiplier": self.size_multiplier,
            },
        }

    async def refresh_balance(self):
        """Fetch user's exchange balance."""
        try:
            if hasattr(self._delta, 'fetch_balance'):
                # Phase 4.4 — off event loop. fetch_balance does a
                # synchronous HTTP call to /v2/wallet/balances.
                bal = await asyncio.to_thread(self._delta.fetch_balance)
                if bal and isinstance(bal, (int, float)):
                    self._cached_balance = float(bal)
        except Exception as e:
            logger.debug("USER %s: balance fetch failed: %s", self.user_id[:8], e)
