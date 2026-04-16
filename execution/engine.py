"""
Live execution engine for the crypto trading bot.

Translates trading signals into exchange orders, manages the full order
lifecycle (entry, TP/SL management, trailing stops, partial exits), and
handles error recovery (retries, partial fills, rejected orders).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from execution.trade import (
    TakeProfit,
    Trade,
    TradeSide,
    TradeStatus,
)

logger = logging.getLogger("bot.execution.engine")

# Retry configuration
MAX_RETRIES = 3
RETRY_DELAY_BASE = 1.0  # seconds, doubled each retry


class ExecutionError(Exception):
    """Raised when an order cannot be placed after all retries."""


class ExecutionEngine:
    """
    Translates signals into real exchange orders via a ccxt-compatible
    async exchange client.

    Usage:
        engine = ExecutionEngine(exchange_client, config, risk_manager)
        trade = await engine.execute_entry(signal, position_size)
        trade = await engine.execute_exit(trade, "manual")
    """

    def __init__(
        self,
        exchange,
        config: Dict[str, Any],
        risk_manager=None,
    ):
        """
        Parameters
        ----------
        exchange : ccxt.pro async exchange instance
            Must support create_order, cancel_order, fetch_order,
            fetch_positions, fetch_balance.
        config : dict
            Full bot config (parsed settings.yaml).
        risk_manager : RiskManager, optional
            If provided, used to register/unregister positions and
            record trade results.
        """
        self.exchange = exchange
        self.config = config
        self.risk_manager = risk_manager

        risk_cfg = config.get("risk", {})
        self.default_leverage: int = risk_cfg.get("default_leverage", 5)
        self.max_leverage: int = risk_cfg.get("max_leverage", 20)

        trailing_cfg = risk_cfg.get("trailing", {})
        self.trailing_enabled: bool = trailing_cfg.get("enabled", True)
        self.trailing_activation_rr: float = trailing_cfg.get("activation_rr", 1.0)
        self.trailing_pct: float = trailing_cfg.get("trail_pct", 0.5)
        self.break_even_after_tp1: bool = trailing_cfg.get("break_even_after_tp1", True)

        tp_cfg = risk_cfg.get("take_profit", {})
        self.tp_rr_levels: List[float] = [
            tp_cfg.get("tp1_rr", 1.5),
            tp_cfg.get("tp2_rr", 2.5),
            tp_cfg.get("tp3_rr", 4.0),
        ]
        self.tp_close_pcts: List[float] = [
            tp_cfg.get("tp1_close_pct", 40) / 100.0,
            tp_cfg.get("tp2_close_pct", 30) / 100.0,
            tp_cfg.get("tp3_close_pct", 30) / 100.0,
        ]

        self.market_type: str = config.get("exchange", {}).get("market_type", "futures")

        # Active trades keyed by trade_id
        self.active_trades: Dict[str, Trade] = {}

        logger.info(
            "ExecutionEngine initialised: market=%s, leverage=%d, trailing=%s",
            self.market_type, self.default_leverage, self.trailing_enabled,
        )

    # ==================================================================
    # Entry
    # ==================================================================

    async def execute_entry(
        self, signal: Dict[str, Any], position_size: float
    ) -> Trade:
        """
        Place an entry order based on a trading signal.

        Parameters
        ----------
        signal : dict
            Must contain: symbol, side ('long'/'short'), entry_price,
            stop_loss. Optional: take_profits, leverage, confidence,
            grade, timeframe, entry_reason, indicators.
        position_size : float
            Size in base currency (e.g. amount of BTC).

        Returns
        -------
        Trade
            The trade object with status OPEN (filled) or FAILED.
        """
        symbol = signal["symbol"]
        side = TradeSide(signal.get("side", "long"))
        entry_price = signal["entry_price"]
        stop_loss = signal.get("stop_loss", 0.0)
        leverage = min(signal.get("leverage", self.default_leverage), self.max_leverage)

        # Order validation — prevent invalid orders
        import math
        if position_size <= 0 or math.isnan(position_size) or math.isinf(position_size):
            logger.error("ORDER REJECTED: invalid position_size=%.6f for %s", position_size, symbol)
            return Trade(trade_id="", symbol=symbol, side=side, status=TradeStatus.FAILED,
                        entry_reason=f"invalid position_size: {position_size}")
        if entry_price <= 0 or math.isnan(entry_price):
            logger.error("ORDER REJECTED: invalid entry_price=%.4f for %s", entry_price, symbol)
            return Trade(trade_id="", symbol=symbol, side=side, status=TradeStatus.FAILED,
                        entry_reason=f"invalid entry_price: {entry_price}")
        if stop_loss <= 0:
            logger.warning("ORDER WARNING: no stop_loss set for %s", symbol)

        # Build take-profit levels from signal or config defaults
        take_profits = self._build_take_profits(signal, entry_price, stop_loss, side)

        trade = Trade(
            symbol=symbol,
            side=side,
            status=TradeStatus.PENDING,
            entry_price=entry_price,
            stop_loss=stop_loss,
            current_sl=stop_loss,
            take_profits=take_profits,
            position_size=position_size,
            leverage=leverage,
            entry_reason=signal.get("entry_reason", "signal"),
            grade=signal.get("grade", ""),
            confidence=signal.get("confidence", 0.0),
            timeframe=signal.get("timeframe", ""),
            indicators=signal.get("indicators", {}),
        )

        logger.info(
            "Executing entry: %s %s %s @ %.4f, size=%.6f, SL=%.4f, lev=%dx",
            trade.trade_id, symbol, side.value, entry_price,
            position_size, stop_loss, leverage,
        )

        try:
            # Set leverage on exchange if futures
            if self.market_type == "futures":
                await self._set_leverage(symbol, leverage)

            # Place order based on config (maker = limit at signal price, taker = market)
            order_side = "buy" if side == TradeSide.LONG else "sell"
            exec_cfg = {"order_type": "auto", "retry_taker_on_reject": True}  # hardcoded since engine has no _config
            _order_type = exec_cfg.get("order_type", "maker")
            
            if _order_type in ("maker", "auto"):
                # Post-only limit order at signal price (zero slippage, maker fee)
                order = await self._place_order_with_retry(
                    symbol=symbol,
                    order_type="limit",
                    side=order_side,
                    amount=position_size,
                    price=entry_price,
                    params={"postOnly": True},
                )
                # If limit order rejected (price crossed), retry as market
                if order is None and exec_cfg.get("retry_taker_on_reject", True):
                    logger.warning("Maker order rejected for %s — retrying as taker", symbol)
                    order = await self._place_order_with_retry(
                        symbol=symbol,
                        order_type="market",
                        side=order_side,
                        amount=position_size,
                    )
            else:
                # Pure taker: market order
                order = await self._place_order_with_retry(
                    symbol=symbol,
                    order_type="market",
                    side=order_side,
                    amount=position_size,
                )

            if order is None:
                trade.mark_failed("order_rejected_after_retries")
                return trade

            order_id = order.get("id", "")
            trade.order_ids.append(order_id)
            trade.entry_order_id = order_id

            # Resolve fill price and size
            fill_price = order.get("average") or order.get("price") or entry_price
            fill_size = order.get("filled", position_size)
            fee_cost = self._extract_fee(order)
            slippage = abs(fill_price - entry_price) * fill_size

            trade.mark_filled(fill_price, fill_size, fee_cost)
            trade.slippage = slippage

            # Handle partial fill
            if fill_size < position_size * 0.99:
                logger.warning(
                    "Partial fill on entry: %.6f / %.6f",
                    fill_size, position_size,
                )

            # Place SL/TP orders on the exchange
            await self._place_sl_tp_orders(trade)

            # Register with risk manager
            if self.risk_manager:
                notional = fill_price * fill_size
                self.risk_manager.register_position(symbol, notional)

            self.active_trades[trade.trade_id] = trade

            logger.info(
                "Entry filled: %s @ %.4f (slippage: %.4f, fee: %.4f)",
                trade.trade_id, fill_price, slippage, fee_cost,
            )

        except Exception as e:
            logger.error("Entry execution failed for %s: %s", trade.trade_id, e)
            trade.mark_failed(f"execution_error: {e}")

        return trade

    # ==================================================================
    # Exit
    # ==================================================================

    async def execute_exit(self, trade: Trade, reason: str = "manual") -> Trade:
        """Close the entire remaining position for a trade."""
        if not trade.is_open:
            logger.warning("Cannot exit trade %s: status=%s", trade.trade_id, trade.status.value)
            return trade

        symbol = trade.symbol
        remaining = trade.remaining_size

        logger.info(
            "Executing full exit: %s %s, size=%.6f, reason=%s",
            trade.trade_id, symbol, remaining, reason,
        )

        try:
            # Cancel any open TP/SL orders first
            await self.cancel_open_orders(symbol)

            # Place closing market order
            close_side = "sell" if trade.side == TradeSide.LONG else "buy"
            order = await self._place_order_with_retry(
                symbol=symbol,
                order_type="market",
                side=close_side,
                amount=remaining,
                params={"reduceOnly": True} if self.market_type == "futures" else {},
            )

            if order is None:
                logger.error("Exit order rejected for %s", trade.trade_id)
                return trade

            trade.order_ids.append(order.get("id", ""))

            exit_price = order.get("average") or order.get("price") or 0.0
            exit_size = order.get("filled", remaining)
            fee_cost = self._extract_fee(order)

            pnl = trade.mark_closed(exit_price, fee_cost, reason)

            # Update risk manager
            if self.risk_manager:
                notional = trade.entry_price * exit_size
                self.risk_manager.unregister_position(symbol, notional)
                self.risk_manager.record_trade_result(pnl)
                if reason in ("stop_loss", "sl"):
                    self.risk_manager.record_sl_hit(symbol)

            # Remove from active trades
            self.active_trades.pop(trade.trade_id, None)

            logger.info(
                "Exit filled: %s @ %.4f, PnL=%.2f (%.2f%%), reason=%s",
                trade.trade_id, exit_price, trade.pnl, trade.pnl_pct, reason,
            )

        except Exception as e:
            logger.error("Exit execution failed for %s: %s", trade.trade_id, e)

        return trade

    async def execute_partial_exit(
        self, trade: Trade, pct: float, reason: str = "partial_tp"
    ) -> Trade:
        """
        Close a percentage of the remaining position.

        Parameters
        ----------
        pct : float
            Fraction to close (0.0 to 1.0).
        """
        if not trade.is_open:
            return trade

        exit_size = trade.remaining_size * pct
        if exit_size <= 0:
            return trade

        symbol = trade.symbol

        logger.info(
            "Executing partial exit: %s %s, %.0f%% (%.6f), reason=%s",
            trade.trade_id, symbol, pct * 100, exit_size, reason,
        )

        try:
            close_side = "sell" if trade.side == TradeSide.LONG else "buy"
            order = await self._place_order_with_retry(
                symbol=symbol,
                order_type="market",
                side=close_side,
                amount=exit_size,
                params={"reduceOnly": True} if self.market_type == "futures" else {},
            )

            if order is None:
                logger.error("Partial exit order rejected for %s", trade.trade_id)
                return trade

            trade.order_ids.append(order.get("id", ""))

            exit_price = order.get("average") or order.get("price") or 0.0
            filled = order.get("filled", exit_size)
            fee_cost = self._extract_fee(order)

            chunk_pnl = trade.mark_partial_exit(exit_price, filled, fee_cost, reason)

            if self.risk_manager:
                notional = trade.entry_price * filled
                self.risk_manager.unregister_position(symbol, notional)
                self.risk_manager.record_trade_result(chunk_pnl)

            # If fully closed, remove from active
            if not trade.is_open:
                self.active_trades.pop(trade.trade_id, None)

            logger.info(
                "Partial exit filled: %s @ %.4f, chunk_pnl=%.2f, remaining=%.6f",
                trade.trade_id, exit_price, chunk_pnl, trade.remaining_size,
            )

        except Exception as e:
            logger.error("Partial exit failed for %s: %s", trade.trade_id, e)

        return trade

    # ==================================================================
    # TP / SL monitoring
    # ==================================================================

    async def check_tp_sl(self, trade: Trade, current_price: float) -> Optional[str]:
        """
        Check if current price has triggered any TP or SL for the trade.
        Executes the appropriate action and returns the action taken
        ('stop_loss', 'take_profit_1', etc.) or None.
        """
        if not trade.is_open:
            return None

        trade.update_price(current_price)

        # Check stop loss first (higher priority)
        if trade.is_stop_hit(current_price):
            logger.warning(
                "STOP LOSS HIT: %s @ %.4f (SL=%.4f)",
                trade.trade_id, current_price, trade.current_sl,
            )
            await self.execute_exit(trade, "stop_loss")
            return "stop_loss"

        # Check take profits
        tp_hit = trade.is_tp_hit(current_price)
        if tp_hit is not None:
            tp_index = trade.take_profits.index(tp_hit) + 1
            tp_hit.hit = True
            tp_hit.hit_time = __import__("datetime").datetime.utcnow()

            logger.info(
                "TAKE PROFIT %d HIT: %s @ %.4f (TP=%.4f, close %.0f%%)",
                tp_index, trade.trade_id, current_price,
                tp_hit.price, tp_hit.close_pct * 100,
            )

            # Execute partial close for this TP level
            if tp_hit.close_pct > 0:
                await self.execute_partial_exit(
                    trade, tp_hit.close_pct, f"take_profit_{tp_index}"
                )

            # Move SL to break-even after TP1
            if tp_index == 1 and self.break_even_after_tp1 and trade.is_open:
                old_sl = trade.current_sl
                trade.current_sl = trade.entry_price
                logger.info(
                    "SL moved to break-even: %s %.4f -> %.4f",
                    trade.trade_id, old_sl, trade.entry_price,
                )

            return f"take_profit_{tp_index}"

        # Check trailing stop activation / update
        if self.trailing_enabled and trade.is_open:
            await self.update_trailing_stop(trade, current_price)

        return None

    async def update_trailing_stop(self, trade: Trade, current_price: float) -> Trade:
        """
        Update the trailing stop for a trade based on current price movement.

        Trailing activates once unrealized profit reaches activation_rr * risk,
        then trails at trail_pct below the highest (long) or above lowest (short).
        """
        if not trade.is_open or trade.entry_price <= 0 or trade.stop_loss <= 0:
            return trade

        risk_per_unit = abs(trade.entry_price - trade.stop_loss)
        if risk_per_unit <= 0:
            return trade

        # Check activation threshold
        if trade.side == TradeSide.LONG:
            current_profit = current_price - trade.entry_price
        else:
            current_profit = trade.entry_price - current_price

        activation_threshold = risk_per_unit * self.trailing_activation_rr

        if current_profit < activation_threshold:
            return trade

        # Trailing is now active
        if not trade.trailing_activated:
            trade.trailing_activated = True
            logger.info(
                "Trailing stop activated for %s at price %.4f",
                trade.trade_id, current_price,
            )

        # Calculate new trailing stop
        trail_distance = current_price * (self.trailing_pct / 100.0)

        if trade.side == TradeSide.LONG:
            new_sl = trade.highest_price_seen - trail_distance
            if new_sl > trade.current_sl:
                old_sl = trade.current_sl
                trade.current_sl = new_sl
                logger.debug(
                    "Trailing SL updated: %s %.4f -> %.4f (high=%.4f)",
                    trade.trade_id, old_sl, new_sl, trade.highest_price_seen,
                )
        else:
            new_sl = trade.lowest_price_seen + trail_distance
            if new_sl < trade.current_sl or trade.current_sl <= 0:
                old_sl = trade.current_sl
                trade.current_sl = new_sl
                logger.debug(
                    "Trailing SL updated: %s %.4f -> %.4f (low=%.4f)",
                    trade.trade_id, old_sl, new_sl, trade.lowest_price_seen,
                )

        trade.updated_at = __import__("datetime").datetime.utcnow()
        return trade

    # ==================================================================
    # Position sync
    # ==================================================================

    async def sync_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Fetch the actual position from the exchange and reconcile
        with local state. Returns the exchange position data.
        """
        try:
            if self.market_type == "futures":
                positions = await self.exchange.fetch_positions([symbol])
                for pos in positions:
                    if pos.get("symbol") == symbol:
                        contracts = abs(float(pos.get("contracts", 0)))
                        notional = abs(float(pos.get("notional", 0)))
                        side = pos.get("side", "")

                        logger.info(
                            "Exchange position for %s: side=%s, contracts=%.6f, notional=$%.2f",
                            symbol, side, contracts, notional,
                        )

                        # Reconcile with any active trades for this symbol
                        for trade in list(self.active_trades.values()):
                            if trade.symbol == symbol and trade.is_open:
                                if contracts == 0:
                                    logger.warning(
                                        "Trade %s shows open locally but closed on exchange",
                                        trade.trade_id,
                                    )
                        return pos
            else:
                balance = await self.exchange.fetch_balance()
                base = symbol.split("/")[0]
                free = float(balance.get(base, {}).get("free", 0))
                total = float(balance.get(base, {}).get("total", 0))
                logger.info(
                    "Spot balance for %s: free=%.6f, total=%.6f",
                    base, free, total,
                )
                return {"symbol": symbol, "free": free, "total": total}

        except Exception as e:
            logger.error("Failed to sync position for %s: %s", symbol, e)
        return None

    async def cancel_open_orders(self, symbol: str) -> int:
        """
        Cancel all open orders for a symbol on the exchange.
        Returns the number of orders cancelled.
        """
        cancelled = 0
        raw_exchange = getattr(self.exchange, '_exchange', self.exchange)
        ex_symbol = symbol
        if hasattr(self.exchange, '_to_exchange_symbol'):
            ex_symbol = self.exchange._to_exchange_symbol(symbol)
        try:
            open_orders = await raw_exchange.fetch_open_orders(ex_symbol)
            for order in open_orders:
                try:
                    await raw_exchange.cancel_order(order["id"], ex_symbol)
                    cancelled += 1
                    logger.debug("Cancelled order %s for %s", order["id"], symbol)
                except Exception as e:
                    logger.warning("Failed to cancel order %s: %s", order["id"], e)

            if cancelled > 0:
                logger.info("Cancelled %d open orders for %s", cancelled, symbol)

        except Exception as e:
            logger.error("Failed to fetch/cancel orders for %s: %s", symbol, e)

        return cancelled

    # ==================================================================
    # Internal helpers
    # ==================================================================

    def _build_take_profits(
        self,
        signal: Dict[str, Any],
        entry_price: float,
        stop_loss: float,
        side: TradeSide,
    ) -> List[TakeProfit]:
        """Build TP levels from signal data or config-based R:R ratios."""
        # If signal provides explicit take-profit levels, use them
        if "take_profits" in signal and signal["take_profits"]:
            tps = []
            for tp_data in signal["take_profits"]:
                if isinstance(tp_data, dict):
                    tps.append(TakeProfit(
                        price=tp_data["price"],
                        close_pct=tp_data.get("close_pct", 0.33),
                    ))
                elif isinstance(tp_data, (int, float)):
                    tps.append(TakeProfit(price=float(tp_data), close_pct=0.33))
            return tps

        # Calculate from R:R ratios
        if stop_loss <= 0 or entry_price <= 0:
            return []

        risk_distance = abs(entry_price - stop_loss)
        tps = []
        for rr, close_pct in zip(self.tp_rr_levels, self.tp_close_pcts):
            if side == TradeSide.LONG:
                tp_price = entry_price + (risk_distance * rr)
            else:
                tp_price = entry_price - (risk_distance * rr)
            tps.append(TakeProfit(price=round(tp_price, 8), close_pct=close_pct))

        return tps

    async def _place_order_with_retry(
        self,
        symbol: str,
        order_type: str,
        side: str,
        amount: float,
        price: Optional[float] = None,
        params: Optional[Dict] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Place an order with exponential backoff retry on transient failures.

        Returns the order dict on success, or None on permanent failure.
        """
        params = params or {}
        last_error = None

        for attempt in range(MAX_RETRIES):
            try:
                # Use raw ccxt exchange directly (bypass CcxtExchangeClient wrapper)
                # Raw ccxt signature: create_order(symbol, type, side, amount, price, params)
                raw_exchange = getattr(self.exchange, '_exchange', self.exchange)
                ex_symbol = symbol
                # Convert symbol for exchange if wrapper has the method
                if hasattr(self.exchange, '_to_exchange_symbol'):
                    ex_symbol = self.exchange._to_exchange_symbol(symbol)

                if order_type == "market":
                    order = await raw_exchange.create_order(
                        ex_symbol, "market", side, amount, None, params
                    )
                elif order_type == "limit":
                    order = await raw_exchange.create_order(
                        ex_symbol, "limit", side, amount, price, params
                    )
                elif order_type == "stop_market":
                    order = await raw_exchange.create_order(
                        ex_symbol, "market", side, amount, None,
                        {**params, "stopPrice": price, "type": "stop_market"},
                    )
                else:
                    order = await self.exchange.create_order(
                        symbol, order_type, side, amount, price, params
                    )

                logger.debug(
                    "Order placed: %s %s %s %.6f @ %s (id=%s)",
                    symbol, order_type, side, amount,
                    price or "market", order.get("id"),
                )
                return order

            except Exception as e:
                last_error = e
                error_msg = str(e).lower()

                # Non-retryable errors
                if any(keyword in error_msg for keyword in [
                    "insufficient", "not enough", "margin",
                    "invalid", "min notional", "lot size",
                ]):
                    logger.error(
                        "Non-retryable order error (attempt %d/%d): %s",
                        attempt + 1, MAX_RETRIES, e,
                    )
                    return None

                # Retryable error
                delay = RETRY_DELAY_BASE * (2 ** attempt)
                logger.warning(
                    "Order failed (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1, MAX_RETRIES, delay, e,
                )
                await asyncio.sleep(delay)

        logger.error(
            "Order failed after %d attempts: %s", MAX_RETRIES, last_error
        )
        return None

    async def _place_sl_tp_orders(self, trade: Trade) -> None:
        """
        Place stop-loss and take-profit orders on the exchange after entry fill.
        These are best-effort; the bot also monitors prices locally.
        Uses raw ccxt exchange to bypass CcxtExchangeClient wrapper.
        """
        symbol = trade.symbol
        close_side = "sell" if trade.side == TradeSide.LONG else "buy"
        raw_exchange = getattr(self.exchange, '_exchange', self.exchange)
        ex_symbol = symbol
        if hasattr(self.exchange, '_to_exchange_symbol'):
            ex_symbol = self.exchange._to_exchange_symbol(symbol)

        # Place SL as stop-market (full position close)
        if trade.stop_loss > 0:
            try:
                sl_size = max(1, round(trade.remaining_size))  # Integer lots for Delta
                sl_order = await raw_exchange.create_order(
                    ex_symbol, "market", close_side, sl_size, None,
                    {
                        "stopPrice": trade.stop_loss,
                        "type": "stop_market",
                        "reduceOnly": True,
                    },
                )
                if sl_order:
                    trade.order_ids.append(sl_order.get("id", ""))
                    logger.info("SL order placed: %s @ %.4f (id=%s)",
                               symbol, trade.stop_loss, sl_order.get("id"))
            except Exception as e:
                logger.warning("Failed to place SL order for %s: %s", trade.trade_id, e)

        # Place TP1 as limit order (only TP1 on exchange, bot manages TP2/TP3)
        if trade.take_profits:
            tp = trade.take_profits[0]
            try:
                tp_size = trade.remaining_size * tp.close_pct
                # Delta uses integer lots — round up to at least 1
                tp_size = max(1, round(tp_size))
                # If position is small (1-2 lots), close entire position at TP1
                if trade.remaining_size <= 2:
                    tp_size = trade.remaining_size
                if tp_size > 0 and tp.price > 0:
                    tp_order = await raw_exchange.create_order(
                        ex_symbol, "limit", close_side, tp_size, tp.price,
                        {"reduceOnly": True},
                    )
                    if tp_order:
                        tp.order_id = tp_order.get("id", "")
                        trade.order_ids.append(tp.order_id)
                        logger.info(
                            "TP1 order placed: %s @ %.4f (%d lots, id=%s)",
                            symbol, tp.price, tp_size, tp.order_id,
                        )
            except Exception as e:
                logger.warning("Failed to place TP1 order for %s: %s", trade.trade_id, e)

    async def _set_leverage(self, symbol: str, leverage: int) -> None:
        """Set leverage on the exchange for a symbol."""
        try:
            # CcxtExchangeClient: set_leverage(symbol, leverage)
            # Raw ccxt: set_leverage(leverage, symbol)
            raw_exchange = getattr(self.exchange, '_exchange', self.exchange)
            ex_symbol = symbol
            if hasattr(self.exchange, '_to_exchange_symbol'):
                ex_symbol = self.exchange._to_exchange_symbol(symbol)
            await raw_exchange.set_leverage(leverage, ex_symbol)
            logger.debug("Leverage set to %dx for %s", leverage, symbol)
        except Exception as e:
            # Some exchanges don't support per-symbol leverage changes
            logger.debug("Could not set leverage for %s: %s", symbol, e)

    @staticmethod
    def _extract_fee(order: Dict[str, Any]) -> float:
        """Extract total fee cost from an order response."""
        fee = order.get("fee", {})
        if isinstance(fee, dict):
            return float(fee.get("cost", 0) or 0)
        if isinstance(fee, (int, float)):
            return float(fee)
        # Some exchanges put fees in trades list
        trades = order.get("trades", [])
        total_fee = 0.0
        for t in trades:
            f = t.get("fee", {})
            if isinstance(f, dict):
                total_fee += float(f.get("cost", 0) or 0)
        return total_fee

    # ==================================================================
    # Introspection
    # ==================================================================

    def get_active_trades(self) -> List[Trade]:
        """Return all currently active (open/partial) trades."""
        return [t for t in self.active_trades.values() if t.is_open]

    def get_trade(self, trade_id: str) -> Optional[Trade]:
        """Look up a trade by ID."""
        return self.active_trades.get(trade_id)

    def __repr__(self) -> str:
        return f"ExecutionEngine(active={len(self.active_trades)}, market={self.market_type})"
