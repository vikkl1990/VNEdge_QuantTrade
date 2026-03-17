"""
Risk management engine for the crypto trading bot.

Enforces all pre-trade and intra-trade risk rules including position sizing,
daily loss limits, exposure caps, circuit breakers, cooldown timers, and
volatility kill switches.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Correlation groups for crypto assets
# ──────────────────────────────────────────────────────────────────────

CORRELATION_GROUPS: Dict[str, List[str]] = {
    "btc_ecosystem": ["BTC/USDT", "BTC/USDC", "WBTC/USDT"],
    "eth_ecosystem": ["ETH/USDT", "ETH/USDC", "STETH/USDT"],
    "layer1_alts": ["SOL/USDT", "AVAX/USDT", "ADA/USDT", "DOT/USDT", "NEAR/USDT"],
    "layer2": ["ARB/USDT", "OP/USDT", "MATIC/USDT"],
    "defi": ["UNI/USDT", "AAVE/USDT", "LINK/USDT", "MKR/USDT"],
    "meme": ["DOGE/USDT", "SHIB/USDT", "PEPE/USDT", "WIF/USDT"],
}


def _symbol_group(symbol: str) -> Optional[str]:
    """Return the correlation group a symbol belongs to, or None."""
    for group_name, symbols in CORRELATION_GROUPS.items():
        if symbol in symbols:
            return group_name
    return None


@dataclass
class DailyStats:
    """Tracks daily-level risk metrics that reset at midnight UTC."""
    date: date = field(default_factory=lambda: datetime.utcnow().date())
    pnl: float = 0.0
    trades_opened: int = 0
    trades_closed: int = 0
    wins: int = 0
    losses: int = 0

    def is_stale(self) -> bool:
        return datetime.utcnow().date() != self.date


class RiskManager:
    """
    Central risk gate that must approve every trade entry and enforces
    position-level and portfolio-level constraints.

    Usage:
        rm = RiskManager(config)
        allowed, reason = rm.check_entry_allowed(signal)
        if allowed:
            size = rm.calculate_position_size(signal, balance)
    """

    def __init__(self, config: Dict[str, Any]):
        """
        Parameters
        ----------
        config : dict
            The full bot configuration (parsed settings.yaml).
            Must contain 'risk', 'risk.safety', and optionally
            'risk.stop_loss', 'risk.trailing'.
        """
        risk_cfg = config.get("risk", {})
        safety_cfg = risk_cfg.get("safety", {})

        # --- Position sizing ---
        self.risk_per_trade_pct: float = risk_cfg.get("risk_per_trade_pct", 1.0)
        self.max_position_size_usd: float = risk_cfg.get("max_position_size_usd", 10_000)
        self.default_leverage: int = risk_cfg.get("default_leverage", 5)
        self.max_leverage: int = risk_cfg.get("max_leverage", 20)

        # --- Portfolio limits ---
        self.max_daily_loss_pct: float = risk_cfg.get("max_daily_loss_pct", 3.0)
        self.max_open_positions: int = risk_cfg.get("max_open_positions", 3)
        self.max_exposure_per_symbol_pct: float = risk_cfg.get("max_exposure_per_symbol_pct", 50.0)
        self.max_correlated_exposure_pct: float = risk_cfg.get("max_correlated_exposure_pct", 100.0)

        # --- Safety mechanisms ---
        self.circuit_breaker_losses: int = safety_cfg.get("circuit_breaker_losses", 3)
        self.circuit_breaker_cooldown: int = safety_cfg.get("circuit_breaker_cooldown", 3600)
        self.volatility_kill_switch: bool = safety_cfg.get("volatility_kill_switch", True)
        self.volatility_kill_atr_mult: float = safety_cfg.get("volatility_kill_atr_multiplier", 3.0)
        self.cooloff_after_sl: int = safety_cfg.get("cooloff_after_sl", 180)

        # --- Stop-loss config (used for position sizing) ---
        sl_cfg = risk_cfg.get("stop_loss", {})
        self.sl_type: str = sl_cfg.get("type", "atr")
        self.sl_fixed_pct: float = sl_cfg.get("fixed_pct", 1.5)
        self.sl_atr_multiplier: float = sl_cfg.get("atr_multiplier", 1.5)

        # --- Runtime state ---
        self.daily: DailyStats = DailyStats()
        self.consecutive_losses: int = 0
        self.circuit_breaker_until: float = 0.0  # unix timestamp

        # symbol -> notional USD currently exposed
        self.open_positions: Dict[str, float] = {}
        # symbol -> side ("long" / "short") for correlation guard
        self.open_position_sides: Dict[str, str] = {}
        # symbol -> unix timestamp when SL was last hit
        self.last_sl_time: Dict[str, float] = {}

        # Running account balance (set externally)
        self.account_balance: float = 0.0

        logger.info(
            "RiskManager initialised: risk/trade=%.1f%%, max_pos=%d, "
            "daily_loss=%.1f%%, circuit_breaker=%d consecutive losses",
            self.risk_per_trade_pct,
            self.max_open_positions,
            self.max_daily_loss_pct,
            self.circuit_breaker_losses,
        )

    # ==================================================================
    # Public API — Pre-trade checks
    # ==================================================================

    def check_entry_allowed(self, signal: Dict[str, Any]) -> Tuple[bool, str]:
        """
        Run all risk rules against a proposed entry signal.

        Parameters
        ----------
        signal : dict
            Must contain at least 'symbol'. May also contain 'atr',
            'avg_atr', 'confidence', 'grade'.

        Returns
        -------
        (allowed, reason) : (bool, str)
            True with empty reason if all checks pass;
            False with human-readable rejection reason otherwise.
        """
        self._maybe_reset_daily()

        symbol = signal.get("symbol", "")

        # 1. Circuit breaker
        if self.check_circuit_breaker():
            remaining = max(0, self.circuit_breaker_until - time.time())
            return False, (
                f"Circuit breaker active: {self.consecutive_losses} consecutive losses. "
                f"Cooldown remaining: {remaining:.0f}s"
            )

        # 2. Daily loss limit
        if self.check_daily_loss():
            return False, (
                f"Daily loss limit hit: {self.daily.pnl:.2f} "
                f"(limit: -{self.max_daily_loss_pct:.1f}% of {self.account_balance:.2f})"
            )

        # 3. Max open positions
        if self.check_max_positions():
            return False, (
                f"Max open positions reached: {len(self.open_positions)}/{self.max_open_positions}"
            )

        # 4. Symbol exposure
        if self.check_max_exposure(symbol):
            current = self.open_positions.get(symbol, 0.0)
            return False, (
                f"Max exposure for {symbol}: ${current:.2f} "
                f"(limit: {self.max_exposure_per_symbol_pct:.1f}% of balance)"
            )

        # 5. Correlated exposure
        if self.check_correlated_exposure(symbol):
            group = _symbol_group(symbol)
            return False, (
                f"Correlated exposure limit hit for group '{group}' "
                f"(limit: {self.max_correlated_exposure_pct:.1f}% of balance)"
            )

        # 6. Post-SL cooldown
        if self.check_cooldown(symbol):
            elapsed = time.time() - self.last_sl_time.get(symbol, 0)
            remaining = max(0, self.cooloff_after_sl - elapsed)
            return False, (
                f"Cooldown active for {symbol} after stop loss. "
                f"Remaining: {remaining:.0f}s"
            )

        # 7. Volatility kill switch
        atr = signal.get("atr")
        avg_atr = signal.get("avg_atr")
        if atr is not None and avg_atr is not None:
            if self.check_volatility_kill_switch(atr, avg_atr):
                return False, (
                    f"Volatility kill switch: ATR {atr:.4f} is "
                    f"{atr / avg_atr:.1f}x average (limit: {self.volatility_kill_atr_mult:.1f}x)"
                )

        # 8. Correlation guard: don't open same-direction BTC + ETH simultaneously
        side = signal.get("side", "")
        if self.check_correlation_guard(symbol, side):
            return False, (
                f"Correlation guard: same-direction {side} already open in correlated group"
            )

        # 9. Graduated drawdown defense
        dd_check, dd_reason = self.check_drawdown_defense(signal)
        if not dd_check:
            return False, dd_reason

        return True, ""

    # ==================================================================
    # Individual risk checks
    # ==================================================================

    def check_daily_loss(self) -> bool:
        """Return True if the daily loss limit has been breached."""
        self._maybe_reset_daily()
        if self.account_balance <= 0:
            return False
        max_loss = self.account_balance * (self.max_daily_loss_pct / 100.0)
        return self.daily.pnl <= -max_loss

    def check_max_positions(self) -> bool:
        """Return True if we are at the maximum number of open positions."""
        return len(self.open_positions) >= self.max_open_positions

    def check_max_exposure(self, symbol: str) -> bool:
        """Return True if exposure for *symbol* already exceeds the per-symbol cap."""
        if self.account_balance <= 0:
            return False
        current = self.open_positions.get(symbol, 0.0)
        limit = self.account_balance * (self.max_exposure_per_symbol_pct / 100.0)
        return current >= limit

    def check_correlated_exposure(self, symbol: str = "") -> bool:
        """
        Return True if opening a new position in *symbol* would exceed the
        correlated exposure limit for its group.

        If symbol has no known group, this check always passes.
        """
        group = _symbol_group(symbol)
        if group is None:
            return False
        if self.account_balance <= 0:
            return False

        group_symbols = CORRELATION_GROUPS.get(group, [])
        total_exposure = sum(
            self.open_positions.get(s, 0.0) for s in group_symbols
        )
        limit = self.account_balance * (self.max_correlated_exposure_pct / 100.0)
        return total_exposure >= limit

    def check_circuit_breaker(self) -> bool:
        """
        Return True if the circuit breaker is active (consecutive losses
        have exceeded the threshold and cooldown has not elapsed).
        """
        if self.consecutive_losses < self.circuit_breaker_losses:
            return False
        return time.time() < self.circuit_breaker_until

    def check_cooldown(self, symbol: str) -> bool:
        """
        Return True if *symbol* is still in its post-stop-loss cooldown window.
        """
        last_sl = self.last_sl_time.get(symbol, 0.0)
        if last_sl == 0.0:
            return False
        return (time.time() - last_sl) < self.cooloff_after_sl

    def check_correlation_guard(self, symbol: str, side: str) -> bool:
        """
        Return True if a correlated symbol already has an open position
        in the same direction.  Prevents e.g. BTC LONG + ETH LONG at the
        same time (highly correlated, doubles drawdown risk).
        """
        if not side:
            return False
        group = _symbol_group(symbol)
        if group is None:
            return False
        group_symbols = CORRELATION_GROUPS.get(group, [])
        for s in group_symbols:
            if s == symbol:
                continue
            if s in self.open_positions and self.open_position_sides.get(s) == side:
                return True
        return False

    def check_drawdown_defense(self, signal: Dict[str, Any]) -> Tuple[bool, str]:
        """
        Graduated drawdown defense — layered response instead of binary pause.

        Levels (based on daily PnL as % of account balance):
          DD > 2%: Reduce max leverage to 3x
          DD > 4%: Only allow A/A+ grade trades (confidence ≥ 80)
          DD > 6%: Reduce stake by 50%
          DD > 8%: Full pause — no new trades

        Returns (allowed, reason) tuple.
        """
        self._maybe_reset_daily()
        if self.account_balance <= 0:
            return True, ""

        dd_pct = abs(min(0, self.daily.pnl)) / self.account_balance * 100

        if dd_pct >= 8.0:
            return False, (
                f"DRAWDOWN DEFENSE L4: Daily DD {dd_pct:.1f}% ≥ 8% — "
                f"FULL PAUSE. Daily PnL: ${self.daily.pnl:.2f}"
            )

        if dd_pct >= 4.0:
            confidence = signal.get("confidence", 0)
            if confidence < 80:
                return False, (
                    f"DRAWDOWN DEFENSE L2: Daily DD {dd_pct:.1f}% ≥ 4% — "
                    f"only A/A+ trades allowed (conf≥80), got {confidence}"
                )

        # DD > 2% and DD > 6% affect leverage/stake (handled in signal_tracker)
        # but still allow the trade — just with reduced sizing
        # Store DD level for downstream use
        self._current_dd_pct = dd_pct

        return True, ""

    @property
    def current_dd_pct(self) -> float:
        """Current daily drawdown as % of account balance."""
        return getattr(self, '_current_dd_pct', 0.0)

    def check_volatility_kill_switch(self, atr: float, avg_atr: float) -> bool:
        """
        Return True if current ATR exceeds the kill-switch threshold
        relative to the rolling average ATR.
        """
        if not self.volatility_kill_switch:
            return False
        if avg_atr <= 0:
            return False
        return atr > (avg_atr * self.volatility_kill_atr_mult)

    # ==================================================================
    # Position sizing
    # ==================================================================

    def calculate_position_size(
        self,
        signal: Dict[str, Any],
        balance: float,
        current_price: Optional[float] = None,
    ) -> float:
        """
        Calculate position size in base currency using the risk-per-trade method.

        Position size = (balance * risk_pct) / (stop_distance * leverage_adj)

        Parameters
        ----------
        signal : dict
            Must contain 'entry_price' (or uses current_price fallback),
            and either 'stop_loss' or 'atr' for stop distance calculation.
        balance : float
            Current account balance in quote currency (e.g. USDT).
        current_price : float, optional
            Fallback price if signal has no 'entry_price'.

        Returns
        -------
        float
            Position size in base currency (e.g. amount of BTC to buy).
            Returns 0.0 if inputs are invalid.
        """
        entry_price = signal.get("entry_price") or current_price
        if not entry_price or entry_price <= 0:
            logger.warning("Cannot size position: no entry price")
            return 0.0

        # Determine stop distance
        stop_price = signal.get("stop_loss", 0.0)
        if stop_price and stop_price > 0:
            stop_distance = abs(entry_price - stop_price)
        else:
            atr = signal.get("atr", 0.0)
            if atr > 0:
                stop_distance = atr * self.sl_atr_multiplier
            else:
                stop_distance = entry_price * (self.sl_fixed_pct / 100.0)

        if stop_distance <= 0:
            logger.warning("Cannot size position: stop distance is zero")
            return 0.0

        # Risk amount in quote currency
        risk_amount = balance * (self.risk_per_trade_pct / 100.0)

        # Leverage
        leverage = signal.get("leverage", self.default_leverage)
        leverage = min(leverage, self.max_leverage)
        leverage = max(leverage, 1)

        # Position size in base currency
        # risk_amount = stop_distance * position_size (for 1x leverage)
        # With leverage, our margin is position_value / leverage, but risk is the same
        position_size = risk_amount / stop_distance

        # Cap by max position size in USD
        position_value_usd = position_size * entry_price
        if position_value_usd > self.max_position_size_usd:
            position_size = self.max_position_size_usd / entry_price
            position_value_usd = self.max_position_size_usd

        # Cap by remaining symbol exposure
        if self.account_balance > 0:
            symbol = signal.get("symbol", "")
            existing = self.open_positions.get(symbol, 0.0)
            max_symbol_usd = self.account_balance * (self.max_exposure_per_symbol_pct / 100.0)
            available = max(0, max_symbol_usd - existing)
            if position_value_usd > available:
                position_size = available / entry_price
                position_value_usd = available

        # Ensure margin doesn't exceed balance
        required_margin = position_value_usd / leverage
        if required_margin > balance:
            position_size = (balance * leverage) / entry_price

        logger.info(
            "Position sized: %.6f %s (notional $%.2f, risk $%.2f, SL dist %.4f, lev %dx)",
            position_size,
            signal.get("symbol", "?"),
            position_size * entry_price,
            risk_amount,
            stop_distance,
            leverage,
        )

        return round(position_size, 8)

    # ==================================================================
    # State mutation — called by execution engine
    # ==================================================================

    def register_position(self, symbol: str, notional_usd: float, side: str = "") -> None:
        """Track a newly opened position."""
        self.open_positions[symbol] = self.open_positions.get(symbol, 0.0) + notional_usd
        if side:
            self.open_position_sides[symbol] = side
        self.daily.trades_opened += 1
        logger.debug(
            "Position registered: %s %s +$%.2f (total: $%.2f)",
            symbol, side, notional_usd, self.open_positions[symbol],
        )

    def unregister_position(self, symbol: str, notional_usd: float) -> None:
        """Remove or reduce exposure tracking for a closed / partially closed position."""
        current = self.open_positions.get(symbol, 0.0)
        remaining = current - notional_usd
        if remaining <= 0.01:  # float tolerance
            self.open_positions.pop(symbol, None)
            self.open_position_sides.pop(symbol, None)
        else:
            self.open_positions[symbol] = remaining
        logger.debug(
            "Position unregistered: %s -$%.2f (remaining: $%.2f)",
            symbol, notional_usd, self.open_positions.get(symbol, 0.0),
        )

    def record_trade_result(self, pnl: float, is_partial_exit: bool = False) -> None:
        """
        Record a trade result for daily tracking and circuit breaker.
        Call this every time a trade (or partial) closes.

        Parameters
        ----------
        pnl : float
            Profit/loss amount.
        is_partial_exit : bool
            If True, this is a partial TP exit — negative PnL from partial
            exits (e.g. TP1 booked but trailing portion stopped out for less)
            should NOT count toward consecutive losses / circuit breaker.
        """
        self._maybe_reset_daily()
        self.daily.pnl += pnl
        self.daily.trades_closed += 1

        if pnl >= 0:
            self.daily.wins += 1
            self.consecutive_losses = 0
        else:
            if is_partial_exit:
                # Partial exit with negative PnL (e.g. trailing portion stopped)
                # Don't count as consecutive loss — TP1 was already booked
                self.daily.losses += 1
                # Reset streak — the trade was still profitable overall
                self.consecutive_losses = 0
            else:
                self.daily.losses += 1
                self.consecutive_losses += 1

        # Trigger circuit breaker (only on full losses, not partials)
        if self.consecutive_losses >= self.circuit_breaker_losses:
            self.circuit_breaker_until = time.time() + self.circuit_breaker_cooldown
            logger.warning(
                "CIRCUIT BREAKER TRIGGERED: %d consecutive losses. "
                "Trading paused for %ds",
                self.consecutive_losses, self.circuit_breaker_cooldown,
            )

        logger.info(
            "Trade result recorded: PnL=%.2f, daily=%.2f, "
            "consecutive_losses=%d",
            pnl, self.daily.pnl, self.consecutive_losses,
        )

    def record_sl_hit(self, symbol: str) -> None:
        """Start a cooloff timer for *symbol* after a stop loss is hit."""
        self.last_sl_time[symbol] = time.time()
        logger.info(
            "Stop loss cooldown started for %s (%ds)",
            symbol, self.cooloff_after_sl,
        )

    def set_account_balance(self, balance: float) -> None:
        """Update the reference balance used for percentage calculations."""
        self.account_balance = balance

    # ==================================================================
    # Daily reset
    # ==================================================================

    def reset_daily(self) -> None:
        """Manually reset daily counters (also happens automatically at midnight UTC)."""
        prev = self.daily
        logger.info(
            "Daily risk stats reset. Previous day: PnL=%.2f, W/L=%d/%d",
            prev.pnl, prev.wins, prev.losses,
        )
        self.daily = DailyStats()

    def _maybe_reset_daily(self) -> None:
        """Auto-reset if the date has rolled over."""
        if self.daily.is_stale():
            self.reset_daily()

    # ==================================================================
    # Introspection
    # ==================================================================

    def get_status(self) -> Dict[str, Any]:
        """Return a snapshot of the current risk state for dashboards / logging."""
        self._maybe_reset_daily()
        total_exposure = sum(self.open_positions.values())
        return {
            "account_balance": self.account_balance,
            "daily_pnl": self.daily.pnl,
            "daily_trades": self.daily.trades_closed,
            "daily_win_rate": (
                (self.daily.wins / self.daily.trades_closed * 100)
                if self.daily.trades_closed > 0
                else 0.0
            ),
            "open_positions": len(self.open_positions),
            "total_exposure_usd": total_exposure,
            "consecutive_losses": self.consecutive_losses,
            "circuit_breaker_active": self.check_circuit_breaker(),
            "circuit_breaker_until": (
                datetime.utcfromtimestamp(self.circuit_breaker_until).isoformat()
                if self.circuit_breaker_until > 0
                else None
            ),
            "positions": dict(self.open_positions),
            "cooldowns": {
                sym: self.cooloff_after_sl - (time.time() - ts)
                for sym, ts in self.last_sl_time.items()
                if (time.time() - ts) < self.cooloff_after_sl
            },
        }

    def __repr__(self) -> str:
        return (
            f"RiskManager(positions={len(self.open_positions)}, "
            f"daily_pnl={self.daily.pnl:.2f}, "
            f"consec_losses={self.consecutive_losses})"
        )
