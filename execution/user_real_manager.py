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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("execution.user_real")


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
        self.ml_threshold = float(user_config.get("ml_threshold", 0.60))
        self.size_multiplier = float(user_config.get("size_multiplier", 1.0))
        self.max_daily_trades = int(user_config.get("max_daily_trades", 15))
        self.enabled = user_config.get("bot_mode", "paper") != "paper"

        # Circuit breaker
        self.cb = UserCircuitBreaker(daily_loss_limit=self.max_daily_loss)

        # Trade tracking
        self.open_trades: Dict[str, UserTradeRecord] = {}
        self.closed_trades: List[Dict] = []
        self._cached_balance: float = 0.0

        # Price feed reference (set by registry from orchestrator)
        self._price_feed = None

        logger.info(
            "UserRealManager created: user=%s email=%s | lev=%dx loss=$%.0f symbols=%s mode=%s",
            user_id[:8], user_email, self.max_leverage, self.max_daily_loss,
            len(self.preferred_symbols), "ENABLED" if self.enabled else "PAPER",
        )

    # ══════════════════════════════════════════════════════════════
    # QUALIFICATION (user-specific gates)
    # ══════════════════════════════════════════════════════════════

    def qualify_signal(self, signal: dict) -> Tuple[bool, str]:
        """Check if this signal should fire a real trade for THIS user."""
        symbol = signal.get("symbol", "")
        meta = signal.get("metadata", {}) or {}

        # 1. User enabled?
        if not self.enabled:
            return False, "user_disabled"

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

        # 5. Confidence floor
        conf = signal.get("confidence", 0)
        if conf < self.min_confidence:
            return False, f"user_conf_floor:{conf}<{self.min_confidence}"

        # 6. Grade filter
        grade = signal.get("grade", "C")
        if grade not in ("A+", "A", "B"):
            return False, f"user_grade_filter:{grade}"

        # 7. ML probability floor
        ml_prob = meta.get("ml_probability")
        if ml_prob is not None and float(ml_prob) > 0 and float(ml_prob) < self.ml_threshold:
            return False, f"user_ml_floor:{ml_prob:.3f}<{self.ml_threshold:.2f}"

        # 8. Max open positions
        if len(self.open_trades) >= 3:
            return False, f"user_max_open:{len(self.open_trades)}"

        # 9. Duplicate symbol
        for t in self.open_trades.values():
            if t.symbol == symbol:
                return False, f"user_duplicate:{symbol}"

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

        # Get balance
        balance = self._cached_balance or 100.0
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

        # Apply user's size multiplier
        margin = base_margin * self.size_multiplier
        margin = max(15, min(margin, self.max_position_notional / self.max_leverage))

        # Leverage (capped by user's max)
        leverage = min(self.max_leverage, 20)

        # Lots
        notional = margin * leverage
        if entry_price > 0:
            lots = max(1, int(notional / entry_price))
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

        # 1. Qualify
        qualified, reason = self.qualify_signal(signal)
        if not qualified:
            logger.debug("USER %s: skip %s — %s", self.user_id[:8], symbol, reason)
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
            # Connect to user's exchange
            if hasattr(self._delta, 'connect'):
                self._delta.connect()

            # Place IOC limit order
            from exchange.delta_client import PRODUCT_MAP
            product_id = PRODUCT_MAP.get(symbol)
            if not product_id:
                logger.warning("USER %s: unknown product for %s", self.user_id[:8], symbol)
                return None

            # Set leverage
            try:
                self._delta._client.set_leverage({
                    "product_id": product_id,
                    "leverage": str(leverage),
                })
            except Exception:
                pass

            # Place order
            slippage_bps = 15
            if entry_price > 0:
                slip = entry_price * slippage_bps / 10000
                limit_price = entry_price + slip if order_side == "buy" else entry_price - slip
            else:
                limit_price = 0

            order_params = {
                "product_id": product_id,
                "size": lots,
                "side": order_side,
                "order_type": "limit_order",
                "limit_price": str(round(limit_price, 2)) if limit_price > 0 else None,
                "time_in_force": "ioc",
                "reduce_only": "false",
            }

            result = self._delta._client.create_order(order_params)

            # Check fill
            fill_price = 0
            if result and isinstance(result, dict):
                fill_price = float(result.get("average_fill_price", 0) or 0)
                if fill_price <= 0:
                    fill_price = float(result.get("price", 0) or 0)

            if fill_price <= 0:
                logger.info("USER %s: %s not filled", self.user_id[:8], symbol)
                return None

            # 4. Recalculate SL from fill price
            if fill_price != entry_price and entry_price > 0:
                sl_shift = fill_price - entry_price
                sl = sl + sl_shift

            # 5. Record trade
            trade_id = f"u_{self.user_id[:8]}_{int(time.time())}"
            initial_risk = abs(fill_price - sl) if sl > 0 else fill_price * 0.01

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
                scanner=meta.get("scanner", ""),
                grade=signal.get("grade", ""),
                trade_type=meta.get("trade_type", "SCALP"),
            )
            self.open_trades[trade_id] = trade

            logger.warning(
                "USER REAL ENTRY: %s %s %s | fill=%.4f sl=%.4f | margin=$%.2f lots=%d lev=%dx",
                self.user_email, symbol, side_str, fill_price, sl, margin, lots, leverage,
            )

            # 6. Record to DB
            if self._db_pool:
                try:
                    await self._record_trade_db(trade, "open")
                except Exception as e:
                    logger.error("USER %s: DB record failed: %s", self.user_id[:8], e)

            # 7. Start independent monitoring
            asyncio.create_task(self._monitor_trade(trade_id))

            return {"trade_id": trade_id, "fill_price": fill_price, "symbol": symbol}

        except Exception as e:
            logger.error("USER %s: execution failed for %s: %s", self.user_id[:8], symbol, e)
            return None

    # ══════════════════════════════════════════════════════════════
    # MONITORING (per-trade, independent from paper)
    # ══════════════════════════════════════════════════════════════

    async def _monitor_trade(self, trade_id: str):
        """Independent 500ms monitoring loop for a user's real trade.

        Checks SL, trail, time decay, early kill — same logic as global
        real manager but using THIS user's settings.
        """
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
                    await asyncio.sleep(1)
                    continue

                side = trade.side.lower()
                entry = trade.entry_price
                sl = trade.stop_loss
                risk = trade.initial_risk or abs(entry - sl) or entry * 0.01

                # Current R
                if side == "long":
                    current_r = (price - entry) / risk if risk > 0 else 0
                else:
                    current_r = (entry - price) / risk if risk > 0 else 0

                # Update MFE
                if current_r > trade.peak_mfe_r:
                    trade.peak_mfe_r = current_r

                # Trade age
                age_sec = time.time() - trade.opened_at if trade.opened_at > 0 else 0

                # ── EXIT CHECKS ──

                # 1. SL hit
                sl_hit = (side == "long" and price <= sl and sl > 0) or \
                         (side != "long" and price >= sl and sl > 0)
                if sl_hit:
                    await self._close_trade(trade, price, "sl_hit")
                    break

                # 2. Early kill (no favor after 60s)
                if age_sec > 60 and trade.peak_mfe_r < 0.05 and current_r < 0:
                    await self._close_trade(trade, price, "early_kill")
                    break

                # 3. Time decay
                max_age = 1800 if trade.trade_type == "SCALP" else 3600
                if age_sec > max_age:
                    await self._close_trade(trade, price, f"time_decay_{int(age_sec/60)}m")
                    break

                # 4. Trail (MFE-based breakeven + lock tiers)
                if trade.peak_mfe_r >= 0.20 and age_sec > 15:
                    fee_buffer = entry * 0.004
                    if side == "long":
                        be_sl = entry + fee_buffer
                        if be_sl > trade.stop_loss:
                            trade.stop_loss = be_sl
                    else:
                        be_sl = entry - fee_buffer
                        if be_sl < trade.stop_loss:
                            trade.stop_loss = be_sl

                    # Lock tiers
                    lock_pct = 0
                    if trade.peak_mfe_r >= 1.0: lock_pct = 0.55
                    elif trade.peak_mfe_r >= 0.7: lock_pct = 0.50
                    elif trade.peak_mfe_r >= 0.5: lock_pct = 0.45
                    elif trade.peak_mfe_r >= 0.4: lock_pct = 0.40
                    elif trade.peak_mfe_r >= 0.3: lock_pct = 0.75
                    elif trade.peak_mfe_r >= 0.2: lock_pct = 0.60

                    if lock_pct > 0:
                        lock_dist = risk * trade.peak_mfe_r * lock_pct
                        if side == "long":
                            new_sl = entry + lock_dist
                            if new_sl > trade.stop_loss:
                                trade.stop_loss = new_sl
                        else:
                            new_sl = entry - lock_dist
                            if new_sl < trade.stop_loss:
                                trade.stop_loss = new_sl

                await asyncio.sleep(0.5)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("USER %s: monitor error for %s: %s", self.user_id[:8], trade_id, e)

    async def _close_trade(self, trade: UserTradeRecord, exit_price: float, reason: str):
        """Close a user's real trade on their exchange."""
        try:
            from exchange.delta_client import PRODUCT_MAP
            product_id = PRODUCT_MAP.get(trade.symbol)
            close_side = "sell" if trade.side == "long" else "buy"

            # Market close on user's exchange
            try:
                self._delta._client.create_order({
                    "product_id": product_id,
                    "size": trade.position_size,
                    "side": close_side,
                    "order_type": "market_order",
                    "reduce_only": "true",
                })
            except Exception as e:
                logger.error("USER %s: close order failed: %s %s — %s",
                            self.user_id[:8], trade.symbol, reason, e)

            # Calculate PnL
            if trade.side == "long":
                pnl_pct = (exit_price - trade.entry_price) / trade.entry_price if trade.entry_price > 0 else 0
            else:
                pnl_pct = (trade.entry_price - exit_price) / trade.entry_price if trade.entry_price > 0 else 0
            pnl_usd = pnl_pct * trade.margin * trade.leverage

            logger.warning(
                "USER REAL EXIT: %s %s %s | entry=%.4f exit=%.4f | pnl=$%.2f | %s | mfe=%.2fR",
                self.user_email, trade.symbol, trade.side,
                trade.entry_price, exit_price, pnl_usd, reason, trade.peak_mfe_r,
            )

            # Record
            closed = {
                "trade_id": trade.trade_id,
                "user_id": trade.user_id,
                "symbol": trade.symbol,
                "side": trade.side,
                "entry_price": trade.entry_price,
                "exit_price": exit_price,
                "pnl_usd": round(pnl_usd, 4),
                "pnl_pct": round(pnl_pct * 100, 4),
                "exit_reason": reason,
                "peak_mfe_r": round(trade.peak_mfe_r, 4),
                "margin": trade.margin,
                "leverage": trade.leverage,
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

            # Record to DB
            if self._db_pool:
                try:
                    await self._record_trade_db(trade, "closed", exit_price, pnl_usd, reason)
                except Exception as e:
                    logger.error("USER %s: DB close record failed: %s", self.user_id[:8], e)

        except Exception as e:
            logger.error("USER %s: close trade error: %s", self.user_id[:8], e)

    # ══════════════════════════════════════════════════════════════
    # DB PERSISTENCE
    # ══════════════════════════════════════════════════════════════

    async def _record_trade_db(self, trade: UserTradeRecord, status: str,
                                exit_price: float = 0, pnl_usd: float = 0, reason: str = ""):
        """Record trade open/close to user_trades table."""
        if not self._db_pool:
            return
        try:
            async with self._db_pool.acquire() as conn:
                if status == "open":
                    await conn.execute("""
                        INSERT INTO user_trades (id, user_id, trade_type, symbol, side,
                            entry_price, quantity, status, signal_data, metadata)
                        VALUES (gen_random_uuid(), $1, 'real', $2, $3, $4, $5, 'open',
                            $6::jsonb, $7::jsonb)
                    """, self.user_id, trade.symbol, trade.side,
                        trade.entry_price, float(trade.position_size),
                        '{}', f'{{"scanner":"{trade.scanner}","grade":"{trade.grade}","leverage":{trade.leverage}}}')
                else:
                    await conn.execute("""
                        UPDATE user_trades SET status='closed', exit_price=$1, pnl_usd=$2,
                            closed_at=NOW(), metadata=metadata || $3::jsonb
                        WHERE user_id=$4 AND symbol=$5 AND status='open'
                        ORDER BY opened_at DESC LIMIT 1
                    """, exit_price, pnl_usd,
                        f'{{"exit_reason":"{reason}","peak_mfe_r":{trade.peak_mfe_r:.4f}}}',
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
                bal = self._delta.fetch_balance()
                if bal and isinstance(bal, (int, float)):
                    self._cached_balance = float(bal)
        except Exception as e:
            logger.debug("USER %s: balance fetch failed: %s", self.user_id[:8], e)
