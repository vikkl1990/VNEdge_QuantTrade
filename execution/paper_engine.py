"""
Paper execution engine for the crypto trading bot.

Simulates order execution with configurable fees and slippage,
tracking virtual PnL without placing real exchange orders.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from execution.trade import Trade, TakeProfit, TradeStatus, TradeSide

logger = logging.getLogger(__name__)


class PaperExecutionEngine:
    """Simulated execution engine for paper trading and forward testing.

    Mimics the ExecutionEngine interface but executes all orders locally
    against the last known price, applying configurable fee and slippage
    models.
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        paper_cfg = config.get("paper_trading", {})
        risk_cfg = config.get("risk", {})

        self.initial_balance: float = paper_cfg.get("initial_balance", 10_000)
        self.balance: float = self.initial_balance
        # Delta India fees: taker 0.06%, maker 0.04%, settlement 0.06%
        self.taker_fee_rate: float = paper_cfg.get("taker_fee_rate", 0.0006)
        self.maker_fee_rate: float = paper_cfg.get("maker_fee_rate", 0.0004)
        self.fee_rate: float = paper_cfg.get("fee_rate", 0.0006)  # default taker
        self.settlement_fee_rate: float = paper_cfg.get("settlement_fee_rate", 0.0006)
        self.slippage_pct: float = paper_cfg.get("slippage_pct", 0.05)

        self.default_leverage: int = risk_cfg.get("default_leverage", 5)
        self.risk_per_trade_pct: float = risk_cfg.get("risk_per_trade_pct", 1.0)

        # TP config
        tp_cfg = risk_cfg.get("take_profit", {})
        self.tp1_close_pct: float = tp_cfg.get("tp1_close_pct", 40) / 100.0
        self.tp2_close_pct: float = tp_cfg.get("tp2_close_pct", 30) / 100.0
        self.tp3_close_pct: float = tp_cfg.get("tp3_close_pct", 30) / 100.0

        # Trailing config
        trail_cfg = risk_cfg.get("trailing", {})
        self.trailing_enabled: bool = trail_cfg.get("enabled", True)
        self.trail_activation_rr: float = trail_cfg.get("activation_rr", 1.0)
        self.trail_pct: float = trail_cfg.get("trail_pct", 0.5) / 100.0
        self.break_even_after_tp1: bool = trail_cfg.get("break_even_after_tp1", True)

        # Active trades
        self._open_trades: Dict[str, Trade] = {}  # trade_id -> Trade
        self._closed_trades: List[Trade] = []

        # Liquidity tiers for realistic slippage model
        self._liquidity_tier: Dict[str, float] = {
            "BTC": 1.0, "ETH": 1.0,
            "SOL": 1.5, "AVAX": 1.5, "LINK": 1.5, "XRP": 1.5, "ADA": 1.5,
            "DOGE": 2.5, "SHIB": 2.5, "PEPE": 2.5, "WIF": 2.5, "BONK": 2.5,
            "FLOKI": 2.5, "MEME": 2.5,
        }
        # Rolling ATR tracker for volatility-adjusted slippage
        self._avg_atr: Dict[str, float] = {}  # symbol -> rolling average ATR

    # ------------------------------------------------------------------
    # Realistic slippage model
    # ------------------------------------------------------------------

    def _calculate_realistic_slippage(
        self,
        symbol: str,
        side: str,
        position_usd: float,
        is_maker: bool = False,
        current_atr: float = 0.0,
    ) -> float:
        """Realistic slippage model based on position size and symbol liquidity.

        Returns slippage_pct (always positive, represents cost).

        Components:
        - Base: 0.02% maker / 0.05% taker
        - Size impact: +0.01% per $1000 beyond $500
        - Liquidity factor: BTC/ETH=1.0x, alts=1.5x, memes=2.5x
        - Volatility factor: scaled by current_atr / avg_atr
        - Capped at 0.15%
        """
        # 1. Base slippage (FIX 7: raised to simulate real market conditions)
        base_slip = 0.04 if is_maker else 0.08  # was 0.02/0.05 — now realistic

        # 2. Size impact: larger positions move the book more
        excess_usd = max(0, position_usd - 500.0)
        size_impact = (excess_usd / 1000.0) * 0.01  # +0.01% per $1000 above $500

        # 3. Liquidity factor from symbol
        coin = symbol.split("/")[0].upper() if "/" in symbol else symbol[:3].upper()
        liq_factor = self._liquidity_tier.get(coin, 1.5)  # default to alt-tier

        # 4. Volatility factor (high vol = more slippage)
        vol_factor = 1.0
        if current_atr > 0:
            avg_atr = self._avg_atr.get(symbol, current_atr)
            if avg_atr > 0:
                atr_ratio = current_atr / avg_atr
                vol_factor = min(atr_ratio, 2.0)  # cap at 2x
            # Update rolling average (EMA with alpha=0.1)
            self._avg_atr[symbol] = avg_atr * 0.9 + current_atr * 0.1

        # Combine all factors
        slip_pct = (base_slip + size_impact) * liq_factor * vol_factor

        # Cap at 0.15% max
        slip_pct = min(slip_pct, 0.15)

        return slip_pct

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        logger.info(
            "Paper execution engine started | balance=%.2f | taker_fee=%.4f | maker_fee=%.4f | settlement=%.4f | slippage=%.2f%%",
            self.balance,
            self.taker_fee_rate,
            self.maker_fee_rate,
            self.settlement_fee_rate,
            self.slippage_pct,
        )

    async def stop(self) -> None:
        logger.info(
            "Paper execution engine stopped | balance=%.2f | open=%d closed=%d",
            self.balance,
            len(self._open_trades),
            len(self._closed_trades),
        )

    # ------------------------------------------------------------------
    # Order execution
    # ------------------------------------------------------------------

    async def execute(
        self, symbol: str, signal: Dict[str, Any]
    ) -> Optional[Trade]:
        """Dispatch a signal dict from the orchestrator to execute_entry.

        This is the interface the orchestrator calls:
            order_result = await self._execution.execute(symbol, sig_dict)
        """
        side = signal.get("side", "long")
        entry_price = signal.get("entry_price", 0.0)
        stop_loss = signal.get("stop_loss", 0.0)
        take_profits = signal.get("take_profits", [])

        if entry_price <= 0:
            logger.warning("execute() called with invalid entry_price for %s", symbol)
            return None

        return await self.execute_entry(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profits=take_profits,
            position_size=signal.get("position_size"),
            leverage=signal.get("leverage"),
            trade_id=signal.get("trade_id"),
            reason=signal.get("entry_reason", signal.get("reason", "")),
            grade=signal.get("grade", ""),
            confidence=signal.get("confidence", 0),
            timeframe=signal.get("timeframe", ""),
            indicators=signal.get("indicators"),
        )

    async def execute_entry(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        stop_loss: float,
        take_profits: List[float],
        position_size: Optional[float] = None,
        leverage: Optional[int] = None,
        trade_id: Optional[str] = None,
        reason: str = "",
        grade: str = "",
        confidence: float = 0,
        timeframe: str = "",
        indicators: Optional[Dict[str, Any]] = None,
    ) -> Optional[Trade]:
        """Simulate a market entry order."""
        lev = leverage or self.default_leverage

        # MAKER-ONLY: post_only limit order rests at signal price = ZERO slippage
        # In real execution: place limit order at signal_price, waits for fill
        # In paper: fill at signal_price exactly (no slippage for maker)
        use_maker = True  # Always maker for entry (Scalper optimization)

        # Maker orders: zero slippage (order rests in book at signal price)
        # Taker orders would use _calculate_realistic_slippage
        if use_maker:
            slip = 0.0
            slip_pct = 0.0
            fill_price = entry_price  # exact fill at signal price
        else:
            est_position_usd = (position_size * entry_price / lev) if position_size else 500.0
            slip_pct = self._calculate_realistic_slippage(
                symbol, side, est_position_usd, is_maker=False,
            )
            slip = entry_price * (slip_pct / 100.0)
            if side == "long":
                fill_price = entry_price + slip
            else:
                fill_price = entry_price - slip

        # Calculate position size if not specified
        if position_size is None:
            risk_amount = self.balance * (self.risk_per_trade_pct / 100.0)
            sl_distance = abs(fill_price - stop_loss)
            if sl_distance <= 0:
                logger.warning("Invalid SL distance for %s", symbol)
                return None
            position_size = (risk_amount / sl_distance) * lev

        notional = fill_price * position_size / lev   # margin actually deployed
        # Exchange fees are charged on FULL notional (price × size), not margin.
        from execution.fees import get_fee_model
        fee = get_fee_model().leg_fee_usd(
            fill_price * position_size, "maker" if use_maker else "taker", symbol,
        )

        if notional > self.balance:
            logger.info(
                "Paper balance low for %s: need %.2f, have %.2f — proceeding (risk model is authoritative)",
                symbol, notional, self.balance,
            )
            # Don't reject — signal tracker / risk model already validated sizing

        # Build TP levels
        tp_list = []
        close_pcts = [self.tp1_close_pct, self.tp2_close_pct, self.tp3_close_pct]
        for i, tp_price in enumerate(take_profits[:3]):
            pct = close_pcts[i] if i < len(close_pcts) else 1.0
            tp_list.append(TakeProfit(price=tp_price, close_pct=pct))

        trade_side = TradeSide.LONG if side == "long" else TradeSide.SHORT

        trade = Trade(
            symbol=symbol,
            side=trade_side,
            stop_loss=stop_loss,
            take_profits=tp_list,
            leverage=lev,
            entry_reason=reason,
            grade=grade,
            confidence=confidence,
            timeframe=timeframe,
            indicators=indicators or {},
        )

        if trade_id:
            trade.trade_id = trade_id

        trade.mark_filled(fill_price, position_size, fee)
        trade.slippage = slip

        self.balance -= fee
        self._open_trades[trade.trade_id] = trade

        logger.info(
            "PAPER ENTRY: %s %s %s @ %.4f (slip=%.4f) | size=%.6f | SL=%.4f | fee=%.4f",
            trade.trade_id,
            symbol,
            side,
            fill_price,
            slip,
            position_size,
            stop_loss,
            fee,
        )

        return trade

    async def execute_exit(
        self,
        trade_id: str,
        current_price: float,
        reason: str = "",
        close_pct: float = 1.0,
    ) -> Optional[float]:
        """Simulate closing a position (fully or partially)."""
        trade = self._open_trades.get(trade_id)
        if trade is None:
            logger.warning("Paper exit: trade %s not found", trade_id)
            return None

        # Realistic exit slippage (taker for market exits)
        exit_position_usd = current_price * trade.remaining_size * close_pct / trade.leverage
        exit_slip_pct = self._calculate_realistic_slippage(
            trade.symbol, "exit", exit_position_usd, is_maker=False,
        )
        slip = current_price * (exit_slip_pct / 100.0)
        if trade.side == TradeSide.LONG:
            fill_price = current_price - slip
        else:
            fill_price = current_price + slip

        exit_size = trade.remaining_size * close_pct
        # Exits are market orders → taker, charged on the leg's full notional
        from execution.fees import get_fee_model
        fee = get_fee_model().leg_fee_usd(fill_price * exit_size, "taker", trade.symbol)

        if close_pct >= 1.0:
            pnl = trade.mark_closed(fill_price, fee, reason)
        else:
            pnl = trade.mark_partial_exit(fill_price, exit_size, fee, reason)

        self.balance += pnl - fee

        logger.info(
            "PAPER EXIT: %s @ %.4f | PnL=%.4f | reason=%s | remaining=%.6f",
            trade_id,
            fill_price,
            pnl,
            reason,
            trade.remaining_size,
        )

        if not trade.is_open:
            del self._open_trades[trade_id]
            self._closed_trades.append(trade)

        return pnl

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    async def check_positions(self, prices: Dict[str, float]) -> List[Dict[str, Any]]:
        """Check all open positions against current prices.

        Returns a list of events (SL hit, TP hit, trailing updates).
        """
        events = []

        for trade_id, trade in list(self._open_trades.items()):
            price = prices.get(trade.symbol)
            if price is None:
                continue

            trade.update_price(price)

            # Check stop-loss
            if trade.is_stop_hit(price):
                pnl = await self.execute_exit(trade_id, price, "stop_loss_hit")
                events.append({
                    "type": "SL_HIT",
                    "trade_id": trade_id,
                    "symbol": trade.symbol,
                    "price": price,
                    "pnl": pnl,
                })
                continue

            # Check take-profits
            tp = trade.is_tp_hit(price)
            if tp and not tp.hit:
                tp.hit = True
                tp.hit_time = datetime.now(timezone.utc)

                pnl = await self.execute_exit(
                    trade_id, price,
                    reason=f"tp_hit_{trade.take_profits.index(tp) + 1}",
                    close_pct=tp.close_pct,
                )
                events.append({
                    "type": f"TP{trade.take_profits.index(tp) + 1}_HIT",
                    "trade_id": trade_id,
                    "symbol": trade.symbol,
                    "price": price,
                    "pnl": pnl,
                })

                # ── ADAPTIVE SL: Ratchet up at each TP level ──
                # After TP1 → SL moves to 75% of TP1 profit
                # After TP2 → SL moves to TP1 price (lock in full TP1)
                # After TP3 → SL moves to TP2 price (lock in full TP2)
                tp_idx = trade.take_profits.index(tp)
                if self.break_even_after_tp1 and trade.is_open:
                    tp_price = tp.price
                    entry = trade.entry_price

                    if tp_idx == 0:
                        # TP1 hit → SL = 75% of TP1 distance above entry
                        tp1_dist = abs(tp_price - entry)
                        buffer = tp1_dist * 0.25
                        if trade.side == TradeSide.LONG:
                            new_sl = tp_price - buffer
                        else:
                            new_sl = tp_price + buffer
                        lock_label = "75% of TP1"

                    elif tp_idx == 1:
                        # TP2 hit → SL = TP1 price (lock in full TP1 profit)
                        tp1_price = trade.take_profits[0].price
                        new_sl = tp1_price
                        lock_label = "at TP1"

                    elif tp_idx == 2:
                        # TP3 hit → SL = TP2 price (lock in full TP2 profit)
                        tp2_price = trade.take_profits[1].price
                        new_sl = tp2_price
                        lock_label = "at TP2"
                    else:
                        new_sl = trade.current_sl
                        lock_label = "unchanged"

                    # Only move SL if it's more favorable than current
                    if trade.side == TradeSide.LONG:
                        should_move = new_sl > trade.current_sl
                    else:
                        should_move = new_sl < trade.current_sl or trade.current_sl <= 0

                    if should_move:
                        trade.current_sl = new_sl
                        locked_pct = abs(new_sl - entry) / abs(tp_price - entry) * 100 if abs(tp_price - entry) > 0 else 0
                        logger.info(
                            "TP%d HIT → SL ratcheted %s: %s SL=%.4f (TP%d=%.4f, entry=%.4f)",
                            tp_idx + 1, lock_label, trade_id, new_sl, tp_idx + 1, tp_price, entry,
                        )
                        events.append({
                            "type": f"SL_RATCHET_TP{tp_idx + 1}",
                            "trade_id": trade_id,
                            "symbol": trade.symbol,
                            "new_sl": new_sl,
                            "tp_price": tp_price,
                            "lock_label": lock_label,
                        })
                continue

            # Trailing stop update
            if self.trailing_enabled and trade.is_open:
                self._update_trailing(trade, price)

        return events

    def _update_trailing(self, trade: Trade, price: float) -> None:
        """Update trailing stop if conditions are met."""
        risk = abs(trade.entry_price - trade.stop_loss)
        if risk <= 0:
            return

        # Check if trailing should activate
        if trade.side == TradeSide.LONG:
            current_rr = (price - trade.entry_price) / risk
            if current_rr >= self.trail_activation_rr:
                new_sl = price * (1 - self.trail_pct)
                if new_sl > trade.current_sl:
                    trade.current_sl = new_sl
                    trade.trailing_activated = True
        else:
            current_rr = (trade.entry_price - price) / risk
            if current_rr >= self.trail_activation_rr:
                new_sl = price * (1 + self.trail_pct)
                if new_sl < trade.current_sl or trade.current_sl <= 0:
                    trade.current_sl = new_sl
                    trade.trailing_activated = True

    # ------------------------------------------------------------------
    # State queries
    # ------------------------------------------------------------------

    def get_open_trades(self) -> Dict[str, Trade]:
        return dict(self._open_trades)

    def get_closed_trades(self) -> List[Trade]:
        return list(self._closed_trades)

    def get_trade(self, trade_id: str) -> Optional[Trade]:
        return self._open_trades.get(trade_id)

    @property
    def equity(self) -> float:
        """Current equity including unrealized PnL."""
        unrealized = sum(t.unrealized_pnl for t in self._open_trades.values())
        return self.balance + unrealized

    @property
    def open_position_count(self) -> int:
        return len(self._open_trades)

    def get_position_for_symbol(self, symbol: str) -> Optional[Trade]:
        for trade in self._open_trades.values():
            if trade.symbol == symbol:
                return trade
        return None

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def get_state(self) -> Dict[str, Any]:
        """Serialize state for recovery."""
        return {
            "balance": self.balance,
            "open_trades": {tid: t.to_dict() for tid, t in self._open_trades.items()},
            "closed_count": len(self._closed_trades),
        }

    def restore_state(self, state: Dict[str, Any]) -> None:
        """Restore state from persisted data."""
        self.balance = state.get("balance", self.initial_balance)
        for tid, tdata in state.get("open_trades", {}).items():
            trade = Trade.from_dict(tdata)
            self._open_trades[tid] = trade
        logger.info(
            "Paper engine state restored: balance=%.2f, open_trades=%d",
            self.balance,
            len(self._open_trades),
        )
