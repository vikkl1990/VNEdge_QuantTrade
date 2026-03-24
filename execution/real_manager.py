"""
Real Trading Manager — Mirror paper trades to real exchange.

Runs as an optional sidecar alongside paper trading. Every paper trade
that succeeds gets mirrored to the real exchange with adaptive position
sizing. Real trading failures never affect paper trading.

Safety:
- Pre-flight checks before every real order
- $25/day loss circuit breaker
- 3 consecutive loss pause
- Adaptive sizing based on wallet balance ($10-25 margin)
- Balance reserve (15% untouchable)
- dry_run mode for logging without execution
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from execution.engine import ExecutionEngine
from execution.trade import Trade, TradeStatus

logger = logging.getLogger("bot.real_trading")

STATE_FILE = Path("storage/real_trading_state.json")


class RealCircuitBreaker:
    """Daily loss limit + consecutive loss tracking for real trades."""

    def __init__(
        self,
        daily_loss_limit: float = 25.0,
        max_consecutive_losses: int = 3,
    ):
        self.daily_loss_limit = daily_loss_limit
        self.max_consecutive_losses = max_consecutive_losses
        self.daily_pnl: float = 0.0
        self.total_pnl: float = 0.0
        self.consecutive_losses: int = 0
        self.today: str = str(date.today())
        self.is_tripped: bool = False
        self.trip_reason: str = ""
        self.trade_count_today: int = 0

    def _maybe_reset_daily(self):
        """Reset daily counters at midnight UTC."""
        today = str(datetime.now(timezone.utc).date())
        if today != self.today:
            logger.info("REAL CB: Daily reset — yesterday PnL=$%.2f, trades=%d",
                       self.daily_pnl, self.trade_count_today)
            self.daily_pnl = 0.0
            self.trade_count_today = 0
            self.today = today
            self.is_tripped = False
            self.trip_reason = ""

    def record_trade(self, pnl_usd: float):
        """Record a real trade result."""
        self._maybe_reset_daily()
        self.daily_pnl += pnl_usd
        self.total_pnl += pnl_usd
        self.trade_count_today += 1

        if pnl_usd < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

        self._check_trip()

    def _check_trip(self):
        """Check if circuit breaker should trip."""
        if self.daily_pnl <= -self.daily_loss_limit:
            self.is_tripped = True
            self.trip_reason = f"Daily loss ${abs(self.daily_pnl):.2f} exceeds ${self.daily_loss_limit} limit"
            logger.critical("REAL CB TRIPPED: %s", self.trip_reason)
        elif self.consecutive_losses >= self.max_consecutive_losses:
            self.is_tripped = True
            self.trip_reason = f"{self.consecutive_losses} consecutive real losses"
            logger.critical("REAL CB TRIPPED: %s", self.trip_reason)

    def is_allowed(self) -> Tuple[bool, str]:
        """Check if a new real trade is allowed."""
        self._maybe_reset_daily()
        if self.is_tripped:
            return False, self.trip_reason
        if self.daily_pnl <= -self.daily_loss_limit:
            return False, f"Daily loss ${abs(self.daily_pnl):.2f} exceeds ${self.daily_loss_limit}"
        if self.consecutive_losses >= self.max_consecutive_losses:
            return False, f"{self.consecutive_losses} consecutive losses"
        return True, ""

    def to_dict(self) -> dict:
        return {
            "daily_pnl": round(self.daily_pnl, 2),
            "total_pnl": round(self.total_pnl, 2),
            "consecutive_losses": self.consecutive_losses,
            "trade_count_today": self.trade_count_today,
            "is_tripped": self.is_tripped,
            "trip_reason": self.trip_reason,
            "today": self.today,
            "daily_loss_limit": self.daily_loss_limit,
        }

    def from_dict(self, d: dict):
        self.daily_pnl = d.get("daily_pnl", 0)
        self.total_pnl = d.get("total_pnl", 0)
        self.consecutive_losses = d.get("consecutive_losses", 0)
        self.trade_count_today = d.get("trade_count_today", 0)
        self.is_tripped = d.get("is_tripped", False)
        self.trip_reason = d.get("trip_reason", "")
        self.today = d.get("today", str(date.today()))
        self._maybe_reset_daily()


class RealTradingManager:
    """
    Mirrors paper trades to real exchange with safety controls.

    Usage:
        mgr = RealTradingManager(exchange, config)
        await mgr.mirror_paper_trade(symbol, signal, paper_trade_id)
        await mgr.mirror_paper_exit(paper_trade_id, exit_price, reason)
    """

    def __init__(self, exchange, config: Dict[str, Any], risk_manager=None):
        self.exchange = exchange
        self.config = config
        self.risk_manager = risk_manager

        rt_cfg = config.get("real_trading", {})
        self.enabled: bool = rt_cfg.get("enabled", False)
        self.dry_run: bool = rt_cfg.get("dry_run", True)
        self.min_margin: float = rt_cfg.get("min_margin_per_trade", 10.0)
        self.max_margin: float = rt_cfg.get("max_margin_per_trade", 25.0)
        self.max_open: int = rt_cfg.get("max_open_positions", 2)
        self.reserve_pct: float = rt_cfg.get("balance_reserve_pct", 15) / 100.0
        self.min_balance: float = rt_cfg.get("min_balance_to_trade", 30.0)
        self.leverage_cap: int = rt_cfg.get("leverage_cap", 10)

        self.circuit_breaker = RealCircuitBreaker(
            daily_loss_limit=rt_cfg.get("daily_loss_limit_usd", 25.0),
            max_consecutive_losses=rt_cfg.get("max_consecutive_losses", 3),
        )

        # Engine wraps the existing ExecutionEngine
        self.engine = ExecutionEngine(exchange, config, risk_manager)

        # Track real trades: paper_trade_id → real Trade
        self.real_trades: Dict[str, Trade] = {}
        self.closed_real_trades: List[Dict] = []
        self.paper_to_real: Dict[str, str] = {}  # paper_id → real_id

        # Balance cache
        self._cached_balance: Optional[float] = None
        self._balance_ts: float = 0

        # API failure tracking
        self._api_failures: int = 0
        self._max_api_failures: int = 5

        # Load persisted state (may override enabled/dry_run from saved toggle)
        self._load_state()

        mode = "DRY RUN" if self.dry_run else "LIVE"
        status = "ENABLED" if self.enabled else "DISABLED"
        logger.info(
            "RealTradingManager: %s (%s) | margin=$%.0f-$%.0f | "
            "max_open=%d | daily_limit=$%.0f | leverage_cap=%dx",
            status, mode, self.min_margin, self.max_margin,
            self.max_open, self.circuit_breaker.daily_loss_limit,
            self.leverage_cap,
        )

    # ==================================================================
    # Mirror Entry
    # ==================================================================

    async def mirror_paper_trade(
        self, symbol: str, signal: Dict[str, Any], paper_trade_id: Optional[str] = None,
    ) -> Optional[Dict]:
        """
        Mirror a paper trade to the real exchange.

        Returns dict with mirror result, or None if skipped/failed.
        Paper trading is NEVER affected by this method.
        """
        if not self.enabled:
            return {"status": "disabled"}

        # Pre-flight safety checks
        allowed, reason = await self._preflight_checks(symbol, signal)
        if not allowed:
            logger.info("REAL SKIP: %s %s — %s", symbol, signal.get("side", "?"), reason)
            return {"status": "skipped", "reason": reason}

        # Calculate adaptive position size
        margin, position_size = await self._calculate_position_size(
            symbol, signal,
        )
        if position_size <= 0:
            logger.warning("REAL SKIP: %s — position size zero", symbol)
            return {"status": "skipped", "reason": "position_size_zero"}

        leverage = min(signal.get("leverage", 5), self.leverage_cap)

        if self.dry_run:
            logger.info(
                "REAL [DRY RUN]: %s %s %s | margin=$%.2f | pos=$%.2f | "
                "size=%.6f | lev=%dx | entry=%.4f | SL=%.4f",
                symbol, signal.get("side", "?"), signal.get("metadata", {}).get("setup_type", "?"),
                margin, margin * leverage, position_size, leverage,
                signal.get("entry_price", 0), signal.get("stop_loss", 0),
            )
            return {
                "status": "dry_run",
                "symbol": symbol,
                "side": signal.get("side"),
                "margin": margin,
                "position_usd": margin * leverage,
                "position_size": position_size,
                "leverage": leverage,
                "paper_trade_id": paper_trade_id,
            }

        # === REAL EXECUTION ===
        try:
            # Build signal with capped leverage
            real_signal = dict(signal)
            real_signal["leverage"] = leverage

            trade = await self.engine.execute_entry(real_signal, position_size)

            if trade.status == TradeStatus.FAILED:
                logger.error("REAL FAILED: %s %s — %s", symbol, signal.get("side"), trade.entry_reason)
                return {"status": "failed", "reason": trade.entry_reason}

            # Track the mapping
            if paper_trade_id:
                self.paper_to_real[paper_trade_id] = trade.trade_id
            self.real_trades[trade.trade_id] = trade

            self._api_failures = 0  # Reset on success

            logger.info(
                "🔴 REAL ENTRY: %s %s %s @ %.4f | margin=$%.2f | pos=$%.2f | lev=%dx | trade=%s",
                symbol, signal.get("side"), signal.get("metadata", {}).get("setup_type", "?"),
                trade.entry_price, margin, trade.entry_price * trade.position_size,
                leverage, trade.trade_id,
            )

            self._save_state()
            return {
                "status": "mirrored",
                "trade_id": trade.trade_id,
                "entry_price": trade.entry_price,
                "position_size": trade.position_size,
                "margin": margin,
                "leverage": leverage,
            }

        except Exception as e:
            self._api_failures += 1
            logger.error("REAL ERROR: %s — %s (failure %d/%d)",
                        symbol, e, self._api_failures, self._max_api_failures)

            if self._api_failures >= self._max_api_failures:
                self.enabled = False
                logger.critical(
                    "🔴 REAL TRADING AUTO-DISABLED: %d consecutive API failures",
                    self._api_failures,
                )

            return {"status": "failed", "reason": str(e)}

    # ==================================================================
    # Mirror Exit
    # ==================================================================

    async def mirror_paper_exit(
        self, paper_trade_id: str, exit_price: float, reason: str,
    ) -> Optional[Dict]:
        """
        Close the real position when paper trade closes.
        """
        real_trade_id = self.paper_to_real.get(paper_trade_id)
        if not real_trade_id:
            return None  # No real trade for this paper trade

        trade = self.real_trades.get(real_trade_id)
        if not trade:
            return None

        if self.dry_run:
            # Calculate what PnL would have been
            if trade.side.value == "long":
                pnl_pct = (exit_price - trade.entry_price) / trade.entry_price
            else:
                pnl_pct = (trade.entry_price - exit_price) / trade.entry_price

            notional = trade.entry_price * trade.position_size
            pnl_usd = pnl_pct * notional
            fee_est = notional * 0.0015  # ~0.15% round trip
            net_pnl = pnl_usd - fee_est

            logger.info(
                "REAL [DRY RUN] EXIT: %s %s | entry=%.4f exit=%.4f | "
                "gross=$%.2f fees=$%.2f net=$%.2f | reason=%s",
                trade.symbol, trade.side.value,
                trade.entry_price, exit_price,
                pnl_usd, fee_est, net_pnl, reason,
            )

            self.circuit_breaker.record_trade(net_pnl)
            self._record_closed_trade(trade, exit_price, net_pnl, reason, dry_run=True)
            return {"status": "dry_run_exit", "pnl_usd": net_pnl}

        # === REAL EXIT ===
        try:
            closed_trade = await self.engine.execute_exit(trade, reason)

            pnl_usd = getattr(closed_trade, "realized_pnl", 0) or 0
            fee = getattr(closed_trade, "total_fees", 0) or 0

            self.circuit_breaker.record_trade(pnl_usd)
            self._record_closed_trade(trade, exit_price, pnl_usd, reason)

            logger.info(
                "🔴 REAL EXIT: %s %s | PnL=$%.2f fees=$%.2f | reason=%s | trade=%s",
                trade.symbol, trade.side.value, pnl_usd, fee, reason, trade.trade_id,
            )

            self._save_state()
            return {"status": "exited", "pnl_usd": pnl_usd, "trade_id": trade.trade_id}

        except Exception as e:
            logger.error(
                "🔴 REAL EXIT FAILED: %s — %s | MANUAL INTERVENTION MAY BE NEEDED",
                trade.symbol, e,
            )
            # Retry once
            try:
                await asyncio.sleep(2)
                closed_trade = await self.engine.execute_exit(trade, reason)
                return {"status": "exited_retry", "trade_id": trade.trade_id}
            except Exception as e2:
                logger.critical(
                    "🔴 REAL EXIT RETRY FAILED: %s — %s | MANUAL CLOSE NEEDED",
                    trade.symbol, e2,
                )
                return {"status": "exit_failed", "reason": str(e2)}

    # ==================================================================
    # Pre-Flight Safety Checks
    # ==================================================================

    async def _preflight_checks(self, symbol: str, signal: dict) -> Tuple[bool, str]:
        """Run all safety checks before placing a real order."""

        # 1. Circuit breaker
        allowed, reason = self.circuit_breaker.is_allowed()
        if not allowed:
            return False, f"circuit_breaker: {reason}"

        # 2. API failure auto-disable
        if self._api_failures >= self._max_api_failures:
            return False, f"api_failures: {self._api_failures} consecutive failures"

        # 3. Max open positions
        open_count = len(self.real_trades)
        if open_count >= self.max_open:
            return False, f"max_open: {open_count}/{self.max_open} positions"

        # 4. Duplicate check
        side = signal.get("side", "")
        for t in self.real_trades.values():
            if t.symbol == symbol and t.side.value == side:
                return False, f"duplicate: {symbol} {side} already open"

        # 5. Balance check
        balance = await self._get_balance()
        if balance < self.min_balance:
            return False, f"low_balance: ${balance:.2f} < ${self.min_balance}"

        return True, ""

    # ==================================================================
    # Position Sizing
    # ==================================================================

    async def _calculate_position_size(
        self, symbol: str, signal: dict,
    ) -> Tuple[float, float]:
        """
        Adaptive margin: $10-25 based on wallet balance.
        Returns (margin_usd, position_size_in_base_currency).
        """
        balance = await self._get_balance()
        usable = balance * (1 - self.reserve_pct)

        # Adaptive margin tiers
        if usable < 50:
            margin = self.min_margin
        elif usable < 100:
            margin = min(15.0, usable * 0.20)
        elif usable < 200:
            margin = min(20.0, usable * 0.15)
        else:
            margin = min(self.max_margin, usable * 0.12)

        margin = max(margin, self.min_margin)
        margin = min(margin, self.max_margin)

        # Notional = margin × leverage
        leverage = min(signal.get("leverage", 5), self.leverage_cap)
        notional = margin * leverage
        entry_price = signal.get("entry_price", 0)

        if entry_price <= 0:
            return 0, 0

        position_size = notional / entry_price

        return margin, position_size

    # ==================================================================
    # Balance
    # ==================================================================

    async def _get_balance(self) -> float:
        """Fetch real exchange wallet balance with 60s cache."""
        now = time.time()
        if self._cached_balance is not None and (now - self._balance_ts) < 60:
            return self._cached_balance

        try:
            bal = await self.exchange.fetch_balance()
            usdt = 0
            # ccxt returns dict-like object: bal['USDT']['free'] or bal['free']['USDT']
            if isinstance(bal, dict):
                # Standard ccxt format: {'free': {'USDT': 118.46}, 'total': {...}}
                free = bal.get("free", {})
                total = bal.get("total", {})
                if isinstance(free, dict):
                    usdt = free.get("USDT", 0)
                elif isinstance(total, dict):
                    usdt = total.get("USDT", 0)
                # Some exchanges: {'USDT': {'free': 118.46}}
                if not usdt:
                    usdt_data = bal.get("USDT", {})
                    if isinstance(usdt_data, dict):
                        usdt = usdt_data.get("free", usdt_data.get("total", 0))
                    elif isinstance(usdt_data, (int, float)):
                        usdt = usdt_data
            # ccxt Balance object with attributes (Delta uses 'USD' not 'USDT')
            elif hasattr(bal, 'free') and isinstance(bal.free, dict):
                usdt = bal.free.get("USDT", bal.free.get("USD", 0))
            elif hasattr(bal, 'total') and isinstance(bal.total, dict):
                usdt = bal.total.get("USDT", bal.total.get("USD", 0))
            self._cached_balance = float(usdt) if usdt else 0
            self._balance_ts = now
            logger.info("REAL: Exchange balance fetched: $%.2f USDT", self._cached_balance)
        except Exception as e:
            logger.warning("REAL: Balance fetch failed: %s (type=%s)", e, type(e).__name__)
            if self._cached_balance is None:
                self._cached_balance = 0

        return self._cached_balance

    # ==================================================================
    # State Persistence
    # ==================================================================

    def _record_closed_trade(self, trade: Trade, exit_price: float,
                              pnl_usd: float, reason: str, dry_run: bool = False):
        """Record a closed real trade."""
        self.closed_real_trades.append({
            "trade_id": trade.trade_id,
            "symbol": trade.symbol,
            "side": trade.side.value,
            "entry_price": trade.entry_price,
            "exit_price": exit_price,
            "pnl_usd": round(pnl_usd, 2),
            "reason": reason,
            "dry_run": dry_run,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        # Remove from active
        self.real_trades.pop(trade.trade_id, None)
        # Remove paper mapping
        for paper_id, real_id in list(self.paper_to_real.items()):
            if real_id == trade.trade_id:
                del self.paper_to_real[paper_id]
                break

    def _save_state(self):
        """Persist real trading state to disk."""
        state = {
            "circuit_breaker": self.circuit_breaker.to_dict(),
            "paper_to_real": self.paper_to_real,
            "closed_trades": self.closed_real_trades[-100:],  # keep last 100
            "enabled": self.enabled,
            "dry_run": self.dry_run,
            "api_failures": self._api_failures,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(state, indent=2))
        except Exception as e:
            logger.error("Failed to save real trading state: %s", e)

    def _load_state(self):
        """Load persisted state on startup, including toggle state."""
        if not STATE_FILE.exists():
            return
        try:
            state = json.loads(STATE_FILE.read_text())
            self.circuit_breaker.from_dict(state.get("circuit_breaker", {}))
            self.paper_to_real = state.get("paper_to_real", {})
            self.closed_real_trades = state.get("closed_trades", [])
            self._api_failures = state.get("api_failures", 0)
            # Restore toggle state from dashboard
            if "enabled" in state:
                self.enabled = state["enabled"]
            if "dry_run" in state:
                self.dry_run = state["dry_run"]
            logger.info(
                "REAL: Loaded state — enabled=%s, dry_run=%s, CB daily=$%.2f, "
                "total=$%.2f, %d closed trades",
                self.enabled, self.dry_run,
                self.circuit_breaker.daily_pnl, self.circuit_breaker.total_pnl,
                len(self.closed_real_trades),
            )
        except Exception as e:
            logger.warning("Failed to load real trading state: %s", e)

    # ==================================================================
    # Status for Dashboard
    # ==================================================================

    def get_status(self) -> Dict:
        """Return current real trading status for dashboard."""
        open_trades = []
        for t in self.real_trades.values():
            open_trades.append({
                "trade_id": t.trade_id,
                "symbol": t.symbol,
                "side": t.side.value,
                "entry_price": t.entry_price,
                "position_size": t.position_size,
            })

        return {
            "enabled": self.enabled,
            "dry_run": self.dry_run,
            "mode": "DRY RUN" if self.dry_run else ("LIVE" if self.enabled else "DISABLED"),
            "balance": self._cached_balance or 0,
            "circuit_breaker": self.circuit_breaker.to_dict(),
            "open_positions": open_trades,
            "open_count": len(self.real_trades),
            "closed_today": self.circuit_breaker.trade_count_today,
            "total_closed": len(self.closed_real_trades),
            "paper_to_real_mappings": len(self.paper_to_real),
            "api_failures": self._api_failures,
            "recent_trades": self.closed_real_trades[-10:],
        }
