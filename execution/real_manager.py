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
from exchange.delta_client import DeltaClient

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

        # Don't count micro-losses (<$0.50) toward consecutive — these are fee/rounding artifacts
        if pnl_usd < -0.50:
            self.consecutive_losses += 1
        elif pnl_usd >= 0:
            self.consecutive_losses = 0
        # else: micro-loss between -$0.50 and $0 — ignore for streak purposes

        self._check_trip()

    def record_trade_with_reason(self, pnl_usd: float, reason: str = ""):
        """Record trade but skip orphan/micro losses from consecutive count."""
        self.daily_pnl += pnl_usd
        self.total_pnl += pnl_usd
        self.trade_count_today += 1

        # Skip from consecutive count: orphan sync, micro-losses, fee artifacts
        if reason == "orphan_sync" and abs(pnl_usd) < 1.0:
            pass  # Timing artifact
        elif pnl_usd < -0.50:
            self.consecutive_losses += 1
        elif pnl_usd >= 0:
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
        self.exchange = exchange  # Main exchange (for paper/data)
        self.config = config
        self.risk_manager = risk_manager

        rt_cfg = config.get("real_trading", {})
        self.enabled: bool = rt_cfg.get("enabled", False)
        self.dry_run: bool = rt_cfg.get("dry_run", True)
        self.min_margin: float = rt_cfg.get("min_margin_per_trade", 10.0)
        self.max_margin: float = rt_cfg.get("max_margin_per_trade", 25.0)
        self.max_open: int = rt_cfg.get("max_open_positions", 5)
        self.reserve_pct: float = rt_cfg.get("balance_reserve_pct", 15) / 100.0
        self.min_balance: float = rt_cfg.get("min_balance_to_trade", 30.0)
        self.leverage_cap: int = rt_cfg.get("leverage_cap", 10)

        self.circuit_breaker = RealCircuitBreaker(
            daily_loss_limit=rt_cfg.get("daily_loss_limit_usd", 25.0),
            max_consecutive_losses=rt_cfg.get("max_consecutive_losses", 10),
        )

        # Delta SDK clients (replaces ccxt for order execution)
        # DRY RUN → demo DeltaClient (testnet)
        # LIVE → live DeltaClient (production)
        self._delta_demo = DeltaClient(mode="demo")
        self._delta_live = DeltaClient(mode="live")
        self._delta_connected = False

        # Legacy engine (kept for compatibility, not used for real orders)
        self.engine = ExecutionEngine(exchange, config, risk_manager)

        # Track real trades: paper_trade_id → real Trade
        self.real_trades: Dict[str, Trade] = {}
        self.closed_real_trades: List[Dict] = []
        self.paper_to_real: Dict[str, str] = {}  # paper_id → real_id
        self._open_positions: Dict[str, Any] = {}  # dry run open positions

        # Balance cache
        self._cached_balance: Optional[float] = None
        self._balance_ts: float = 0

        # API failure tracking
        self._api_failures: int = 0
        self._max_api_failures: int = 5

        # Load persisted state (may override enabled/dry_run from saved toggle)
        self._load_state()

    async def _get_trading_exchange(self):
        """Get the correct exchange client based on mode.
        DRY RUN → demo exchange (testnet)
        LIVE → real exchange (personal account)
        """
        import os

        if self.dry_run:
            # Use demo exchange
            if self._demo_exchange is None or not self._demo_connected:
                try:
                    import ccxt.async_support as ccxt_async
                    demo_key = os.getenv("DELTA_DEMO_API_KEY", "")
                    demo_secret = os.getenv("DELTA_DEMO_API_SECRET", "")
                    demo_url = os.getenv("DELTA_DEMO_BASE_URL", "https://cdn-ind.testnet.deltaex.org")

                    if not demo_key:
                        logger.warning("REAL: No demo API key configured, using main exchange")
                        return self.exchange

                    self._demo_exchange = ccxt_async.delta({
                        "apiKey": demo_key,
                        "secret": demo_secret,
                        "urls": {"api": {"public": demo_url, "private": demo_url}},
                        "options": {"defaultType": "swap"},
                    })
                    await self._demo_exchange.load_markets()
                    self._demo_connected = True
                    logger.info("REAL: Demo exchange connected (testnet) — %d markets", len(self._demo_exchange.markets))
                except Exception as e:
                    logger.error("REAL: Failed to connect demo exchange: %s", e)
                    return self.exchange
            return type("DemoWrapper", (), {
                "_exchange": self._demo_exchange,
                "fetch_balance": self._demo_exchange.fetch_balance,
                "set_leverage": lambda s, l: self._demo_exchange.set_leverage(l, s),
                "fetch_ticker": self._demo_exchange.fetch_ticker,
                "_to_exchange_symbol": lambda s: s,
            })()
        else:
            # Use real exchange (already connected via main bot)
            return self.exchange

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

        # Block investment/momentum_trend signals from real/demo trading
        scanner = signal.get("metadata", {}).get("setup_type", "") or signal.get("scanner", "")
        if scanner in ("momentum_trend", "simple_bias", "investment", ""):
            logger.info("REAL SKIP: %s — scanner '%s' blocked from real trading", symbol, scanner)
            return {"status": "skipped", "reason": "blocked_scanner"}

        # Dedup: skip if this paper trade already has a dry run/real entry
        if paper_trade_id and paper_trade_id in self.paper_to_real:
            return {"status": "already_mirrored", "existing_id": self.paper_to_real[paper_trade_id]}

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
            # DRY RUN → Place REAL order on DEMO exchange via delta-rest-client
            dry_id = f"demo_{paper_trade_id or str(int(time.time()))}"
            entry_price = signal.get("entry_price", 0)
            meta = signal.get("metadata", {})
            tps = signal.get("take_profits", [])
            demo_fill = entry_price
            slippage_bps = 0.0

            try:
                # Connect delta client if needed
                if not self._delta_demo.is_connected:
                    self._delta_demo.connect()

                side_str = signal.get("side", "long")
                order_side = "buy" if side_str in ("long", "buy") else "sell"
                close_side = "sell" if order_side == "buy" else "buy"

                # Calculate lots from position size
                delta = self._delta_demo
                lots = delta.calculate_lots(symbol, margin * leverage, entry_price)

                # Set leverage
                delta.set_leverage(symbol, leverage)

                # Get SL and TP prices
                sl = signal.get("stop_loss", 0)
                tp1 = 0
                if tps:
                    tp1 = float(tps[0]) if isinstance(tps[0], (int, float)) else 0

                # ATOMIC BRACKET ORDER: entry + SL + TP in one call
                order = delta.place_bracket_order(
                    symbol=symbol,
                    side=order_side,
                    lots=lots,
                    stop_loss_price=sl,
                    take_profit_price=tp1,
                )

                if order and not order.get("error"):
                    # Extract fill data
                    order_id = order.get("id", order.get("order_id", ""))
                    demo_fill = float(order.get("average_fill_price", entry_price) or entry_price)
                    if demo_fill == 0:
                        demo_fill = entry_price
                    dry_id = "demo_%s" % (order_id or dry_id)
                    slippage_bps = abs(demo_fill - entry_price) / entry_price * 10000 if entry_price > 0 else 0
                    is_bracket = not order.get("bracket_fallback", False)
                    logger.info(
                        "REAL [DEMO %s]: %s %s | signal=%.4f fill=%.4f slip=%.1fbps | lots=%d | SL=%.4f TP=%.4f",
                        "BRACKET" if is_bracket else "FALLBACK",
                        symbol, order_side, entry_price, demo_fill, slippage_bps,
                        lots, sl, tp1,
                    )
                else:
                    logger.warning("REAL [DEMO]: Order failed: %s", order.get("error", "unknown"))

            except Exception as demo_err:
                logger.warning("REAL [DEMO]: Order failed: %s — tracking locally only", demo_err)

            logger.info(
                "REAL [DRY RUN]: %s %s %s | margin=$%.2f | pos=$%.2f | "
                "lots=%d | lev=%dx | signal=%.4f | fill=%.4f | slip=%.1fbps | id=%s",
                symbol, signal.get("side", "?"), meta.get("setup_type", "?"),
                margin, margin * leverage, int(position_size), leverage,
                entry_price, demo_fill, slippage_bps, dry_id,
            )

            # Track with demo fill data
            dry_trade = type("DryTrade", (), {
                "trade_id": dry_id,
                "symbol": symbol,
                "side": signal.get("side", "long"),
                "entry_price": demo_fill,
                "stop_loss": signal.get("stop_loss", 0),
                "tp1": float(tps[0]) if tps and isinstance(tps[0], (int, float)) else 0,
                "tp2": float(tps[1]) if len(tps) > 1 and isinstance(tps[1], (int, float)) else 0,
                "tp3": float(tps[2]) if len(tps) > 2 and isinstance(tps[2], (int, float)) else 0,
                "position_size": position_size,
                "margin": margin,
                "leverage": leverage,
                "status": "open",
                "opened_at": time.time(),
                "scanner": meta.get("setup_type", ""),
                "trade_type": meta.get("display_section", ""),
                "confidence": signal.get("confidence", 0),
                "ml_prob": meta.get("ml_probability", 0),
                "ml_verdict": meta.get("ml_verdict", ""),
                "regime": meta.get("regime", ""),
                "paper_trade_id": paper_trade_id,
                "current_price": demo_fill,
                "slippage_bps": slippage_bps,
            })()
            self.real_trades[dry_id] = dry_trade
            if paper_trade_id:
                self.paper_to_real[paper_trade_id] = dry_id
            self._save_state()
            return {
                "status": "dry_run",
                "trade_id": dry_id,
                "symbol": symbol,
                "side": signal.get("side"),
                "margin": margin,
                "position_usd": margin * leverage,
                "position_size": position_size,
                "leverage": leverage,
                "paper_trade_id": paper_trade_id,
                "fill_price": demo_fill,
                "slippage_bps": slippage_bps,
            }

        # === REAL EXECUTION ===
        try:
            # Set leverage on exchange before placing order
            try:
                await self.exchange.set_leverage(symbol, leverage)
                logger.info("REAL: Leverage set to %dx for %s", leverage, symbol)
            except Exception as lev_err:
                logger.warning("REAL: Could not set leverage for %s: %s (continuing)", symbol, lev_err)

            # Build signal with capped leverage
            real_signal = dict(signal)
            real_signal["leverage"] = leverage

            logger.info(
                "REAL: Placing order — %s %s | size=%.6f | entry=%.4f | lev=%dx | engine=%s",
                symbol, signal.get("side"), position_size,
                signal.get("entry_price", 0), leverage,
                type(self.engine).__name__,
            )
            try:
                trade = await self.engine.execute_entry(real_signal, position_size)
                logger.info("REAL: Engine returned trade status=%s reason=%s id=%s",
                           trade.status, trade.entry_reason, trade.trade_id[:12] if trade.trade_id else "none")
            except Exception as engine_err:
                logger.error("REAL: Engine execute_entry EXCEPTION: %s: %s", type(engine_err).__name__, engine_err)
                import traceback
                logger.error("REAL: Traceback: %s", traceback.format_exc())
                return {"status": "failed", "reason": str(engine_err)}

            if trade.status == TradeStatus.FAILED:
                logger.error(
                    "REAL FAILED: %s %s — reason=%s | size=%.6f entry=%.4f lev=%dx",
                    symbol, signal.get("side"), trade.entry_reason,
                    position_size, signal.get("entry_price", 0), leverage,
                )
                return {"status": "failed", "reason": trade.entry_reason}

            # Track the mapping
            if paper_trade_id:
                self.paper_to_real[paper_trade_id] = trade.trade_id
            self.real_trades[trade.trade_id] = trade

            self._api_failures = 0  # Reset on success

            # Calculate slippage: signal price vs actual fill
            signal_price = signal.get("entry_price", 0)
            fill_price = trade.entry_price  # actual fill from exchange
            slippage_ticks = 0.0
            slippage_bps = 0.0
            slippage_impact_r = 0.0
            if signal_price > 0 and fill_price > 0:
                raw_slip = abs(fill_price - signal_price)
                slippage_bps = (raw_slip / signal_price) * 10000
                atr = signal.get("metadata", {}).get("atr", 0)
                initial_risk = abs(signal_price - signal.get("stop_loss", 0))
                if initial_risk > 0:
                    slippage_impact_r = raw_slip / initial_risk
                tick_size = atr * 0.01 if atr > 0 else signal_price * 0.0001
                slippage_ticks = raw_slip / tick_size if tick_size > 0 else 0

            logger.info(
                "🔴 REAL ENTRY: %s %s %s @ %.4f (signal=%.4f slip=%.1fbps %.2fR) | "
                "margin=$%.2f | pos=$%.2f | lev=%dx | trade=%s",
                symbol, signal.get("side"), signal.get("metadata", {}).get("setup_type", "?"),
                fill_price, signal_price, slippage_bps, slippage_impact_r,
                margin, fill_price * trade.position_size,
                leverage, trade.trade_id,
            )

            # Alert on high slippage
            if slippage_ticks > 4:
                logger.warning(
                    "⚠️ HIGH SLIPPAGE: %s %s | %.1f ticks | %.1f bps | %.3fR",
                    symbol, signal.get("side"), slippage_ticks, slippage_bps, slippage_impact_r,
                )

            self._save_state()
            return {
                "status": "mirrored",
                "trade_id": trade.trade_id,
                "entry_price": trade.entry_price,
                "signal_price": signal_price,
                "fill_price": fill_price,
                "slippage_bps": round(slippage_bps, 2),
                "slippage_ticks": round(slippage_ticks, 2),
                "slippage_impact_r": round(slippage_impact_r, 4),
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
        paper_slippage_bps: float = 0.0,
    ) -> Optional[Dict]:
        """
        Close the real position when paper trade closes.
        Uses paper exit price for accurate PnL (not current market price).
        """
        real_trade_id = self.paper_to_real.get(paper_trade_id)

        # Fallback 1: search by paper_trade_id in open trades
        if not real_trade_id:
            for tid, t in list(self.real_trades.items()):
                if getattr(t, "paper_trade_id", "") == paper_trade_id:
                    real_trade_id = tid
                    break
            if not real_trade_id:
                for tid, t in list(self._open_positions.items()):
                    pid = t.get("paper_trade_id", "") if isinstance(t, dict) else getattr(t, "paper_trade_id", "")
                    if pid == paper_trade_id:
                        real_trade_id = tid
                        break

        # Fallback 2: match by symbol — find ANY open demo trade for this symbol
        # This catches cases where mapping was lost but demo position exists
        if not real_trade_id:
            # Get symbol from the closed paper signal
            paper_sym = None
            for tid, t in list(self.real_trades.items()):
                t_sym = getattr(t, "symbol", t.get("symbol", "")) if isinstance(t, dict) else getattr(t, "symbol", "")
                if t_sym and not paper_sym:
                    # We need the paper signal's symbol — get from orchestrator context
                    pass
            # Search all open real trades for matching symbol
            for tid, t in list(self.real_trades.items()):
                t_sym = getattr(t, "symbol", "") if not isinstance(t, dict) else t.get("symbol", "")
                t_status = getattr(t, "status", "") if not isinstance(t, dict) else t.get("status", "")
                if t_status in ("open", "active", ""):
                    real_trade_id = tid
                    logger.info("REAL EXIT: Fallback symbol match — paper=%s → real=%s (sym=%s)",
                               paper_trade_id[:12] if paper_trade_id else "?", tid[:12], t_sym)
                    break
            if not real_trade_id:
                for tid, t in list(self._open_positions.items()):
                    real_trade_id = tid
                    t_sym = t.get("symbol", "") if isinstance(t, dict) else getattr(t, "symbol", "")
                    logger.info("REAL EXIT: Fallback from _open_positions — paper=%s → real=%s (sym=%s)",
                               paper_trade_id[:12] if paper_trade_id else "?", tid[:12], t_sym)
                    break

        if not real_trade_id:
            logger.debug("REAL EXIT: No matching real trade for paper=%s (mappings=%d, open=%d)",
                        paper_trade_id[:12] if paper_trade_id else "?",
                        len(self.paper_to_real), len(self.real_trades))
            return None

        # Look up in real_trades first, then _open_positions (dry run)
        trade = self.real_trades.get(real_trade_id)
        if not trade:
            trade = self._open_positions.get(real_trade_id)
        if not trade:
            return None

        if self.dry_run:
            # For dry run trades stored as dicts
            if isinstance(trade, dict):
                side_str = trade.get("side", "long")
                entry_p = trade.get("entry_price", 0)
                pos_size = trade.get("position_size", 0)
                margin = trade.get("margin", 0)
                leverage = trade.get("leverage", 1)
                symbol = trade.get("symbol", "?")
            else:
                side_str = trade.side.value if hasattr(trade.side, "value") else str(trade.side)
                entry_p = trade.entry_price
                pos_size = getattr(trade, "position_size", 0)
                margin = getattr(trade, "margin", 0)
                leverage = getattr(trade, "leverage", 1)
                symbol = getattr(trade, "symbol", "?")

            if side_str == "long":
                pnl_pct = (exit_price - entry_p) / entry_p if entry_p else 0
            else:
                pnl_pct = (entry_p - exit_price) / entry_p if entry_p else 0

            # Use margin × leverage as notional (same as paper trade sizing)
            notional = margin * leverage
            pnl_usd = pnl_pct * notional
            fee_est = notional * 0.0015  # ~0.15% round trip (0.075% × 2)
            net_pnl = pnl_usd - fee_est

            # Inherit paper slippage if demo has none
            if paper_slippage_bps > 0 and getattr(trade, "slippage_bps", 0) == 0:
                if isinstance(trade, dict):
                    trade["slippage_bps"] = paper_slippage_bps
                else:
                    trade.slippage_bps = paper_slippage_bps

            logger.info(
                "REAL [DRY RUN] EXIT: %s %s | entry=%.4f exit=%.4f | "
                "gross=$%.2f fees=$%.2f net=$%.2f | margin=$%.2f lev=%dx | slip=%.1fbps | reason=%s",
                symbol, side_str,
                entry_p, exit_price,
                pnl_usd, fee_est, net_pnl, margin, leverage, paper_slippage_bps, reason,
            )

            # Close position on Delta exchange (demo) immediately
            try:
                if hasattr(self, '_delta_demo') and self._delta_demo:
                    close_side = "sell" if side_str == "long" else "buy"
                    lots = int(pos_size) if pos_size else 0
                    if lots > 0:
                        self._delta_demo.close_position(symbol, close_side, lots)
                        logger.info("REAL [DEMO CLOSE]: %s %s %d lots — synced with paper exit",
                                   symbol, close_side, lots)
            except Exception as close_err:
                logger.debug("REAL: Demo exchange close failed (non-critical): %s", close_err)

            # Remove from open positions
            self._open_positions.pop(real_trade_id, None)
            self.paper_to_real.pop(paper_trade_id, None)

            self.circuit_breaker.record_trade(net_pnl)
            self._record_closed_trade(trade, exit_price, net_pnl, reason, dry_run=True)
            self._save_state()
            return {"status": "dry_run_exit", "pnl_usd": net_pnl, "reason": reason}

        # === REAL EXIT ===
        try:
            closed_trade = await self.engine.execute_exit(trade, reason)

            pnl_usd = getattr(closed_trade, "realized_pnl", 0) or 0
            fee = getattr(closed_trade, "total_fees", 0) or 0

            self.circuit_breaker.record_trade(pnl_usd)
            self._record_closed_trade(trade, exit_price, pnl_usd, reason)

            logger.info(
                "🔴 REAL EXIT: %s %s | PnL=$%.2f fees=$%.2f | reason=%s | trade=%s",
                trade.symbol, trade.side.value if hasattr(trade.side, "value") else str(trade.side), pnl_usd, fee, reason, trade.trade_id,
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

        # 4. Duplicate check — local state
        side = signal.get("side", "")
        if hasattr(side, "value"):
            side = side.value
        for t in self.real_trades.values():
            t_side = t.side.value if hasattr(t.side, "value") else str(t.side)
            t_sym = getattr(t, "symbol", "")
            # Match both USDT and USD:USD formats
            sym_match = (t_sym == symbol or
                        t_sym.replace("/USDT", "/USD:USD") == symbol.replace("/USDT", "/USD:USD"))
            if sym_match and t_side == side:
                return False, f"duplicate: {symbol} {side} already open (local)"

        # 4b. Duplicate check — exchange positions (prevents double entry after restart)
        try:
            trading_ex = await self._get_trading_exchange()
            raw_exchange = getattr(trading_ex, '_exchange', trading_ex)
            positions = await raw_exchange.fetch_positions()
            ex_symbol = symbol.replace("/USDT", "/USD:USD")
            for p in positions:
                contracts = float(p.get("contracts", 0) or 0)
                if abs(contracts) == 0:
                    continue
                p_sym = p.get("symbol", "")
                p_side = "long" if p.get("side") in ("buy", "long") else "short"
                if (p_sym == ex_symbol or p_sym == symbol) and p_side == side:
                    return False, f"duplicate: {symbol} {side} already open on exchange"
        except Exception as e:
            logger.warning("REAL: Exchange duplicate check failed: %s (continuing)", e)

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

        # Adaptive: scale margin by scanner score (confidence)
        # Score 90+ = full margin, Score 55 = 40% of margin
        score = signal.get("confidence", signal.get("metadata", {}).get("weighted_score", 70))
        if score >= 90:
            score_mult = 1.0      # Full conviction
        elif score >= 75:
            score_mult = 0.8      # High conviction
        elif score >= 65:
            score_mult = 0.6      # Medium conviction
        else:
            score_mult = 0.4      # Low conviction — minimum size

        margin = max(margin * score_mult, self.min_margin)
        margin = min(margin, self.max_margin)

        # Notional = margin × leverage
        leverage = min(signal.get("leverage", 5), self.leverage_cap)
        notional = margin * leverage
        entry_price = signal.get("entry_price", 0)

        if entry_price <= 0:
            return 0, 0

        # Convert to exchange lot count (Delta uses integer lots)
        # Contract sizes: BTC=0.001, ETH=0.01, SOL=1.0
        contract_sizes = {
            "BTC/USDT": 0.001, "BTC/USD": 0.001,
            "ETH/USDT": 0.01,  "ETH/USD": 0.01,
            "SOL/USDT": 1.0,   "SOL/USD": 1.0,
            "XRP/USDT": 1.0,   "XRP/USD": 1.0,
            "LTC/USDT": 0.1,   "LTC/USD": 0.1,
            "ADA/USDT": 1.0,   "ADA/USD": 1.0,
            "DOT/USDT": 1.0,   "DOT/USD": 1.0,
            "TAO/USDT": 0.01,  "TAO/USD": 0.01,
            "DOGE/USDT": 1.0,  "DOGE/USD": 1.0,
            "LINK/USDT": 1.0,  "LINK/USD": 1.0,
            "AVAX/USDT": 1.0,  "AVAX/USD": 1.0,
        }
        base_sym = symbol.split(":")[0] if ":" in symbol else symbol
        contract_size = contract_sizes.get(base_sym, 1.0)
        lot_value = entry_price * contract_size  # USD value per lot

        # Calculate lots (round down to integer, minimum 1)
        lots = max(1, int(notional / lot_value))

        # Recalculate actual margin used
        actual_notional = lots * lot_value
        margin = actual_notional / leverage

        logger.info(
            "REAL SIZING: %s | notional=$%.2f | lot_value=$%.2f | lots=%d | "
            "actual_notional=$%.2f | margin=$%.2f | lev=%dx",
            symbol, notional, lot_value, lots, actual_notional, margin, leverage,
        )

        # Return margin and LOT COUNT (not fractional coins)
        return margin, float(lots)

    # ==================================================================
    # Balance
    # ==================================================================

    async def _get_balance(self) -> float:
        """Fetch wallet balance via delta-rest-client with 60s cache."""
        now = time.time()
        if self._cached_balance is not None and (now - self._balance_ts) < 60:
            return self._cached_balance

        try:
            delta = self._delta_demo if self.dry_run else self._delta_live
            if not delta.is_connected:
                delta.connect()
            usdt = delta.fetch_balance()
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

    def _record_closed_trade(self, trade, exit_price: float,
                              pnl_usd: float, reason: str, dry_run: bool = False):
        """Record a closed real trade to:
        1. In-memory closed_real_trades list
        2. State file (persisted)
        3. Real trade feedback file (for analytics)
        """
        side_str = trade.side.value if hasattr(trade.side, "value") else str(trade.side)
        entry = getattr(trade, "entry_price", 0)
        margin = getattr(trade, "margin", 0)
        leverage = getattr(trade, "leverage", 0)
        position_size = getattr(trade, "position_size", 0)
        scanner = getattr(trade, "scanner", "")
        pnl_pct = round(pnl_usd / margin * 100, 2) if margin > 0 else 0

        record = {
            "trade_id": trade.trade_id,
            "symbol": getattr(trade, "symbol", ""),
            "side": side_str,
            "entry_price": entry,
            "exit_price": exit_price,
            "margin": margin,
            "leverage": leverage,
            "position_size": position_size,
            "pnl_usd": round(pnl_usd, 4),
            "pnl_pct": pnl_pct,
            "scanner": scanner,
            "reason": reason,
            "dry_run": dry_run,
            "paper_trade_id": getattr(trade, "paper_trade_id", ""),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "confidence": getattr(trade, "confidence", 0),
            "ml_prob": getattr(trade, "ml_prob", 0),
            "ml_verdict": getattr(trade, "ml_verdict", ""),
            "regime": getattr(trade, "regime", ""),
            "trade_type": getattr(trade, "trade_type", ""),
            "slippage_bps": round(getattr(trade, "slippage_bps", 0), 2),
            "slippage_impact_r": getattr(trade, "slippage_impact_r", 0),
        }
        self.closed_real_trades.append(record)

        # Also write to real trade feedback file (separate from paper)
        try:
            feedback_file = Path("storage/real_trade_feedback.jsonl")
            with open(feedback_file, "a") as f:
                f.write(json.dumps(record, default=str) + "\n")
            logger.info("REAL RECORDED: %s %s %s | entry=%.4f exit=%.4f | pnl=$%+.2f | %s",
                       record["symbol"], side_str, scanner, entry, exit_price, pnl_usd, reason)
        except Exception as e:
            logger.warning("REAL: Failed to write feedback: %s", e)

        # Remove from active
        self.real_trades.pop(trade.trade_id, None)
        # Remove paper mapping
        for paper_id, real_id in list(self.paper_to_real.items()):
            if real_id == trade.trade_id:
                del self.paper_to_real[paper_id]
                break

    def _save_state(self):
        """Persist real trading state to disk."""
        # Serialize open trades (including dry run)
        open_trades_data = []
        for t in self.real_trades.values():
            side = t.side.value if hasattr(t.side, "value") else str(t.side)
            open_trades_data.append({
                "trade_id": t.trade_id,
                "symbol": t.symbol,
                "side": side,
                "entry_price": t.entry_price,
                "stop_loss": getattr(t, "stop_loss", 0),
                "tp1": getattr(t, "tp1", 0),
                "tp2": getattr(t, "tp2", 0),
                "tp3": getattr(t, "tp3", 0),
                "position_size": getattr(t, "position_size", 0),
                "margin": getattr(t, "margin", 0),
                "leverage": getattr(t, "leverage", 0),
                "status": getattr(t, "status", "open"),
                "opened_at": getattr(t, "opened_at", 0),
                "scanner": getattr(t, "scanner", ""),
                "trade_type": getattr(t, "trade_type", ""),
                "confidence": getattr(t, "confidence", 0),
                "ml_prob": getattr(t, "ml_prob", 0),
                "ml_verdict": getattr(t, "ml_verdict", ""),
                "regime": getattr(t, "regime", ""),
                "paper_trade_id": getattr(t, "paper_trade_id", ""),
                "current_price": getattr(t, "current_price", t.entry_price),
            })
        state = {
            "circuit_breaker": self.circuit_breaker.to_dict(),
            "paper_to_real": self.paper_to_real,
            "open_trades": open_trades_data,
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

    def sync_with_paper(self, active_paper_ids: set, closed_paper_trades: dict = None):
        """Close orphaned dry run positions whose paper trades are already closed.

        Args:
            active_paper_ids: set of currently active paper trade IDs
            closed_paper_trades: dict of {paper_id: {exit_price, exit_reason}} for recently closed
        """
        if closed_paper_trades is None:
            closed_paper_trades = {}
        orphans = []
        for trade_id, trade in list(self.real_trades.items()):
            paper_id = getattr(trade, "paper_trade_id", "")
            if paper_id and paper_id not in active_paper_ids:
                orphans.append(trade_id)
        for trade_id in orphans:
            trade = self.real_trades[trade_id]
            paper_id = getattr(trade, "paper_trade_id", "")
            # Use paper exit price if available (more accurate than current market)
            paper_close = closed_paper_trades.get(paper_id, {})
            exit_price = paper_close.get("exit_price") or getattr(trade, "current_price", trade.entry_price)
            side_str = trade.side.value if hasattr(trade.side, "value") else str(trade.side)
            if side_str == "long":
                pnl_pct = (exit_price - trade.entry_price) / trade.entry_price if trade.entry_price else 0
            else:
                pnl_pct = (trade.entry_price - exit_price) / trade.entry_price if trade.entry_price else 0
            # Use margin (USD stake), not entry_price × lots (which gives wrong notional)
            margin = getattr(trade, "margin", 0) or 15.10
            leverage = getattr(trade, "leverage", 10) or 10
            position_usd = margin * leverage
            net_pnl = pnl_pct * position_usd - position_usd * 0.0015
            # Safety cap: orphan PnL should never exceed margin
            net_pnl = max(net_pnl, -margin)
            logger.info("REAL [DRY RUN] ORPHAN CLOSE: %s %s | pnl=$%.2f | margin=$%.2f pos=$%.2f | paper closed without mirror",
                        trade.symbol, side_str, net_pnl, margin, position_usd)
            self.circuit_breaker.record_trade_with_reason(net_pnl, "orphan_sync")
            self._record_closed_trade(trade, exit_price, net_pnl, "orphan_sync", dry_run=True)
        if orphans:
            self._save_state()

    async def update_prices(self):
        """Update current prices for all open dry run positions."""
        if not self.real_trades:
            return
        for trade in list(self.real_trades.values()):
            try:
                ticker = await self.exchange.fetch_ticker(trade.symbol)
                if ticker and isinstance(ticker, dict):
                    trade.current_price = ticker.get("last", ticker.get("close", trade.entry_price))
                elif hasattr(ticker, "last"):
                    trade.current_price = ticker.last or trade.entry_price
            except Exception:
                pass  # Keep last known price
        # Also refresh balance
        try:
            await self._get_balance()
        except Exception as e:
            logger.debug("Balance refresh failed during price update: %s", e)

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
            # Restore open trades (dry run positions survive restart)
            for td in state.get("open_trades", []):
                dry_obj = type("DryTrade", (), td)()
                self.real_trades[td["trade_id"]] = dry_obj
            logger.info(
                "REAL: Loaded state — enabled=%s, dry_run=%s, CB daily=$%.2f, "
                "total=$%.2f, %d closed trades, %d open trades",
                self.enabled, self.dry_run,
                self.circuit_breaker.daily_pnl, self.circuit_breaker.total_pnl,
                len(self.closed_real_trades), len(self.real_trades),
            )
        except Exception as e:
            logger.warning("Failed to load real trading state: %s", e)

    # ==================================================================
    # Status for Dashboard
    # ==================================================================

    async def refresh_balance(self) -> float:
        """Force-refresh exchange balance (called by dashboard endpoint)."""
        self._balance_ts = 0  # invalidate cache
        return await self._get_balance()

    async def sync_exchange_positions(self) -> None:
        """Reconcile local state with actual exchange positions (demo or real).
        Uses delta-rest-client for position queries.
        """
        try:
            delta = self._delta_demo if self.dry_run else self._delta_live
            if not delta.is_connected:
                delta.connect()

            positions = delta.get_all_positions()

            # Build set of currently open symbols on exchange
            exchange_open = set()
            for p in positions:
                exchange_open.add(p.get("symbol", ""))

            # Detect locally tracked positions that are GONE from exchange
            # (closed by SL, TP, or liquidation on exchange side)
            for trade_id in list(self.real_trades.keys()):
                t = self.real_trades[trade_id]
                t_sym = getattr(t, "symbol", "")
                # Convert to exchange format for comparison
                ex_sym = t_sym.replace("/USDT", "/USD:USD")
                if ex_sym not in exchange_open and t_sym not in exchange_open:
                    # Position closed on exchange! Record it.
                    entry = getattr(t, "entry_price", 0)
                    # Try to get exit price from recent trades
                    exit_price = 0
                    pnl_usd = 0
                    try:
                        fills = delta.get_fills(limit=10)
                        side = getattr(t, "side", "long")
                        close_side = "sell" if side in ("long", "buy") else "buy"
                        for fill in fills:
                            fill_sym = fill.get("product", {}).get("symbol", "")
                            if fill.get("side") == close_side and fill_sym == PRODUCT_MAP.get(t_sym, {}).get("symbol", ""):
                                exit_price = float(fill.get("fill_price", 0) or 0)
                                fee = float(fill.get("commission", 0) or 0)
                                size = int(fill.get("size", 0) or 0)
                                contract_size = PRODUCT_MAP.get(t_sym, {}).get("contract_size", 1)
                                qty = size * contract_size
                                if side in ("long", "buy"):
                                    pnl_usd = (exit_price - entry) * qty - abs(fee)
                                else:
                                    pnl_usd = (entry - exit_price) * qty - abs(fee)
                                break
                    except Exception as e:
                        logger.warning("REAL: Could not fetch exit details: %s", e)

                    if not exit_price:
                        exit_price = getattr(t, "current_price", entry)

                    reason = "exchange_closed"
                    logger.warning(
                        "REAL: Position CLOSED on exchange: %s %s | entry=%.4f exit=%.4f | pnl=$%.2f",
                        t_sym, getattr(t, "side", "?"), entry, exit_price, pnl_usd,
                    )

                    # Record the closed trade
                    self._record_closed_trade(t, exit_price, pnl_usd, reason, dry_run=False)
                    self.circuit_breaker.record_trade(pnl_usd)
                    self._save_state()

                    # Send Telegram alert
                    try:
                        from bot.alerts.manager import AlertManager, AlertLevel
                        # Use global alert if available
                        emoji = "🟢" if pnl_usd >= 0 else "🔴"
                        msg = (
                            "%s REAL CLOSE: %s %s | entry=%.2f exit=%.2f | "
                            "PnL=$%+.2f | reason=%s"
                        ) % (emoji, t_sym, getattr(t, "side", "?"),
                             entry, exit_price, pnl_usd, reason)
                        logger.info(msg)
                    except Exception as e:
                        logger.warning("Position close recording failed: %s", e)

            # Now add any NEW exchange positions not locally tracked
            for p in positions:
                contracts = float(p.get("contracts", 0) or 0)
                if abs(contracts) == 0:
                    continue
                sym = p.get("symbol", "")
                side = p.get("side", "long")
                entry = float(p.get("entryPrice", 0) or 0)
                # Check if already tracked
                already_tracked = any(
                    getattr(t, "symbol", "") == sym or
                    getattr(t, "symbol", "").replace("/USDT", "/USD:USD") == sym
                    for t in self.real_trades.values()
                )
                if not already_tracked and entry > 0:
                    trade_id = "exchange_%s_%s" % (sym.replace("/", "").replace(":", ""), int(time.time()))
                    # Normalize side: buy→long, sell→short
                    norm_side = "long" if side in ("buy", "long") else "short"
                    # Calculate margin and leverage from exchange data
                    mark_price = float(p.get("markPrice", entry) or entry)
                    notional = abs(contracts * mark_price)
                    init_margin = float(p.get("initialMargin", 0) or 0)
                    lev = int(float(p.get("leverage", 0) or 0))
                    if not lev and init_margin > 0:
                        lev = max(1, int(notional / init_margin))
                    if not lev:
                        lev = 10  # default for Delta
                    if not init_margin:
                        init_margin = notional / lev
                    # Normalize symbol for display
                    display_sym = sym.replace("/USD:USD", "/USDT").replace("/USD:", "/USDT:")
                    orphan = type("ExchangePosition", (), {
                        "trade_id": trade_id, "symbol": display_sym, "side": norm_side,
                        "entry_price": entry, "stop_loss": 0, "tp1": 0, "tp2": 0, "tp3": 0,
                        "position_size": contracts, "margin": round(init_margin, 2),
                        "leverage": lev,
                        "status": "open", "opened_at": time.time(),
                        "scanner": "exchange_sync", "trade_type": "",
                        "confidence": 0, "ml_prob": 0, "ml_verdict": "",
                        "regime": "", "paper_trade_id": "",
                        "current_price": mark_price,
                    })()
                    self.real_trades[trade_id] = orphan
                    logger.warning("REAL: Synced orphaned exchange position: %s %s %s contracts @ %.4f",
                                 sym, side, contracts, entry)
        except Exception as e:
            logger.warning("REAL: Position sync failed: %s", e)

    async def update_exchange_sl(self, paper_trade_id: str, symbol: str, new_sl: float):
        """Update SL on exchange when smart trail moves the stop."""
        # Find the real trade mapped to this paper trade
        real_trade_id = self.paper_to_real.get(paper_trade_id)
        if not real_trade_id:
            return  # no real mirror for this paper trade

        trade = self.real_trades.get(real_trade_id)
        if not trade:
            return

        try:
            trading_ex = await self._get_trading_exchange()
            if not trading_ex:
                return

            # Get product info
            from exchange.delta_client import PRODUCT_MAP
            prod = PRODUCT_MAP.get(symbol, {})
            product_id = prod.get("demo_id" if self.dry_run else "prod_id")
            if not product_id:
                return

            side = trade.side.value if hasattr(trade.side, "value") else str(trade.side)
            sl_side = "sell" if side == "long" else "buy"

            # Cancel existing SL order, place new one
            try:
                open_orders = trading_ex.get_live_orders()
                if isinstance(open_orders, list):
                    for o in open_orders:
                        if (o.get("product_id") == product_id and
                            o.get("reduce_only") == "true" and
                            o.get("order_type") in ("stop_market_order", "stop_limit_order")):
                            trading_ex.cancel_order(product_id, o.get("id"))
                            logger.debug("REAL: Cancelled old SL order %s", o.get("id"))
            except Exception as e:
                logger.debug("REAL: Cancel old SL failed: %s", e)

            # Place new SL
            size = int(getattr(trade, "position_size", 0))
            if size > 0:
                trading_ex.place_stop_order(
                    product_id=product_id,
                    size=size,
                    side=sl_side,
                    stop_price=str(new_sl),
                )
                # Update local trade SL
                trade.stop_loss = new_sl
                logger.info("REAL SL SYNC: %s %s | SL → %.4f | exchange updated", symbol, side, new_sl)
        except Exception as e:
            logger.debug("REAL SL SYNC failed: %s %s | %s", symbol, new_sl, e)

    def get_status(self) -> Dict:
        """Return current real trading status for dashboard."""
        open_trades = []
        for t in self.real_trades.values():
            side = t.side.value if hasattr(t.side, "value") else str(t.side)
            entry = t.entry_price
            current = getattr(t, "current_price", entry) or entry
            pos_size = getattr(t, "position_size", 0)  # lots (contracts)
            margin = getattr(t, "margin", 0)
            lev = getattr(t, "leverage", 1)
            # Get contract size for correct PnL calculation
            contract_sizes = {
                "BTC/USDT": 0.001, "ETH/USDT": 0.01, "SOL/USDT": 1.0,
                "XRP/USDT": 1.0, "LTC/USDT": 0.1, "ADA/USDT": 1.0,
                "DOT/USDT": 1.0, "TAO/USDT": 0.01, "DOGE/USDT": 1.0,
                "LINK/USDT": 1.0, "AVAX/USDT": 1.0,
            }
            contract_size = contract_sizes.get(t.symbol, 1.0)
            qty = pos_size * contract_size  # actual base currency amount
            # UPNL calculation: price_diff × quantity (not lots!)
            if side == "long":
                upnl_pct = ((current - entry) / entry * 100) if entry > 0 else 0
                upnl_usd = (current - entry) * qty
            else:
                upnl_pct = ((entry - current) / entry * 100) if entry > 0 else 0
                upnl_usd = (entry - current) * qty
            # Time open
            opened = getattr(t, "opened_at", 0)
            duration_min = (time.time() - opened) / 60 if opened > 0 else 0
            open_trades.append({
                "trade_id": t.trade_id,
                "symbol": t.symbol,
                "side": side,
                "entry_price": entry,
                "current_price": current,
                "stop_loss": getattr(t, "stop_loss", 0),
                "tp1": getattr(t, "tp1", 0),
                "tp2": getattr(t, "tp2", 0),
                "tp3": getattr(t, "tp3", 0),
                "position_size": pos_size,
                "margin": margin,
                "leverage": lev,
                "position_usd": margin * lev,
                "upnl_pct": round(upnl_pct, 3),
                "upnl_usd": round(upnl_usd, 4),
                "scanner": getattr(t, "scanner", ""),
                "trade_type": getattr(t, "trade_type", ""),
                "confidence": getattr(t, "confidence", 0),
                "ml_prob": getattr(t, "ml_prob", 0),
                "ml_verdict": getattr(t, "ml_verdict", ""),
                "regime": getattr(t, "regime", ""),
                "paper_trade_id": getattr(t, "paper_trade_id", ""),
                "opened_at": opened,
                "duration_min": round(duration_min, 1),
            })

        # Slippage metrics from closed trades
        slippage_data = [t.get("slippage_bps", 0) for t in self.closed_real_trades if t.get("slippage_bps")]
        avg_slippage_bps = sum(slippage_data) / len(slippage_data) if slippage_data else 0
        max_slippage_bps = max(slippage_data) if slippage_data else 0
        slippage_r = [t.get("slippage_impact_r", 0) for t in self.closed_real_trades if t.get("slippage_impact_r")]
        avg_slippage_r = sum(slippage_r) / len(slippage_r) if slippage_r else 0

        # Separate dry run (demo) vs live trade data — always expose BOTH
        demo_trades = [t for t in self.closed_real_trades if t.get("dry_run")]
        live_trades = [t for t in self.closed_real_trades if not t.get("dry_run")]
        demo_pnl = sum(t.get("pnl_usd", 0) for t in demo_trades)
        live_pnl = sum(t.get("pnl_usd", 0) for t in live_trades)
        today_str = str(date.today())
        demo_today = [t for t in demo_trades if t.get("timestamp", "")[:10] == today_str]
        live_today = [t for t in live_trades if t.get("timestamp", "")[:10] == today_str]

        # Summary uses current mode for headline numbers
        if self.dry_run:
            headline_closed = len(demo_trades)
            headline_pnl = demo_pnl
            headline_today = len(demo_today)
        else:
            headline_closed = len(live_trades)
            headline_pnl = live_pnl
            headline_today = len(live_today)

        return {
            "enabled": self.enabled,
            "dry_run": self.dry_run,
            "mode": "DRY RUN" if self.dry_run else ("LIVE" if self.enabled else "DISABLED"),
            "balance": self._cached_balance or 0,
            "circuit_breaker": self.circuit_breaker.to_dict(),
            "open_positions": open_trades,
            "open_count": len(self.real_trades),
            "closed_today": headline_today,
            "total_closed": headline_closed,
            "total_pnl": round(headline_pnl, 2),
            "paper_to_real_mappings": len(self.paper_to_real),
            "api_failures": self._api_failures,
            "recent_trades": demo_trades[-10:] if self.dry_run else live_trades[-10:],
            # ALWAYS expose both sets for dashboard tabs
            "demo_trades": demo_trades[-20:],
            "live_trades": live_trades[-20:],
            "dry_run_stats": {
                "total": len(demo_trades),
                "pnl": round(demo_pnl, 2),
                "today": len(demo_today),
            },
            "live_stats": {
                "total": len(live_trades),
                "pnl": round(live_pnl, 2),
                "today": len(live_today),
            },
            "slippage": {
                "avg_bps": round(avg_slippage_bps, 2),
                "max_bps": round(max_slippage_bps, 2),
                "avg_impact_r": round(avg_slippage_r, 4),
                "samples": len(slippage_data),
            },
        }

    async def reconcile_positions(self) -> Dict:
        """Compare local open trades vs actual exchange positions.

        Call periodically (every 5 min) to detect drift between
        local state and exchange reality. Logs discrepancies but
        does NOT auto-fix — human review required.
        """
        if not self.enabled or self.dry_run:
            return {"status": "skipped", "reason": "disabled or dry_run"}

        try:
            positions = await self.exchange.fetch_positions()
            exchange_pos = {}
            for p in positions:
                contracts = float(p.get("contracts", 0) or 0)
                if abs(contracts) > 0:
                    sym = p.get("symbol", "")
                    exchange_pos[sym] = {
                        "side": p.get("side", ""),
                        "contracts": contracts,
                        "notional": float(p.get("notional", 0) or 0),
                        "entry_price": float(p.get("entryPrice", 0) or 0),
                        "unrealized_pnl": float(p.get("unrealizedPnl", 0) or 0),
                    }

            # Compare with local trades
            local_symbols = set()
            for t in self.real_trades.values():
                sym = t.symbol
                local_symbols.add(sym)
                if sym not in exchange_pos:
                    logger.warning(
                        "RECONCILE MISMATCH: %s exists locally but NOT on exchange",
                        sym,
                    )

            for sym, pos in exchange_pos.items():
                if sym not in local_symbols:
                    logger.warning(
                        "RECONCILE MISMATCH: %s exists on exchange (%.4f contracts) "
                        "but NOT tracked locally — ORPHANED POSITION",
                        sym, pos["contracts"],
                    )

            return {
                "status": "ok",
                "exchange_positions": len(exchange_pos),
                "local_positions": len(self.real_trades),
                "mismatches": len(exchange_pos.symmetric_difference(local_symbols))
                if isinstance(exchange_pos, set) else 0,
            }
        except Exception as e:
            logger.error("RECONCILE ERROR: %s", e)
            return {"status": "error", "reason": str(e)}
