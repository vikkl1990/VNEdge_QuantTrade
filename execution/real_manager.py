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
from exchange.delta_client import DeltaClient, PRODUCT_MAP
from bot.signal_tracker import TRADE_TYPE_CONFIG, TRADE_TYPE_SCALP

logger = logging.getLogger("bot.real_trading")

STATE_FILE = Path("storage/real_trading_state.json")

# Delta India fee structure (GST-inclusive)
# Taker: 0.059%, Maker: 0.0236%, Settlement: 0.059%
# Round-trip worst case (taker entry + taker exit): 0.059% × 2 = 0.118%
DELTA_ROUND_TRIP_FEE_PCT = 0.00118


def _normalize_side(side) -> str:
    """Normalize trade side to 'long' or 'short' string regardless of input type."""
    if hasattr(side, "value"):
        side = side.value
    s = str(side).lower().strip()
    if s in ("long", "buy"):
        return "long"
    if s in ("short", "sell"):
        return "short"
    return s  # fallback: return as-is


def _safe_connect(delta, timeout_sec: float = 5.0) -> bool:
    """Attempt to connect delta client with timeout protection.

    Returns True if connected, False if failed.
    """
    if delta.is_connected:
        return True
    try:
        import signal as _signal

        def _timeout_handler(signum, frame):
            raise TimeoutError("delta.connect() timed out")

        old_handler = _signal.signal(_signal.SIGALRM, _timeout_handler)
        _signal.alarm(int(timeout_sec))
        try:
            delta.connect()
            return delta.is_connected
        finally:
            _signal.alarm(0)
            _signal.signal(_signal.SIGALRM, old_handler)
    except (TimeoutError, Exception) as e:
        logger.warning("Delta connect failed (%.1fs timeout): %s", timeout_sec, e)
        return False


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
        self.on_trip: Optional[callable] = None  # Emergency callback when CB trips

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
        """Check if circuit breaker should trip. Fires emergency close on trip."""
        was_tripped = self.is_tripped
        if self.daily_pnl <= -self.daily_loss_limit:
            self.is_tripped = True
            self.trip_reason = f"Daily loss ${abs(self.daily_pnl):.2f} exceeds ${self.daily_loss_limit} limit"
            logger.critical("REAL CB TRIPPED: %s", self.trip_reason)
        elif self.consecutive_losses >= self.max_consecutive_losses:
            self.is_tripped = True
            self.trip_reason = f"{self.consecutive_losses} consecutive real losses"
            logger.critical("REAL CB TRIPPED: %s", self.trip_reason)

        # Fire emergency close callback on FIRST trip (not repeated)
        if self.is_tripped and not was_tripped and self.on_trip:
            try:
                self.on_trip(self.trip_reason)
            except Exception as e:
                logger.error("REAL CB: emergency callback failed: %s", e)

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

        exec_cfg = config.get("execution", {})
        self._order_type: str = exec_cfg.get("order_type", "maker")  # "maker" | "taker" | "auto"
        self._retry_taker_on_reject: bool = exec_cfg.get("retry_taker_on_reject", True)
        self.min_margin: float = rt_cfg.get("min_margin_per_trade", 50.0)  # $50 min for fee viability  # $30 min for profitability  # $15 minimum margin per trade
        self.max_margin: float = rt_cfg.get("max_margin_per_trade", 75.0)  # $75 max for better P&L  # $50 maximum margin per trade
        self.max_open: int = rt_cfg.get("max_open_positions", 5)
        self.reserve_pct: float = rt_cfg.get("balance_reserve_pct", 15) / 100.0
        self.min_balance: float = rt_cfg.get("min_balance_to_trade", 30.0)
        self.leverage_cap: int = rt_cfg.get("leverage_cap", 75)  # raised: demo uses 20x-75x per confidence tier

        self.circuit_breaker = RealCircuitBreaker(
            daily_loss_limit=rt_cfg.get("daily_loss_limit_usd", 25.0),
            max_consecutive_losses=rt_cfg.get("max_consecutive_losses", 3)  # 3 is safe default,
        )

        # Nautilus-inspired execution engine (wraps delta_client)
        from execution.nautilus_engine import NautilusExecutionEngine
        self._nautilus_demo = None
        self._nautilus_live = None

        # Delta SDK clients (replaces ccxt for order execution)
        # DRY RUN → demo DeltaClient (testnet)
        # LIVE → live DeltaClient (production)
        self._delta_demo = DeltaClient(mode="demo")
        self._delta_live = DeltaClient(mode="live")
        self._delta_connected = False

        # Legacy ccxt demo exchange (used by _get_trading_exchange fallback)
        self._demo_exchange = None
        self._demo_connected = False

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

        # Orphan dedup: persist across cycles, cleared every 60s
        self._orphan_closed_set: set = set()
        self._orphan_closed_set_ts: float = 0

        # Signal tracker reference (set by orchestrator for scanner WR lookup)
        self._signal_tracker_ref = None

        # Load persisted state (may override enabled/dry_run from saved toggle)
        self._load_state()

        # Wire emergency close-all to circuit breaker
        self.circuit_breaker.on_trip = self._emergency_close_all

    def _resolve_post_only(self, signal: Dict[str, Any]) -> bool:
        """Return True if the entry order should be placed as post_only (maker).

        Respects the execution.order_type config:
          maker — always post_only (saves ~0.036% per entry vs taker)
          taker — always market (guaranteed fill)
          auto  — maker if signal is within the Scalper fee window, taker otherwise
        """
        if self._order_type == "maker":
            return True
        if self._order_type == "taker_only":  # explicit taker-only (no maker attempt)
            return False
        # auto: use the order_type the strategy already decided
        meta = signal.get("metadata", {})
        return meta.get("order_type", "market") == "post_only"

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

    # ==================================================================
    # Smart Qualification & Sizing (V2)
    # ==================================================================

    SMART_WHITELIST = {"BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "LTC/USDT", "DOGE/USDT", "LINK/USDT", "DOT/USDT", "TAO/USDT", "ADA/USDT"}
    SMART_GRADE_ALLOW = {"A+", "A", "B"}  # REJECT and C removed — data: conf<50 trades are instant SL hits  # REJECT has 75.8% WR -- data proves profitable  # C has 84.9% WR (highest grade!)

    def _smart_qualify(self, signal: dict) -> Tuple[bool, str]:
        """Gate every real entry through a strict qualification pipeline."""
        symbol = signal.get("symbol", "") or signal.get("metadata", {}).get("symbol", "")
        meta = signal.get("metadata", {})

        # 1. Symbol whitelist
        if symbol not in self.SMART_WHITELIST:
            logger.info("SMART QUALIFY FAIL: %s not in whitelist", symbol)
            return False, f"symbol_blocked:{symbol}"

        # 2. Daily trade limit (max 5 real trades per day)
        MAX_DAILY_TRADES = 999  # no daily limit
        if self.circuit_breaker.trade_count_today >= MAX_DAILY_TRADES:
            logger.info("SMART QUALIFY FAIL: %d/%d daily trades used", self.circuit_breaker.trade_count_today, MAX_DAILY_TRADES)
            return False, f"daily_limit:{self.circuit_breaker.trade_count_today}/{MAX_DAILY_TRADES}"

        # 3. Circuit breaker
        allowed, cb_reason = self.circuit_breaker.is_allowed()
        if not allowed:
            logger.info("SMART QUALIFY FAIL: circuit_breaker -- %s", cb_reason)
            return False, f"circuit_breaker:{cb_reason}"

        # Drawdown kill switch: auto-disable if total PnL < -25% of starting balance
        _total_pnl = self.circuit_breaker.total_pnl
        _starting = 100.0  # approximate starting balance
        if _total_pnl < -(_starting * 0.25):
            logger.critical("DRAWDOWN KILL: total_pnl=$%.2f exceeds 25%% drawdown limit — DISABLING", _total_pnl)
            self.enabled = False
            self._save_state()
            return False, f"drawdown_kill:${_total_pnl:.0f}"

        # 3. Grade filter
        grade = signal.get("grade", "") or meta.get("grade", "")
        if grade and grade not in self.SMART_GRADE_ALLOW:
            logger.info("SMART QUALIFY FAIL: grade=%s not in %s", grade, self.SMART_GRADE_ALLOW)
            return False, f"grade_blocked:{grade}"

        # 4. ML probability

        # ACTIVE PAIRS FILTER: only trade pairs we monitor
        REAL_ACTIVE_PAIRS = {"BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT"}
        if symbol not in REAL_ACTIVE_PAIRS:
            logger.info("SMART QUALIFY FAIL: %s not in active pairs %s", symbol, REAL_ACTIVE_PAIRS)
            return False, f"pair_not_active:{symbol}"

        # ML verdict filter: only trade when ML says TAKE or STRONG_TAKE
        ml_verdict = meta.get("ml_verdict", "")
        if ml_verdict and ml_verdict not in ("TAKE", "STRONG_TAKE", "WEAK", "NO_MODEL", ""):
            logger.info("SMART QUALIFY FAIL: ml_verdict=%s (need WEAK/TAKE/STRONG_TAKE)", ml_verdict)
            return False, f"ml_verdict:{ml_verdict}"

        ml_prob = meta.get("ml_probability", None)
        if ml_prob is not None and float(ml_prob) > 0 and float(ml_prob) <= 0.35:
            logger.info("SMART QUALIFY FAIL: ml_prob=%.3f <= 0.45", float(ml_prob))
            return False, f"ml_prob_low:{ml_prob}"

        # 5. Scanner win-rate check
        scanner_name = meta.get("setup_type", "") or signal.get("scanner", "")
        if scanner_name:
            wr_data = self._get_scanner_wr(scanner_name)
            if wr_data["trades"] >= 20 and wr_data["wr"] <= 65.0:
                logger.info("SMART QUALIFY FAIL: scanner=%s wr=%.1f%% (%d trades) <= 65%%",
                           scanner_name, wr_data["wr"], wr_data["trades"])
                wr_val = wr_data["wr"]
                return False, "scanner_wr_low:%s=%.1f%%" % (scanner_name, wr_val)

        # 6. Max open positions
        open_count = len(self.real_trades)
        if open_count >= self.max_open:  # use config value (currently 1)
            logger.info("SMART QUALIFY FAIL: %d/%d positions open", open_count, self.max_open)
            return False, f"max_open:{open_count}/{self.max_open}"

        # 7. Balance check
        try:
            delta = self._delta_demo if self.dry_run else self._delta_live
            if _safe_connect(delta):
                bal = float(delta.fetch_balance() or 0)
            else:
                bal = self._cached_balance or 0
        except Exception:
            bal = self._cached_balance or 0
        if bal < self.min_balance:
            logger.info("SMART QUALIFY FAIL: balance=$%.2f < min=$%.2f", bal, self.min_balance)
            return False, f"low_balance:${bal:.2f}"

        # 8. Duplicate symbol check
        side_str = _normalize_side(signal.get("side", "long"))
        for t in self.real_trades.values():
            t_sym = getattr(t, "symbol", "") if not isinstance(t, dict) else t.get("symbol", "")
            if t_sym == symbol:
                logger.info("SMART QUALIFY FAIL: %s already has open position", symbol)
                return False, f"duplicate:{symbol}"

        logger.info("SMART QUALIFY PASS: %s %s | grade=%s ml=%.3f scanner=%s",
                    symbol, side_str, grade, float(ml_prob or 0), scanner_name)
        return True, "qualified"

    def _smart_size(self, signal: dict) -> Tuple[float, int, int]:
        """Risk-based position sizing. Returns (margin, leverage, lots)."""
        from exchange.delta_client import PRODUCT_MAP

        meta = signal.get("metadata", {})
        symbol = signal.get("symbol", "") or meta.get("symbol", "")
        entry_price = signal.get("entry_price", 0)
        sl_price = signal.get("stop_loss", 0)

        # Get balance
        try:
            delta = self._delta_demo if self.dry_run else self._delta_live
            if _safe_connect(delta):
                balance = float(delta.fetch_balance() or 0)
                self._cached_balance = balance
                self._balance_ts = time.time()
            else:
                balance = self._cached_balance or 0
        except Exception:
            balance = self._cached_balance or 0

        usable = balance * (1 - self.reserve_pct)

        # SL distance as percentage
        if entry_price > 0 and sl_price > 0:
            sl_distance_pct = abs(entry_price - sl_price) / entry_price
        else:
            sl_distance_pct = 0.02  # default 2%

        # Direct margin targeting: $15-$50 based on confidence and balance
        # Higher confidence = higher margin allocation
        confidence = signal.get("confidence", 70)
        grade = signal.get("grade", "C")
        
        # Grade-based margin tiers (raised for fee viability)
        if grade in ("A+",):
            target_margin = 75.0   # max conviction — full size
        elif grade in ("A",):
            target_margin = 65.0   # high conviction
        elif grade in ("B",):
            target_margin = 55.0   # standard
        else:
            target_margin = 50.0   # minimum — still fee-viable
        
        # Cap to balance limits
        margin = min(target_margin, self.max_margin)
        margin = min(margin, usable * 0.45)  # max 45% of usable per trade
        margin = max(margin, self.min_margin)  # at least $15

        # margin is already >= min_margin from max() above

        # Leverage from paper signal (mirrors what paper used)
        # Paper's signal_tracker already computed optimal leverage per confidence tier
        paper_lev = signal.get("leverage", 20)
        leverage = min(paper_lev, self.leverage_cap)
        leverage = max(leverage, 20)  # minimum 20x for meaningful position

        # Lots from contract size
        contract_size = PRODUCT_MAP.get(symbol, {}).get("contract_size", 1.0)
        notional = margin * leverage
        lot_value = entry_price * contract_size if entry_price > 0 else 1
        lots = max(1, int(notional / lot_value)) if lot_value > 0 else 0

        logger.info(
            "SMART SIZE: %s | bal=$%.2f usable=$%.2f risk=$%.2f | "
            "SL_dist=%.3f%% pos_usd=$%.0f | margin=$%.2f lev=%dx lots=%d | notional=$%.0f",
            symbol, balance, usable, margin,
            sl_distance_pct * 100, notional, margin, leverage, lots, notional,
        )

        # --- FIX 4: Slippage-aware sizing ---
        # Reduce margin if recent slippage > 8bp on this symbol
        _recent_slip = getattr(self, '_recent_slippage', {}).get(symbol, 0)
        if _recent_slip > 8:
            _slip_factor = max(0.5, 1.0 - (_recent_slip - 8) / 50)  # 8bp=1.0x, 18bp=0.8x, 58bp=0.0x
            margin = margin * _slip_factor
            lots = max(1, int(lots * _slip_factor))
            logger.info("SLIP SIZE ADJ: %s recent_slip=%.0fbp → factor=%.2f → margin=$%.2f lots=%d",
                       symbol, _recent_slip, _slip_factor, margin, lots)

        return margin, leverage, lots

    def _get_nautilus(self):
        """Lazy-init NautilusExecutionEngine."""
        from execution.nautilus_engine import NautilusExecutionEngine
        if self.dry_run:
            if self._nautilus_demo is None:
                self._nautilus_demo = NautilusExecutionEngine(self._delta_demo, mode="demo")
            return self._nautilus_demo
        else:
            if self._nautilus_live is None:
                self._nautilus_live = NautilusExecutionEngine(self._delta_live, mode="live")
            return self._nautilus_live

    def _execute_real_entry(
        self, symbol: str, side: str, lots: int, leverage: int,
        entry_price: float, sl: float, tp: float, coid: str = None,
        grade: str = "", confidence: int = 0, signal: dict = None,
    ) -> Tuple[dict, float]:
        """Execute entry via PURE MAKER limit order at best bid/ask.

        Architecture v2:
        - Get live bid/ask from exchange
        - Place limit at best_bid+tick (long) or best_ask-tick (short)
        - post_only=True — if would cross spread, order is rejected (not filled as taker)
        - Wait 5 seconds for fill
        - If not filled → SKIP trade (missing costs $0, taker costs $0.06+)
        - Atomic bracket: entry + SL (mark_price trigger) + TP in one call
        """
        from exchange.delta_client import PRODUCT_MAP

        delta = self._delta_demo if self.dry_run else self._delta_live
        if not _safe_connect(delta):
            return {"error": "delta_connect_failed"}, 0.0

        # Set leverage
        try:
            delta.set_leverage(symbol, leverage)
        except Exception as e:
            logger.warning("REAL ENTRY: set_leverage failed: %s (continuing)", e)

        # Set isolated margin mode
        try:
            delta.set_margin_mode("isolated")
        except Exception:
            pass

        # --- FIX 1: Smart limit price at best bid/ask ---
        product_info = PRODUCT_MAP.get(symbol, {})
        tick = product_info.get("tick_size", 0.01)

        # Get live bid/ask for smart pricing
        smart_price = entry_price  # fallback to signal price
        try:
            ticker = delta.get_ticker(symbol)
            if ticker:
                best_bid = float(ticker.get("best_bid", 0) or ticker.get("bid", 0) or 0)
                best_ask = float(ticker.get("best_ask", 0) or ticker.get("ask", 0) or 0)
                if best_bid > 0 and best_ask > 0:
                    spread = best_ask - best_bid
                    # Regime-aware limit offset
                    _sig_meta = signal.get("metadata", {}) if hasattr(signal, 'get') else {}
                    _regime = _sig_meta.get("regime", "") if isinstance(_sig_meta, dict) else ""
                    if _regime in ("trending_up", "trending_down", "breakout"):
                        _offset_mult = 0.04  # tighter in trends
                    elif _regime in ("ranging", "sideways", "volatile", "high_volatility"):
                        _offset_mult = 0.07  # wider for wick capture
                    else:
                        _offset_mult = 0.05  # default

                    # Get ATR from signal metadata
                    _sig_atr = float(_sig_meta.get("atr", 0)) if isinstance(_sig_meta, dict) else 0
                    _limit_offset = _sig_atr * _offset_mult if _sig_atr > 0 else spread * 0.5

                    # Cap: never place more than 1.2 * ATR from signal
                    if _sig_atr > 0:
                        _limit_offset = min(_limit_offset, _sig_atr * 1.2)

                    if side == "buy":
                        smart_price = best_bid + min(_limit_offset, spread * 0.3)
                    else:
                        smart_price = best_ask - min(_limit_offset, spread * 0.3)

                    smart_price = round(smart_price / tick) * tick

                    # Anti-chase: reject if price moved > 0.4 ATR from signal
                    if _sig_atr > 0:
                        _chase_dist = abs(smart_price - entry_price) / _sig_atr
                        if _chase_dist > 0.4:
                            logger.warning("ANTI-CHASE: %s price moved %.1f ATR from signal — SKIPPING", symbol, _chase_dist)
                            return {"error": "anti_chase_skip"}, 0

                    # Slippage check: if smart_price is >8bp from signal, log warning
                    slip_bp = abs(smart_price - entry_price) / entry_price * 10000
                    if slip_bp > 8:
                        logger.warning("REAL ENTRY: Smart price %.4f is %.1fbp from signal %.4f — market moved",
                                      smart_price, slip_bp, entry_price)

                    logger.info("REAL ENTRY [SMART PRICE]: %s %s | bid=%.4f ask=%.4f signal=%.4f → limit=%.4f (%.1fbp from signal)",
                               symbol, side, best_bid, best_ask, entry_price, smart_price, slip_bp)
        except Exception as e:
            logger.warning("REAL ENTRY: ticker failed, using signal price: %s", e)

        # --- FIX 3: Atomic bracket order with mark_price SL ---
        # SL SANITY CHECK: SL must be on correct side of entry
        if side == "buy" and sl >= smart_price:
            logger.error("SL SANITY FAIL: %s LONG but SL %.4f >= price %.4f — BLOCKED", symbol, sl, smart_price)
            return {"error": "sl_above_entry_for_long"}, 0
        if side == "sell" and sl <= smart_price:
            logger.error("SL SANITY FAIL: %s SHORT but SL %.4f <= price %.4f — BLOCKED", symbol, sl, smart_price)
            return {"error": "sl_below_entry_for_short"}, 0

        order = delta.place_bracket_order(
            symbol=symbol,
            side=side,
            lots=lots,
            stop_loss_price=sl,
            take_profit_price=tp,
            limit_price=0,  # market order — bracket SL/TP/trail all created atomically
            client_order_id=coid,
            post_only=False,
            time_in_force="",
            trail_amount=abs(entry_price - sl) if entry_price > 0 and sl > 0 else 0,  # native Delta trailing
        )

        # Check both no-error AND actually filled (IOC may cancel instantly)
        order_state = order.get("state", "") if order else ""
        order_filled = order and not order.get("error") and order_state not in ("cancelled", "rejected")
        if order and not order_filled:
            logger.warning("REAL ENTRY: Order returned but state=%s — treating as NOT filled", order_state)
        if order_filled:
            # Bracket succeeded
            try:
                delta.enable_auto_topup(symbol)
            except Exception:
                pass
            fill = float(order.get("average_fill_price", 0) or 0)
            logger.info(
                "REAL ENTRY [BRACKET]: %s %s | lots=%d lev=%dx | fill=%.4f | SL=%.4f TP=%.4f",
                symbol, side, lots, leverage, fill, sl, tp,
            )

            # NATIVE TRAILING STOP: check if bracket actually created the trail
            # bracket_order field in response tells us if it worked
            _bracket_created = order.get("bracket_order") is not None if order else False
            if _bracket_created:
                logger.info("REAL TRAIL: bracket_trail_amount ACTIVE (Delta native trailing)")
            else:
                logger.warning("REAL TRAIL: bracket FAILED — trail NOT active, SL placed separately")

            # SL COVERAGE CHECK: ensure ALL lots on exchange have SL protection
            try:
                import time as _tc
                _tc.sleep(1)
                product_id = delta._get_product_id(symbol)
                # Get total position size on exchange
                all_pos = delta.get_all_positions()
                total_lots = 0
                for _p in all_pos:
                    _pid = _p.get("product", {}).get("id", 0)
                    if _pid == product_id:
                        total_lots = abs(int(float(_p.get("size", 0))))
                        break
                
                # Get total SL coverage
                sl_covered = 0
                ex_orders = delta.get_open_orders()
                for _o in ex_orders:
                    _opid = _o.get("product", {}).get("id", 0)
                    if _opid == product_id and _o.get("stop_order_type") == "stop_loss_order":
                        sl_covered += int(_o.get("size", 0) or 0)
                
                gap = total_lots - sl_covered
                if gap > 0:
                    # Unprotected lots! Place additional SL
                    close_side = "sell" if side == "buy" else "buy"
                    logger.warning("SL COVERAGE GAP: %s has %d lots but only %d covered — placing SL for %d more",
                                   symbol, total_lots, sl_covered, gap)
                    delta._client.place_stop_order(
                        product_id=product_id,
                        size=gap,
                        side=close_side,
                        stop_price=str(sl),
                        order_type=delta._OrderType.MARKET,
                        isTrailingStopLoss=False,
                    )
                    logger.info("SL COVERAGE FIXED: %s +%d lots @ %.4f — all %d lots now protected",
                               symbol, gap, sl, total_lots)
                else:
                    logger.info("SL COVERAGE OK: %s %d/%d lots covered", symbol, sl_covered, total_lots)
            except Exception as _cov_err:
                logger.warning("SL COVERAGE CHECK failed for %s: %s", symbol, _cov_err)

            # ORDER TIMEOUT: If limit order is pending (not filled), wait 10s then cancel
            if order.get("state") == "open" and fill <= 0:
                logger.info("REAL ENTRY: IOC order pending — checking fill in 2s...")
                import time as _tw
                _tw.sleep(3)
                # Check if filled
                try:
                    product_id = delta._get_product_id(symbol)
                    pos = delta.get_all_positions()
                    pos_found = any(
                        p.get("product", {}).get("id") == product_id and abs(float(p.get("size", 0))) > 0
                        for p in pos
                    )
                    if pos_found:
                        # Filled! Get the fill price
                        for p in pos:
                            if p.get("product", {}).get("id") == product_id:
                                fill = float(p.get("entry_price", 0))
                                break
                        logger.info("REAL ENTRY: Limit filled after wait — fill=%.4f", fill)
                    else:
                        # --- FIX 1: SKIP trade — no taker fallback ---
                        # Missing a trade costs $0. Taker costs $0.06+ per trade.
                        # For Grade A: taker fallback (cancel maker first)
                        order_id = order.get("id")
                        if order_id and (grade in ("A+", "A") or confidence >= 85):
                            # Only cancel for taker fallback — Grade A signals
                            try:
                                delta._client.cancel_order(product_id=product_id, order_id=order_id)
                            except Exception:
                                pass

                            # ORPHAN PREVENTION: verify no partial fill after cancel
                            import time as _tv
                            _tv.sleep(0.5)
                            try:
                                _pos_check = delta.get_all_positions()
                                _partial_size = 0
                                for _pc in _pos_check:
                                    if _pc.get("product", {}).get("id") == product_id:
                                        _partial_size = abs(int(float(_pc.get("size", 0))))

                                if _partial_size > 0:
                                    # PARTIAL FILL DETECTED — attach SL immediately
                                    logger.warning("ORPHAN PREVENTION: %s partial fill %d lots detected after cancel — attaching SL", symbol, _partial_size)
                                    close_side = "sell" if side == "buy" else "buy"
                                    try:
                                        delta._client.place_stop_order(
                                            product_id=product_id,
                                            size=_partial_size,
                                            side=close_side,
                                            stop_price=str(sl),
                                            order_type=delta._OrderType.MARKET,
                                            isTrailingStopLoss=False,
                                        )
                                        logger.info("ORPHAN PREVENTION: SL placed at %.4f for %d lots on %s", sl, _partial_size, symbol)
                                    except Exception as _sl_err:
                                        # SL failed — emergency market close
                                        logger.error("ORPHAN PREVENTION: SL failed, emergency close: %s", _sl_err)
                                        try:
                                            delta._client.place_order(
                                                product_id=product_id,
                                                size=_partial_size,
                                                side=close_side,
                                                order_type=delta._OrderType.MARKET,
                                                reduce_only=True,
                                            )
                                            logger.info("ORPHAN PREVENTION: Emergency closed %d lots on %s", _partial_size, symbol)
                                        except Exception:
                                            pass
                            except Exception as _ver_err:
                                logger.warning("ORPHAN PREVENTION: verify failed: %s", _ver_err)

                            # Cancel any bracket SL/TP from the unfilled order
                            try:
                                ex_orders2 = delta.get_open_orders()
                                for _o2 in ex_orders2:
                                    if _o2.get("product", {}).get("id") == product_id:
                                        try:
                                            delta._client.cancel_order(product_id=product_id, order_id=_o2.get("id"))
                                        except Exception:
                                            pass
                            except Exception:
                                pass
                        # Smart approach: DON'T cancel the maker order — let it fill naturally
                        # Cancelling creates orphans when Delta partially fills during cancel
                        # Grade A: taker fallback (guaranteed fill, worth the fee)
                        # Others: leave maker order alive (will fill or expire on Delta's end)
                        if grade in ("A+", "A"):  # STRICT: only A+/A get taker (was also conf>=85)
                            logger.info("REAL ENTRY: Maker not filled — TAKER FALLBACK (Grade %s, conf=%d)", grade, confidence)
                            import time as _tf2
                            _tf2.sleep(0.3)
                            taker_order = delta.place_bracket_order(
                                symbol=symbol, side=side, lots=lots,
                                stop_loss_price=sl, take_profit_price=tp,
                                limit_price=0, client_order_id=coid,
                                post_only=False,
                            )
                            if taker_order and not taker_order.get("error"):
                                fill = float(taker_order.get("average_fill_price", 0) or 0)
                                logger.info("REAL ENTRY [TAKER]: %s %s lots=%d fill=%.4f (Grade %s)", symbol, side, lots, fill, grade)
                                order = taker_order
                            else:
                                logger.error("REAL ENTRY: Taker fallback failed: %s", taker_order)
                                return {"error": "taker_fallback_failed"}, 0
                        else:
                            # DON'T cancel — leave maker order alive on Delta
                            # It will either fill naturally or expire via Delta's order expiry
                            logger.info("REAL ENTRY: Maker not filled 8s — LEAVING order alive (Grade %s, no cancel = no orphan)", grade, confidence)
                            return {"error": "maker_pending_no_cancel"}, 0
                except Exception as _tw_err:
                    logger.warning("REAL ENTRY: Timeout check failed: %s", _tw_err)

            # CRITICAL: if fill is 0, order didn't actually execute
            if fill <= 0:
                logger.warning("REAL ENTRY: Bracket returned fill=0 — order likely cancelled/unfilled")
                return {"error": "bracket_no_fill"}, 0.0

            return order, fill

        # Bracket failed -- fallback to market + separate SL
        logger.warning("REAL ENTRY: Bracket failed (%s) -- fallback to market+SL",
                       order.get("error", "unknown") if order else "no_response")

        market_order = delta.place_market_order(
            symbol=symbol, side=side, lots=lots, client_order_id=coid,
        )
        if not market_order or market_order.get("error"):
            err = market_order.get("error", "unknown") if market_order else "no_response"
            logger.error("REAL ENTRY: Market order FAILED: %s", err)
            return {"error": str(err)}, 0.0

        fill = float(market_order.get("average_fill_price", 0) or 0)
        logger.info("REAL ENTRY [MARKET]: %s %s | lots=%d | fill=%.4f", symbol, side, lots, fill)

        # Wait for position to settle
        import time as _time
        time.sleep(2)

        # Place SL (3 retries, emergency close if all fail)
        close_side = "sell" if side == "buy" else "buy"
        sl_placed = False
        for attempt in range(3):
            try:
                sl_result = delta.place_stop_loss(symbol, close_side, lots, sl)
                if sl_result and not sl_result.get("error"):
                    sl_placed = True
                    logger.info("REAL ENTRY [SL]: %s @ %.4f | attempt=%d", symbol, sl, attempt + 1)
                    # Also place TP
                    if tp > 0:
                        try:
                            tp_result = delta.place_take_profit(symbol, close_side, lots, tp)
                            if tp_result and not tp_result.get("error"):
                                logger.info("REAL ENTRY [TP]: %s @ %.4f", symbol, tp)
                            else:
                                logger.warning("REAL ENTRY [TP] FAILED: %s", tp_result)
                        except Exception as tp_err:
                            logger.warning("REAL ENTRY [TP] error: %s", tp_err)
                    break
            except Exception as sl_err:
                logger.warning("REAL ENTRY: SL attempt %d failed: %s", attempt + 1, sl_err)
                time.sleep(1)

        if not sl_placed:
            logger.critical(
                "REAL ENTRY: SL FAILED 3x for %s %s %d lots -- EMERGENCY CLOSE",
                symbol, side, lots,
            )
            try:
                delta._client.create_order({
                    "product_id": delta._get_product_id(symbol),
                    "size": lots,
                    "side": close_side,
                    "order_type": "market_order",
                    "reduce_only": "true",
                })
                logger.warning("REAL ENTRY: Emergency close executed for %s", symbol)
            except Exception:
                logger.critical("REAL ENTRY: EMERGENCY CLOSE ALSO FAILED -- MANUAL INTERVENTION NEEDED")
            return {"error": "sl_failed_emergency_close"}, 0.0

        # Enable auto-topup
        try:
            delta.enable_auto_topup(symbol)
        except Exception:
            pass

        market_order["bracket_fallback"] = True
        return market_order, fill

    def _cancel_symbol_orders(self, symbol: str):
        """Cancel all open SL/TP orders for a symbol."""
        try:
            delta = self._delta_demo if self.dry_run else self._delta_live
            if not _safe_connect(delta):
                return
            product_id = delta._get_product_id(symbol)
            if not product_id:
                return
            ex_orders = delta.get_open_orders()
            for o in ex_orders:
                o_pid = o.get("product", {}).get("id", 0)
                o_type = o.get("stop_order_type", "")
                if o_pid == product_id and o_type in ("stop_loss_order", "take_profit_order"):
                    try:
                        delta._client.cancel_order(product_id=product_id, order_id=o.get("id"))
                        logger.info("CANCEL ORDER: %s %s (order %s)", symbol, o_type, o.get("id"))
                    except Exception as e:
                        logger.warning("CANCEL ORDER failed: %s %s -- %s", symbol, o.get("id"), e)
        except Exception as e:
            logger.debug("_cancel_symbol_orders error for %s: %s", symbol, e)

    def _get_scanner_wr(self, scanner_name: str) -> dict:
        """Get win-rate stats for a scanner from the signal tracker."""
        try:
            if self._signal_tracker_ref is not None:
                tracker = self._signal_tracker_ref
                # Try get_scanner_stats or get_paper_stats methods
                if hasattr(tracker, "get_scanner_stats"):
                    stats = tracker.get_scanner_stats(scanner_name)
                    if stats:
                        return {
                            "trades": int(stats.get("total", stats.get("trades", 0))),
                            "wr": float(stats.get("win_rate", stats.get("wr", 100))),
                        }
                if hasattr(tracker, "scanner_stats"):
                    stats = tracker.scanner_stats.get(scanner_name, {})
                    if stats:
                        return {
                            "trades": int(stats.get("total", stats.get("trades", 0))),
                            "wr": float(stats.get("win_rate", stats.get("wr", 100))),
                        }
        except Exception as e:
            logger.debug("_get_scanner_wr error for %s: %s", scanner_name, e)
        return {"trades": 0, "wr": 100.0}

    # ==================================================================
    # Mirror Entry
    # ==================================================================

    async def mirror_paper_trade(
        self, symbol: str, signal: Dict[str, Any], paper_trade_id: Optional[str] = None,
    ) -> Optional[Dict]:
        """
        Mirror a paper trade to the real exchange (Smart V2).

        Returns dict with mirror result, or None if skipped/failed.
        Paper trading is NEVER affected by this method.
        """
        if not self.enabled:
            return {"status": "disabled"}

        # Inject symbol into signal if missing
        if "symbol" not in signal:
            signal["symbol"] = symbol

        # Dedup: skip if this paper trade already mirrored
        if paper_trade_id and paper_trade_id in self.paper_to_real:
            return {"status": "already_mirrored", "existing_id": self.paper_to_real[paper_trade_id]}

        # -- Smart Qualification --
        qualified, qual_reason = self._smart_qualify(signal)
        try:
            from bot.signal_journey import SignalJourney as _SJ
            _SJ.stamp(signal, "real_qualify", passed=qualified, reason=qual_reason)
        except Exception:
            pass
        try:
            from bot import pipeline_metrics as _pm
            if qualified:
                _pm.record_real_pass()
            else:
                _pm.record_real_reject(qual_reason)
        except Exception:
            pass
        if not qualified:
            logger.info("REAL SKIP: %s %s -- %s", symbol, signal.get("side", "?"), qual_reason)
            return {"status": "skipped", "reason": qual_reason}

        logger.info("SMART: QUALIFY PASSED, entering sizing for %s", symbol)
        # -- Smart Sizing --
        # Smart Sizing (with error catch)
        try:
            margin, leverage, lots = self._smart_size(signal)
        except Exception as _se:
            logger.error("SMART SIZE ERROR: %s -- %s", symbol, _se, exc_info=True)
            return {"status": "failed", "reason": str(_se)}
        if lots <= 0 or margin <= 0:
            logger.warning("REAL SKIP: %s -- sizing failed (margin=$%.2f lots=%d)", symbol, margin, lots)
            return {"status": "skipped", "reason": "sizing_failed"}

        # -- Build order params --
        meta = signal.get("metadata", {})
        sl = signal.get("stop_loss", 0)
        # REAL TRADE: widen SL by 0.15% buffer for execution latency
        # Paper exits instantly (0ms), real has 200-500ms API delay = noise stopouts
        entry_raw = signal.get("entry_price", 0)
        side_raw = signal.get("side", "long")
        if sl > 0 and entry_raw > 0 and not self.dry_run:
            buffer = entry_raw * 0.0015  # 0.15% buffer
            if side_raw in ("long", "buy"):
                sl = sl - buffer  # widen down for long
            else:
                sl = sl + buffer  # widen up for short
            logger.info("REAL SL BUFFER: %s %s | paper_sl=%.4f real_sl=%.4f (buffer=%.4f)",
                        signal.get("symbol"), side_raw, signal.get("stop_loss"), sl, buffer)
        tps = signal.get("take_profits", [])
        tp = float(tps[0]) if tps and isinstance(tps[0], (int, float)) else 0
        side_str = _normalize_side(signal.get("side", "long"))
        order_side = "buy" if side_str == "long" else "sell"
        entry_price = signal.get("entry_price", 0)
        coid = paper_trade_id[:32] if paper_trade_id else None

        # -- Price freshness: skip if price already moved too far --
        try:
            delta = self._delta_demo if self.dry_run else self._delta_live
            if _safe_connect(delta):
                _ticker = delta.get_ticker(symbol)
                if _ticker:
                    _bid = float(_ticker.get("best_bid", 0) or 0)
                    _ask = float(_ticker.get("best_ask", 0) or 0)
                    _mid = (_bid + _ask) / 2 if _bid > 0 and _ask > 0 else 0
                    if _mid > 0 and entry_price > 0:
                        _dev_bp = abs(_mid - entry_price) / entry_price * 10000
                        if _dev_bp > 25:
                            logger.warning("REAL SKIP: %s — price moved %.0fbp (signal=%.4f mid=%.4f bid=%.4f ask=%.4f)",
                                          symbol, _dev_bp, entry_price, _mid, _bid, _ask)
                            return {"status": "skipped", "reason": f"price_stale_{_dev_bp:.0f}bp"}
                        logger.info("REAL PRICE FRESH: %s | signal=%.4f mid=%.4f dev=%.0fbp | bid=%.4f ask=%.4f",
                                   symbol, entry_price, _mid, _dev_bp, _bid, _ask)
        except Exception as _pe:
            logger.debug("Price check failed: %s", _pe)

        # -- Execute Entry --
        try:
            _grade = signal.get("grade", "") or meta.get("grade", "")
            _conf = int(signal.get("confidence", 0) or meta.get("confidence", 0))
            order, fill_price = self._execute_real_entry(
                symbol=symbol, side=order_side, lots=lots, entry_price=entry_price,
                leverage=leverage, sl=sl, tp=tp, coid=coid,
                grade=_grade, confidence=_conf, signal=signal,
            )

            if order.get("error"):
                # Clean up any orphan orders from failed entry
                try:
                    delta = self._delta_live if not self.dry_run else self._delta_demo
                    if _safe_connect(delta):
                        product_id = delta._get_product_id(symbol)
                        for o in delta.get_open_orders():
                            if o.get("product", {}).get("id") == product_id:
                                delta._client.cancel_order(product_id=product_id, order_id=o.get("id"))
                                logger.info("REAL: Cleaned orphan order after failed entry: %s", o.get("id"))
                except Exception:
                    pass
                logger.error("REAL ENTRY FAILED: %s -- %s", symbol, order["error"])
                self._api_failures += 1
                if self._api_failures >= self._max_api_failures:
                    self.enabled = False
                    logger.critical("REAL TRADING AUTO-DISABLED: %d API failures", self._api_failures)
                return {"status": "failed", "reason": order["error"]}

            if fill_price <= 0:
                fill_price = entry_price

            # Slippage
            slippage_bps = abs(fill_price - entry_price) / entry_price * 10000 if entry_price > 0 else 0

            # Max slippage guard: if fill > 40bp from signal, emergency close
            if slippage_bps > 40 and not self.dry_run:
                logger.critical("REAL ENTRY: EXCESSIVE SLIPPAGE %.0fbp (signal=%.4f fill=%.4f) — CLOSING",
                               slippage_bps, entry_price, fill_price)
                try:
                    close_side = "sell" if side_str == "long" else "buy"
                    delta._client.create_order({
                        "product_id": delta._get_product_id(symbol),
                        "size": lots, "side": close_side,
                        "order_type": "market_order", "reduce_only": "true",
                    })
                    logger.info("REAL SLIPPAGE CLOSE: %s closed to prevent loss", symbol)
                except Exception as _sc:
                    logger.error("REAL SLIPPAGE CLOSE failed: %s", _sc)
                return {"error": f"slippage_{slippage_bps:.0f}bp"}, fill_price

            # Build trade tracking object
            order_id = order.get("id", order.get("order_id", ""))
            mode_prefix = "demo" if self.dry_run else "live"
            trade_id = "%s_%s" % (mode_prefix, order_id or str(int(time.time())))

            trade = type("LiveTrade", (), {
                "trade_id": trade_id,
                "symbol": symbol,
                "side": side_str,
                "entry_price": fill_price,
                "stop_loss": sl,
                "tp1": tp,
                "tp2": float(tps[1]) if len(tps) > 1 and isinstance(tps[1], (int, float)) else 0,
                "tp3": float(tps[2]) if len(tps) > 2 and isinstance(tps[2], (int, float)) else 0,
                "position_size": lots,
                "margin": margin,
                "leverage": leverage,
                "status": "open",
                "opened_at": time.time(),
                "scanner": meta.get("setup_type", ""),
                "trade_type": meta.get("trade_type_override", "") or "SCALP",  # must be SCALP/INTRADAY/RUNNER
                "confidence": signal.get("confidence", 0),
                "ml_prob": meta.get("ml_probability", 0),
                "ml_verdict": meta.get("ml_verdict", ""),
                "regime": meta.get("regime", ""),
                "paper_trade_id": paper_trade_id or "",
                "client_order_id": coid or "",
                "current_price": fill_price,
                "slippage_bps": slippage_bps,
                "grade": signal.get("grade", "") or meta.get("grade", ""),
                # PARALLEL EXIT: independent tracking from real fill price
                "initial_risk": abs(fill_price - sl) if fill_price > 0 and sl > 0 else 0,
                "highest_price": fill_price,
                "lowest_price": fill_price,
                "peak_mfe_r": 0.0,
                "mfe_stale_seconds": 0.0,
                "last_mfe_update_time": time.time(),
                "momentum_decay_count": 0,
                "breakeven_set": False,
                "chandelier_stop": 0.0,
                "entry_atr": float(meta.get("atr", 0) or signal.get("signal_atr", 0) or 0),
                "independent_exit": True,  # flag: this trade manages its own exits
            })()

            # Store in tracking maps
            self.real_trades[trade_id] = trade
            if paper_trade_id:
                self.paper_to_real[paper_trade_id] = trade_id
            self._save_state()
            self._api_failures = 0

            # Journey: stamp successful real entry
            try:
                from bot.signal_journey import SignalJourney as _SJ
                _SJ.stamp(signal, "real_exec", passed=True,
                          reason=f"fill={fill_price:.4f}_slip={slippage_bps:.0f}bp_id={trade_id[:16]}")
            except Exception:
                pass

            _sl_dist_bp = abs(fill_price - sl) / fill_price * 10000 if fill_price > 0 and sl > 0 else 0
            mode_tag = "DRY RUN" if self.dry_run else "LIVE"
            logger.info(
                "REAL ENTRY [%s]: %s %s | margin=$%.2f lev=%dx lots=%d | "
                "SL=%.4f TP=%.4f SL_dist=%.0fbp | signal=%.4f fill=%.4f slip=%.1fbps | id=%s",
                mode_tag, symbol, side_str, margin, leverage, lots,
                sl, tp, _sl_dist_bp, entry_price, fill_price, slippage_bps, trade_id,
            )

            return {
                "status": "dry_run" if self.dry_run else "mirrored",
                "trade_id": trade_id,
                "symbol": symbol,
                "side": side_str,
                "margin": margin,
                "leverage": leverage,
                "position_size": lots,
                "fill_price": fill_price,
                "slippage_bps": round(slippage_bps, 2),
                "paper_trade_id": paper_trade_id,
            }

        except Exception as e:
            self._api_failures += 1
            logger.error("REAL ERROR: %s -- %s (failure %d/%d)",
                        symbol, e, self._api_failures, self._max_api_failures)
            if self._api_failures >= self._max_api_failures:
                self.enabled = False
                logger.critical("REAL TRADING AUTO-DISABLED: %d consecutive API failures", self._api_failures)
            return {"status": "failed", "reason": str(e)}

    # ==================================================================
    # Mirror Exit
    # ==================================================================

    async def mirror_paper_exit(
        self, paper_trade_id: str, exit_price: float, reason: str,
        paper_slippage_bps: float = 0.0,
        symbol: str = "", side: str = "",
    ) -> Optional[Dict]:
        """
        Close the real position when paper trade closes (Smart V2).
        Uses actual exchange fills for PnL, not paper math.
        """
        # 1. Find real trade via paper_to_real mapping
        real_trade_id = self.paper_to_real.get(paper_trade_id)

        # 2. Fallback: search real_trades by paper_trade_id attribute
        if not real_trade_id:
            for tid, t in list(self.real_trades.items()):
                if getattr(t, "paper_trade_id", "") == paper_trade_id:
                    real_trade_id = tid
                    break

        # 3. Fallback: match by symbol+side from kwargs
        if not real_trade_id and (symbol or side):
            norm_side = _normalize_side(side) if side else None
            for tid, t in list(self.real_trades.items()):
                t_sym = getattr(t, "symbol", "") if not isinstance(t, dict) else t.get("symbol", "")
                t_side = _normalize_side(getattr(t, "side", ""))
                t_status = getattr(t, "status", "open")
                if t_status not in ("open", "active", ""):
                    continue
                if symbol and t_sym != symbol:
                    continue
                if norm_side and t_side != norm_side:
                    continue
                real_trade_id = tid
                logger.info("REAL EXIT: Fallback symbol match -- paper=%s -> real=%s",
                           paper_trade_id[:12] if paper_trade_id else "?", tid[:12])
                break

        if not real_trade_id:
            logger.debug("REAL EXIT: No matching real trade for paper=%s", paper_trade_id[:12] if paper_trade_id else "?")
            return None

        trade = self.real_trades.get(real_trade_id)
        if not trade:
            return None

        # Extract trade attributes
        if isinstance(trade, dict):
            side_str = _normalize_side(trade.get("side", "long"))
            entry_p = trade.get("entry_price", 0)
            pos_size = trade.get("position_size", 0)
            t_margin = trade.get("margin", 0)
            t_leverage = trade.get("leverage", 1)
            t_symbol = trade.get("symbol", "?")
        else:
            side_str = _normalize_side(getattr(trade, "side", "long"))
            entry_p = getattr(trade, "entry_price", 0)
            pos_size = getattr(trade, "position_size", 0)
            t_margin = getattr(trade, "margin", 0)
            t_leverage = getattr(trade, "leverage", 1)
            t_symbol = getattr(trade, "symbol", "?")

        lots = int(pos_size) if pos_size else 1
        close_side = "sell" if side_str == "long" else "buy"

        # 4. Connect to delta and close position
        try:
            delta = self._delta_demo if self.dry_run else self._delta_live
            if not _safe_connect(delta):
                logger.error("REAL EXIT: Delta connect failed for %s", t_symbol)
                return {"status": "exit_failed", "reason": "delta_connect_failed"}

            # 5. Close position with reduce_only market order
            close_result = delta._client.create_order({
                "product_id": delta._get_product_id(t_symbol),
                "size": lots,
                "side": close_side,
                "order_type": "market_order",
                "reduce_only": "true",
            })

            # 6. Get actual fill price
            fill_price = float(close_result.get("average_fill_price", exit_price) or exit_price)
            if fill_price <= 0:
                fill_price = exit_price

            # 7. Calculate PnL from actual fills
            if side_str == "long":
                pnl_pct = (fill_price - entry_p) / entry_p if entry_p > 0 else 0
            else:
                pnl_pct = (entry_p - fill_price) / entry_p if entry_p > 0 else 0

            pnl_usd = pnl_pct * t_margin * t_leverage
            fee = float(close_result.get("paid_commission", 0) or 0)
            net_pnl = pnl_usd - fee

            # Cap: never lose more than margin
            if t_margin > 0:
                net_pnl = max(net_pnl, -t_margin)

            # 8. Cancel orphan SL/TP orders
            self._cancel_symbol_orders(t_symbol)

            # 9. Record in circuit breaker
            self.circuit_breaker.record_trade(net_pnl)

            # 10. Record closed trade
            self._record_closed_trade(trade, fill_price, net_pnl, reason, dry_run=self.dry_run)

            # 11. Remove from tracking
            self.real_trades.pop(real_trade_id, None)
            self.paper_to_real.pop(paper_trade_id, None)

            # 12. Save state
            self._save_state()

            mode_tag = "DRY RUN" if self.dry_run else "LIVE"
            logger.info(
                "REAL EXIT [%s]: %s %s | entry=%.4f exit=%.4f | "
                "gross=$%.2f fee=$%.2f net=$%.2f | margin=$%.2f lev=%dx | %s",
                mode_tag, t_symbol, side_str, entry_p, fill_price,
                pnl_usd, fee, net_pnl, t_margin, t_leverage, reason,
            )

            return {"status": "exited", "pnl_usd": net_pnl, "trade_id": real_trade_id, "reason": reason}

        except Exception as e:
            logger.critical("REAL EXIT FAILED: %s -- %s | EMERGENCY CLOSE", t_symbol, e)
            # Emergency: try raw close
            try:
                delta = self._delta_demo if self.dry_run else self._delta_live
                pid = delta._get_product_id(t_symbol)
                delta._client.create_order({
                    "product_id": pid, "size": lots,
                    "side": close_side, "order_type": "market_order", "reduce_only": "true"
                })
                logger.warning("REAL EXIT: Emergency close succeeded for %s", t_symbol)
                self.real_trades.pop(real_trade_id, None)
                self.paper_to_real.pop(paper_trade_id, None)
                self._save_state()
            except Exception as e2:
                logger.critical("REAL EXIT: EMERGENCY CLOSE ALSO FAILED: %s -- MANUAL INTERVENTION", e2)
            return {"status": "exit_failed", "reason": str(e)}

    # ==================================================================
    # Pre-Flight Safety Checks
    # ==================================================================

    # Symbols allowed for live trading (proven liquidity + correct product IDs)
    LIVE_ALLOWED_SYMBOLS = {"BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "LTC/USDT", "DOGE/USDT", "LINK/USDT", "DOT/USDT", "TAO/USDT", "ADA/USDT"}  # all mapped pairs

    async def _preflight_checks(self, symbol: str, signal: dict) -> Tuple[bool, str]:
        """Run all safety checks before placing a real order."""

        # 0. Symbol whitelist for live trading
        if not self.dry_run and symbol not in self.LIVE_ALLOWED_SYMBOLS:
            return False, f"symbol_blocked: {symbol} not in LIVE_ALLOWED_SYMBOLS (only BTC/ETH/SOL)"

        # 0b. Fee gate REMOVED — was blocking all trades

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
            t_side = _normalize_side(getattr(t, "side", ""))
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
            if not _safe_connect(delta):
                return self._cached_balance or 0
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
        side_str = _normalize_side(getattr(trade, "side", "long"))
        entry = getattr(trade, "entry_price", 0)
        margin = getattr(trade, "margin", 0)
        leverage = getattr(trade, "leverage", 0)
        position_size = getattr(trade, "position_size", 0)
        scanner = getattr(trade, "scanner", "")
        pnl_pct = round(pnl_usd / margin * 100, 2) if margin > 0 else 0

        _now = datetime.now(timezone.utc)
        _opened = getattr(trade, "opened_at", 0)
        _duration_min = (_now.timestamp() - _opened) / 60 if _opened > 0 else 0
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
            "exit_reason": reason,
            "dry_run": dry_run,
            "paper_trade_id": getattr(trade, "paper_trade_id", ""),
            "timestamp": _now.isoformat(),
            "confidence": getattr(trade, "confidence", 0),
            "grade": getattr(trade, "grade", ""),
            "ml_prob": getattr(trade, "ml_prob", 0),
            "ml_verdict": getattr(trade, "ml_verdict", ""),
            "regime": getattr(trade, "regime", ""),
            "trade_type": getattr(trade, "trade_type", ""),
            "slippage_bps": round(getattr(trade, "slippage_bps", 0), 2),
            "slippage_impact_r": getattr(trade, "slippage_impact_r", 0),
            "duration_min": round(_duration_min, 1),
            "stop_loss": getattr(trade, "stop_loss", 0),
            "peak_mfe_r": round(getattr(trade, "peak_mfe_r", 0), 3),
            "initial_risk": getattr(trade, "initial_risk", 0),
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
            side = _normalize_side(getattr(t, "side", "long"))
            open_trades_data.append({
                "trade_id": t.trade_id,
                "symbol": t.symbol,
                "side": side,
                "entry_price": t.entry_price,
                "stop_loss": getattr(t, "stop_loss", 0),
                "tp1": getattr(t, "tp1", 0),
                "tp2": getattr(t, "tp2", 0),
                # Independent exit fields (survive restart)
                "independent_exit": getattr(t, "independent_exit", True),
                "initial_risk": getattr(t, "initial_risk", 0),
                "highest_price": getattr(t, "highest_price", t.entry_price),
                "lowest_price": getattr(t, "lowest_price", t.entry_price),
                "peak_mfe_r": getattr(t, "peak_mfe_r", 0),
                "mfe_stale_seconds": getattr(t, "mfe_stale_seconds", 0),
                "last_mfe_update_time": getattr(t, "last_mfe_update_time", 0),
                "momentum_decay_count": getattr(t, "momentum_decay_count", 0),
                "breakeven_set": getattr(t, "breakeven_set", False),
                "chandelier_stop": getattr(t, "chandelier_stop", 0),
                "entry_atr": getattr(t, "entry_atr", 0),
                "trade_type": getattr(t, "trade_type", "SCALP"),
                "regime": getattr(t, "regime", ""),
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
                "client_order_id": getattr(t, "client_order_id", ""),
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
            paper_id = getattr(trade, "paper_trade_id", "") or ""
            # Independent exit trades manage their own lifecycle — never orphan them
            if getattr(trade, "independent_exit", False):
                continue
            if paper_id and paper_id not in active_paper_ids:
                orphans.append(trade_id)
            elif not paper_id and not active_paper_ids:
                logger.info("ORPHAN GHOST: %s has no paper_trade_id and 0 active signals — marking orphan",
                           getattr(trade, "symbol", trade_id))
                orphans.append(trade_id)

        # Clear persistent orphan dedup set every 60 seconds
        now = time.time()
        if now - self._orphan_closed_set_ts > 60:
            self._orphan_closed_set.clear()
            self._orphan_closed_set_ts = now

        closed_count = 0

        for trade_id in orphans:
            trade = self.real_trades.get(trade_id)
            if not trade:
                continue
            paper_id = getattr(trade, "paper_trade_id", "")

            # DEDUP: skip if we already closed this exact symbol+side+entry combo
            # Persists across cycles (cleared every 60s) to prevent multi-fire
            dedup_key = f"{trade.symbol}_{getattr(trade, 'side', '')}_{trade.entry_price}"
            if dedup_key in self._orphan_closed_set:
                logger.info("ORPHAN DEDUP: %s already closed this cycle — skipping", dedup_key)
                self.real_trades.pop(trade_id, None)
                self.paper_to_real.pop(paper_id, None)
                continue
            self._orphan_closed_set.add(dedup_key)

            # Use paper exit price if available (CRITICAL: avoid entry==exit fee-only losses)
            paper_close = closed_paper_trades.get(paper_id, {})
            exit_price = paper_close.get("exit_price", 0)
            exit_reason = paper_close.get("exit_reason", "orphan_sync")
            exit_source = "paper_close"

            # Fallback: use current_price (from WS ticker), NEVER use entry_price
            if not exit_price or exit_price <= 0:
                exit_price = getattr(trade, "current_price", 0)
                exit_source = "current_price"

            # If still no valid exit price, skip this orphan — don't create fee-only losses
            if not exit_price or exit_price <= 0:
                logger.debug("ORPHAN SKIP: %s — no valid exit price (entry=%s, current=%s), waiting for price update",
                            trade.symbol, trade.entry_price, getattr(trade, "current_price", 0))
                continue

            side_str = _normalize_side(getattr(trade, "side", "long"))

            # Zero-move detection: if exit ~= entry, record $0 PnL (don't charge fees)
            if abs(exit_price - trade.entry_price) < 0.0001 * trade.entry_price:
                net_pnl = 0.0
                logger.info("ORPHAN ZERO-MOVE: %s entry==exit (%.4f), recording $0 PnL", trade.symbol, exit_price)
            else:
                if side_str == "long":
                    pnl_pct = (exit_price - trade.entry_price) / trade.entry_price if trade.entry_price else 0
                else:
                    pnl_pct = (trade.entry_price - exit_price) / trade.entry_price if trade.entry_price else 0
                # Use margin (USD stake), not entry_price × lots (which gives wrong notional)
                margin = getattr(trade, "margin", 0) or 15.10
                leverage = getattr(trade, "leverage", 10) or 10
                position_usd = margin * leverage
                # Delta India fees: 0.059% taker × 2 = 0.118% round trip
                net_pnl = pnl_pct * position_usd - position_usd * DELTA_ROUND_TRIP_FEE_PCT
                # Safety cap: orphan PnL should never exceed notional (margin × leverage)
                net_pnl = max(net_pnl, -position_usd)

            margin = getattr(trade, "margin", 0) or 15.10
            leverage = getattr(trade, "leverage", 10) or 10
            position_usd = margin * leverage

            # Use paper exit reason if available for better tracking
            close_reason = exit_reason if exit_reason != "orphan_sync" else "orphan_sync"
            logger.info("REAL [DRY RUN] ORPHAN CLOSE: %s %s | pnl=$%.2f | margin=$%.2f pos=$%.2f | exit=%.4f (src=%s) | reason=%s",
                        trade.symbol, side_str, net_pnl, margin, position_usd, exit_price, exit_source, close_reason)
            self.circuit_breaker.record_trade_with_reason(net_pnl, close_reason)
            self._record_closed_trade(trade, exit_price, net_pnl, close_reason, dry_run=True)
            closed_count += 1

        if closed_count:
            self._save_state()

    async def update_prices(self):
        """Update current prices for all open dry run positions."""
        if not self.real_trades:
            return
        # Log every tick so we know it is running


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
                # Ensure all loaded trades have independent_exit fields
                if "independent_exit" not in td:
                    td["independent_exit"] = True
                if "initial_risk" not in td:
                    entry = td.get("entry_price", 0)
                    sl = td.get("stop_loss", 0)
                    td["initial_risk"] = abs(entry - sl) if entry > 0 and sl > 0 else entry * 0.01
                if "highest_price" not in td:
                    td["highest_price"] = td.get("entry_price", 0)
                if "lowest_price" not in td:
                    td["lowest_price"] = td.get("entry_price", 0)
                for _fld in ["peak_mfe_r", "mfe_stale_seconds", "chandelier_stop", "momentum_decay_count"]:
                    if _fld not in td:
                        td[_fld] = 0
                if "breakeven_set" not in td:
                    td["breakeven_set"] = False
                if "entry_atr" not in td:
                    td["entry_atr"] = td.get("initial_risk", 0)
                if "trade_type" not in td:
                    td["trade_type"] = "SCALP"
                # Backfill independent_exit fields for trades from older versions
                if "independent_exit" not in td:
                    td["independent_exit"] = True
                if "initial_risk" not in td:
                    _e = td.get("entry_price", 0)
                    _s = td.get("stop_loss", 0)
                    td["initial_risk"] = abs(_e - _s) if _e > 0 and _s > 0 else _e * 0.008
                for _fld, _def in [("highest_price", td.get("entry_price", 0)),
                                   ("lowest_price", td.get("entry_price", 0)),
                                   ("peak_mfe_r", 0), ("mfe_stale_seconds", 0),
                                   ("last_mfe_update_time", 0), ("momentum_decay_count", 0),
                                   ("breakeven_set", False), ("chandelier_stop", 0),
                                   ("entry_atr", td.get("initial_risk", 0))]:
                    if _fld not in td:
                        td[_fld] = _def
                if "trade_type" not in td or td["trade_type"] not in ("SCALP", "INTRADAY", "RUNNER"):
                    td["trade_type"] = "SCALP"
                dry_obj = type("DryTrade", (), td)()
                self.real_trades[td["trade_id"]] = dry_obj
            logger.info(
                "REAL: Loaded state — enabled=%s, dry_run=%s, CB daily=$%.2f, "
                "total=$%.2f, %d closed trades, %d open trades",
                self.enabled, self.dry_run,
                self.circuit_breaker.daily_pnl, self.circuit_breaker.total_pnl,
                len(self.closed_real_trades), len(self.real_trades),
            )
            # Log loaded trade details for debugging
            for _tid, _t in self.real_trades.items():
                logger.info("REAL LOADED: %s %s %s | independent=%s risk=%.4f",
                           _tid[:20], getattr(_t, "symbol", "?"), getattr(_t, "side", "?"),
                           getattr(_t, "independent_exit", False),
                           getattr(_t, "initial_risk", 0))
        except Exception as e:
            logger.warning("Failed to load real trading state: %s", e)

    # ==================================================================
    # Status for Dashboard
    # ==================================================================

    async def refresh_balance(self) -> float:
        """Force-refresh exchange balance (called by dashboard endpoint)."""
        self._balance_ts = 0  # invalidate cache
        return await self._get_balance()


    async def reconcile_exchange_positions(self):
        # Re-enabled with independent_exit guard
        """On startup: import exchange positions not in local state.

        Prevents ghost positions by ensuring every exchange position
        is tracked locally. If a position exists on the exchange but
        not in self.real_trades, it gets imported with a synthetic trade ID.
        """
        try:
            delta = self._delta_demo if self.dry_run else self._delta_live
            if not _safe_connect(delta):
                logger.warning("RECONCILE: Delta connect failed — skipping")
                return

            from exchange.delta_client import PRODUCT_MAP
            _reverse_sym = {v["symbol"]: k for k, v in PRODUCT_MAP.items() if v.get("symbol")}

            positions = delta.get_all_positions()
            local_symbols = {getattr(t, "symbol", "") for t in self.real_trades.values()}

            imported = 0
            for p in positions:
                size = int(float(p.get("size", 0)))
                if abs(size) == 0:
                    continue

                ex_sym = p.get("product", {}).get("symbol", "")
                internal_sym = _reverse_sym.get(ex_sym, ex_sym)
                entry = float(p.get("entry_price", 0))

                # Skip if already tracked locally
                if internal_sym in local_symbols:
                    continue

                # Import as tracked position
                side = "long" if size > 0 else "short"
                trade_id = "imported_%s_%d" % (internal_sym.replace("/", ""), int(time.time()))

                trade = type("ImportedTrade", (), {
                    "trade_id": trade_id,
                    "symbol": internal_sym,
                    "side": side,
                    "entry_price": entry,
                    "stop_loss": 0,
                    "tp1": 0, "tp2": 0, "tp3": 0,
                    "position_size": abs(size),
                    "margin": 0,
                    "leverage": 20,
                    "status": "open",
                    "opened_at": time.time(),
                    "scanner": "imported",
                    "trade_type": "imported",
                    "confidence": 0,
                    "ml_prob": 0,
                    "ml_verdict": "",
                    "regime": "",
                    "paper_trade_id": "",
                    "client_order_id": "",
                    "current_price": entry,
                    "slippage_bps": 0,
                    "slippage_impact_r": 0,
                })()

                self.real_trades[trade_id] = trade
                imported += 1
                logger.warning(
                    "RECONCILE: Imported exchange position %s %s %d lots @ %.4f (not in local state)",
                    internal_sym, side, abs(size), entry,
                )

                # Check if position has SL/TP orders before auto-closing
                has_protection = False
                try:
                    product_id = delta._get_product_id(internal_sym)
                    open_orders = delta._client.get_active_orders(product_id=product_id) or []
                    for o in open_orders:
                        if o.get("stop_order_type") in ("stop_loss_order", "take_profit_order"):
                            has_protection = True
                            break
                except Exception:
                    has_protection = True  # assume protected if cant check

                if has_protection:
                    logger.info(
                        "RECONCILE: Imported %s %s %d lots — HAS SL/TP, keeping open",
                        internal_sym, side, abs(size),
                    )
                else:
                    try:
                        close_side = "sell" if side == "long" else "buy"
                        product_id = delta._get_product_id(internal_sym)
                        delta._client.place_order(
                            product_id=product_id,
                            size=abs(size),
                            side=close_side,
                            order_type=delta._OrderType.MARKET,
                            reduce_only=True,
                        )
                        logger.warning(
                            "RECONCILE: AUTO-CLOSED orphan %s %s %d lots — no SL/TP protection",
                            internal_sym, side, abs(size),
                        )
                    except Exception as close_err:
                        logger.error(
                            "RECONCILE: FAILED to auto-close orphan %s — MANUAL CLOSE REQUIRED: %s",
                            internal_sym, close_err,
                        )

            if imported > 0:
                logger.info("RECONCILE: Imported %d exchange positions into local state", imported)
                self._save_state()
            else:
                logger.info("RECONCILE: All exchange positions already tracked (0 imports)")

        except Exception as e:
            logger.warning("RECONCILE: Failed — %s", e)

    async def sync_exchange_positions(self) -> None:
        """Reconcile local state with actual exchange positions (demo or real).
        Uses delta-rest-client for position queries.
        """
        try:
            delta = self._delta_demo if self.dry_run else self._delta_live
            if not _safe_connect(delta):
                logger.warning("SYNC: Delta connect failed — skipping position sync")
                return

            positions = delta.get_all_positions()

            # FIX #1: Build exchange_open using OUR internal symbol format
            # Reverse-map: "BTCUSD" → "BTC/USDT" using PRODUCT_MAP
            _reverse_sym = {v["symbol"]: k for k, v in PRODUCT_MAP.items() if v.get("symbol")}
            exchange_open = {}  # internal_symbol → position_data
            for p in positions:
                ex_sym = p.get("product", {}).get("symbol", p.get("symbol", ""))
                internal_sym = _reverse_sym.get(ex_sym, ex_sym)
                size = float(p.get("size", 0))
                if abs(size) > 0:
                    exchange_open[internal_sym] = p

            # FIX #3: Do NOT detect exchange_closed — trust paper→real mirror for exits
            # The old approach caused false positives every sync cycle because of symbol
            # format mismatches. Now we only UPDATE tracked positions with exchange data.
            for trade_id in list(self.real_trades.keys()):
                t = self.real_trades[trade_id]
                t_sym = getattr(t, "symbol", "")
                if t_sym in exchange_open:
                    # Position still open — update current price from exchange
                    ep = exchange_open[t_sym]
                    current = float(ep.get("entry_price", 0))
                    if current > 0:
                        t.current_price = current
                else:
                    # Position not found — but DON'T assume it's closed
                    # It may be a symbol format issue or API lag
                    logger.debug("SYNC: %s not found on exchange — trusting local state", t_sym)

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


    # ══════════════════════════════════════════════════════════════
    # PARALLEL EXIT SYSTEM — real trades manage their own exits
    # Uses real fill prices, not paper signal prices
    # ══════════════════════════════════════════════════════════════


    async def partial_close_real(self, paper_trade_id: str, tp_level: int, close_pct: float):
        """Partial close real position when paper hits TP1/TP2.
        close_pct: fraction to close (0.35 = 35%)."""
        real_trade_id = self.paper_to_real.get(paper_trade_id)
        if not real_trade_id:
            return
        trade = self.real_trades.get(real_trade_id)
        if not trade:
            return

        symbol = getattr(trade, "symbol", "")
        side = _normalize_side(getattr(trade, "side", "long"))
        total_lots = int(getattr(trade, "position_size", 0))
        close_lots = max(1, int(total_lots * close_pct))

        try:
            delta = self._delta_demo if self.dry_run else self._delta_live
            if not _safe_connect(delta):
                return

            close_side = "sell" if side == "long" else "buy"
            result = delta._client.create_order({
                "product_id": delta._get_product_id(symbol),
                "size": close_lots,
                "side": close_side,
                "order_type": "market_order",
                "reduce_only": "true",
            })
            fill = float(result.get("average_fill_price", 0) or 0)
            # Update remaining position size
            trade.position_size = total_lots - close_lots
            if hasattr(trade, "tp1_hit") and tp_level == 1:
                trade.tp1_hit = True
            if hasattr(trade, "tp2_hit") and tp_level == 2:
                trade.tp2_hit = True
            logger.info("REAL PARTIAL TP%d: %s %s | closed %d/%d lots @ %.4f | remaining=%d",
                       tp_level, symbol, side, close_lots, total_lots, fill, trade.position_size)
        except Exception as e:
            logger.error("REAL PARTIAL TP%d FAILED: %s — %s", tp_level, symbol, e)

    async def update_real_trades(self, prices: dict):
        """Independent exit logic for real trades. Called every tick from orchestrator.
        Uses real fill prices for SL/trail/time calculations."""
        if not self.real_trades:
            return
        
        to_close = []
        sl_updates = []
        now = time.time()

        for trade_id, t in list(self.real_trades.items()):
            # Skip trades without independent exit flag
            if not getattr(t, "independent_exit", False):
                continue

            symbol = getattr(t, "symbol", "")
            price = prices.get(symbol, 0)
            if price <= 0:
                continue

            side = _normalize_side(getattr(t, "side", "long"))
            is_long = side == "long"
            entry = getattr(t, "entry_price", 0)
            risk = getattr(t, "initial_risk", 0)
            if risk <= 0 or entry <= 0:
                continue

            # Update current price
            t.current_price = price

            # Track highest/lowest from REAL perspective
            if price > getattr(t, "highest_price", 0):
                t.highest_price = price
            if price < getattr(t, "lowest_price", 999999):
                t.lowest_price = price

            # Current R from REAL fill price
            current_r = (price - entry) / risk if is_long else (entry - price) / risk

            # Peak MFE tracking
            if current_r > getattr(t, "peak_mfe_r", 0):
                t.peak_mfe_r = current_r
                t.last_mfe_update_time = now
                t.mfe_stale_seconds = 0
            elif getattr(t, "last_mfe_update_time", 0) > 0:
                t.mfe_stale_seconds = now - t.last_mfe_update_time

            age = now - getattr(t, "opened_at", now)
            regime = getattr(t, "regime", "")
            tt = getattr(t, "trade_type", "SCALP") or "SCALP"
            try:
                config = TRADE_TYPE_CONFIG.get(tt, TRADE_TYPE_CONFIG[TRADE_TYPE_SCALP])
            except Exception:
                config = {"max_age_sec": 900, "early_kill_sec": 60, "early_kill_mfe": 0.10,
                          "chandelier_mult_trending": 2.0, "chandelier_mult_ranging": 1.5,
                          "extension_trigger_r": 0.15, "extended_age_sec": 1350,
                          "full_extend_r": 0.3, "full_extended_age_sec": 1800}

            # ── [1] HARD LOSS CAP (-1.2R) ──
            if current_r <= -1.2:
                to_close.append((trade_id, price, "hard_loss_cap"))
                continue

            # ── [2] CHANDELIER TRAIL (from real highest/lowest) ──
            atr = getattr(t, "initial_risk", 0) or getattr(t, "entry_atr", 0) or 0
            if atr > 0:
                if regime in ("trending_up", "trending_down", "breakout"):
                    mult = config.get("chandelier_mult_trending", 2.0)
                else:
                    mult = config.get("chandelier_mult_ranging", 1.5)

                _min_dist = entry * 0.0040  # 0.40% floor (match baseline)  # minimum 0.15% from entry
                if is_long:
                    new_stop = t.highest_price - (atr * mult)
                    new_stop = max(new_stop, entry - _min_dist) if new_stop < entry else new_stop  # floor
                    if new_stop > getattr(t, "chandelier_stop", 0) and new_stop > entry:
                        t.chandelier_stop = new_stop
                        if new_stop > t.stop_loss:
                            old_sl = t.stop_loss
                            t.stop_loss = new_stop
                            t.breakeven_set = True
                            sl_updates.append((trade_id, symbol, new_stop))
                            logger.info("REAL CHANDELIER: %s long | SL %.4f -> %.4f | high=%.4f atr=%.4f mult=%.1f",
                                       symbol, old_sl, new_stop, t.highest_price, atr, mult)
                else:
                    new_stop = t.lowest_price + (atr * mult)
                    # Floor: don't tighten closer than 0.15% from entry
                    _sl_ceil = entry + entry * 0.0040  # 0.40% floor (match baseline)
                    if new_stop > _sl_ceil:
                        new_stop = _sl_ceil
                    ch_stop = getattr(t, "chandelier_stop", 0)
                    if (ch_stop == 0 or new_stop < ch_stop) and new_stop < entry:
                        t.chandelier_stop = new_stop
                        if new_stop < t.stop_loss:
                            old_sl = t.stop_loss
                            t.stop_loss = new_stop
                            t.breakeven_set = True
                            sl_updates.append((trade_id, symbol, new_stop))
                            logger.info("REAL CHANDELIER: %s short | SL %.4f -> %.4f | low=%.4f atr=%.4f mult=%.1f",
                                       symbol, old_sl, new_stop, t.lowest_price, atr, mult)

            # ── MFE PROFIT LOCK FLOOR (real) ──
            if getattr(t, "peak_mfe_r", 0) >= 0.3 and getattr(t, "initial_risk", 0) > 0:
                _peak = t.peak_mfe_r
                _lp = 0.85 if _peak >= 1.5 else (0.80 if _peak >= 1.0 else (0.70 if _peak >= 0.5 else 0.60))
                _lock_dist = _peak * _lp * t.initial_risk
                if is_long:
                    _mfe_sl = entry + _lock_dist
                    if _mfe_sl > t.stop_loss:
                        t.stop_loss = _mfe_sl
                        t.breakeven_set = True
                        sl_updates.append((trade_id, symbol, _mfe_sl))
                else:
                    _mfe_sl = entry - _lock_dist
                    if _mfe_sl < t.stop_loss:
                        t.stop_loss = _mfe_sl
                        t.breakeven_set = True
                        sl_updates.append((trade_id, symbol, _mfe_sl))

            # ── [3] SL HIT (real's own SL) ──
            sl = getattr(t, "stop_loss", 0)
            if sl > 0:
                hit = (price <= sl) if is_long else (price >= sl)
                if hit:
                    reason = "trail_profit" if getattr(t, "breakeven_set", False) and current_r > 0 else "sl_hit"
                    to_close.append((trade_id, price, reason))
                    logger.info("REAL %s: %s %s @ %.4f | SL=%.4f R=%.2f peak=%.2fR",
                               reason.upper(), symbol, side, price, sl, current_r, t.peak_mfe_r)
                    continue

            # ── [4] BREAKEVEN at 0.15R MFE ──
            if getattr(t, "peak_mfe_r", 0) >= 0.15 and not getattr(t, "breakeven_set", False):
                fee_buf = entry * 0.0003
                be_sl = entry + fee_buf if is_long else entry - fee_buf
                if (is_long and be_sl > t.stop_loss) or (not is_long and be_sl < t.stop_loss):
                    old_sl = t.stop_loss
                    t.stop_loss = be_sl
                    t.breakeven_set = True
                    sl_updates.append((trade_id, symbol, be_sl))
                    logger.info("REAL BREAKEVEN: %s %s | MFE=%.2fR | SL %.4f -> %.4f",
                               symbol, side, t.peak_mfe_r, old_sl, be_sl)

            # ── [5] DYNAMIC TIME DECAY ──
            still_growing = getattr(t, "peak_mfe_r", 0) > 0 and current_r >= t.peak_mfe_r * 0.85
            ext_trigger = config.get("extension_trigger_r", 0.15)
            ext_age = config.get("extended_age_sec", config["max_age_sec"])
            full_ext_r = config.get("full_extend_r", 0.3)
            full_ext_age = config.get("full_extended_age_sec", config["max_age_sec"] * 2)

            if getattr(t, "peak_mfe_r", 0) >= full_ext_r and still_growing:
                effective_max = full_ext_age
            elif getattr(t, "peak_mfe_r", 0) >= ext_trigger:
                effective_max = ext_age
            else:
                effective_max = config["max_age_sec"]

            if age >= effective_max and current_r < 0:
                to_close.append((trade_id, price, "time_decay"))
                logger.info("REAL TIME DECAY: %s %s | age=%dm max=%dm R=%.2f peak=%.2fR",
                           symbol, side, int(age/60), int(effective_max/60), current_r, t.peak_mfe_r)
                continue

            # ── [6] EARLY KILL ──
            ek_sec = config.get("early_kill_sec", 0)
            ek_mfe = config.get("early_kill_mfe", 0)
            if ek_sec > 0 and age >= ek_sec and getattr(t, "peak_mfe_r", 0) < ek_mfe and current_r < -0.15:
                to_close.append((trade_id, price, "early_kill"))
                logger.info("REAL EARLY KILL: %s %s | age=%.0fs mfe=%.2fR < %.2fR R=%.2f",
                           symbol, side, age, t.peak_mfe_r, ek_mfe, current_r)
                continue

        # ── Execute closes on Delta ──
        for trade_id, exit_price, reason in to_close:
            trade = self.real_trades.get(trade_id)
            if not trade:
                continue
            try:
                await self._close_real_independent(trade, exit_price, reason)
            except Exception as e:
                logger.error("REAL CLOSE FAILED: %s — %s", trade_id, e)

        # ── Sync SL updates to Delta ──
        for trade_id, symbol, new_sl in sl_updates:
            try:
                await self.update_exchange_sl(trade_id, symbol, new_sl)
            except Exception as e:
                logger.debug("REAL SL SYNC failed: %s — %s", symbol, e)

    async def _close_real_independent(self, trade, exit_price: float, reason: str):
        """Close a real trade on Delta independently (not mirroring paper)."""
        symbol = getattr(trade, "symbol", "")
        side = _normalize_side(getattr(trade, "side", "long"))
        lots = int(getattr(trade, "position_size", 0))
        entry = getattr(trade, "entry_price", 0)
        margin = getattr(trade, "margin", 0)
        leverage = getattr(trade, "leverage", 1)

        # Close on Delta
        try:
            delta = self._delta_demo if self.dry_run else self._delta_live
            if _safe_connect(delta):
                close_side = "sell" if side == "long" else "buy"
                close_result = delta._client.create_order({
                    "product_id": delta._get_product_id(symbol),
                    "size": lots,
                    "side": close_side,
                    "order_type": "market_order",
                    "reduce_only": "true",
                })
                fill = float(close_result.get("average_fill_price", exit_price) or exit_price)
                if fill > 0:
                    exit_price = fill
        except Exception as e:
            logger.error("REAL INDEPENDENT CLOSE: Delta close failed %s — %s", symbol, e)

        # PnL from real fills
        contract_size = PRODUCT_MAP.get(symbol, {}).get("contract_size", 1.0)
        qty = lots * contract_size
        if side == "long":
            pnl_usd = (exit_price - entry) * qty
        else:
            pnl_usd = (entry - exit_price) * qty

        # Deduct fees
        fee_rate = 0.00059 * 2  # taker both sides
        fee_usd = abs(exit_price * qty * fee_rate)
        pnl_usd -= fee_usd

        logger.info("REAL INDEPENDENT EXIT: %s %s | entry=%.4f exit=%.4f | pnl=$%+.2f | %s | peak=%.2fR",
                    symbol, side, entry, exit_price, pnl_usd, reason, getattr(trade, "peak_mfe_r", 0))

        # Update circuit breaker
        self.circuit_breaker.record_trade(pnl_usd)

        # Record
        self._record_closed_trade(trade, exit_price, pnl_usd, reason, self.dry_run)
        self._save_state()


    async def update_exchange_sl(self, paper_trade_id: str, symbol: str, new_sl: float):
        """Update SL on exchange when smart trail moves the stop.

        Tries atomic edit_bracket first (no gap), falls back to cancel+replace.
        """
        real_trade_id = self.paper_to_real.get(paper_trade_id)
        if not real_trade_id:
            return

        trade = self.real_trades.get(real_trade_id)
        if not trade:
            return

        try:
            delta = self._delta_demo if self.dry_run else self._delta_live
            if not _safe_connect(delta):
                return

            side = _normalize_side(getattr(trade, "side", "long"))

            # Try atomic bracket edit first (Tier 3: no protection gap)
            result = delta.edit_bracket(symbol, stop_loss_price=new_sl)
            if result and not result.get("error"):
                trade.stop_loss = new_sl
                logger.info("REAL SL SYNC [EDIT]: %s %s | SL → %.4f", symbol, side, new_sl)
                return

            # Fallback: cancel + replace
            prod = PRODUCT_MAP.get(symbol, {})
            product_id = prod.get("demo_id" if self.dry_run else "prod_id")
            if not product_id:
                return

            sl_side = "sell" if side == "long" else "buy"

            # Cancel existing SL orders
            try:
                delta.cancel_all_orders_bulk(product_id)
            except Exception:
                pass

            # Place new SL with reduce_only + close_on_trigger
            size = int(getattr(trade, "position_size", 0))
            if size > 0:
                delta.place_stop_loss(symbol, sl_side, size, new_sl)
                trade.stop_loss = new_sl
                logger.info("REAL SL SYNC [REPLACE]: %s %s | SL → %.4f", symbol, side, new_sl)
        except Exception as e:
            logger.debug("REAL SL SYNC failed: %s %s | %s", symbol, new_sl, e)

    def get_status(self) -> Dict:
        """Return current real trading status for dashboard."""
        open_trades = []
        for t in self.real_trades.values():
            side = _normalize_side(getattr(t, "side", "long"))
            entry = t.entry_price
            current = getattr(t, "current_price", entry) or entry
            pos_size = getattr(t, "position_size", 0)  # lots (contracts)
            margin = getattr(t, "margin", 0)
            lev = getattr(t, "leverage", 1)
            # Get contract size from PRODUCT_MAP (single source of truth)
            contract_size = PRODUCT_MAP.get(t.symbol, {}).get("contract_size", 1.0)
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

    # ==================================================================
    # Emergency Controls (Tier 1)
    # ==================================================================

    def _emergency_close_all(self, reason: str):
        """Called by circuit breaker on trip. Immediately close ALL positions.

        Cancels all orders (no rate limit), closes all positions,
        and clears local state to prevent further trading.
        """
        logger.critical("EMERGENCY CLOSE-ALL triggered: %s", reason)
        n_trades = len(self.real_trades)

        # Step 1: Try to close on exchange (best-effort, timeout-protected)
        try:
            delta = self._delta_demo if self.dry_run else self._delta_live
            if not _safe_connect(delta, timeout_sec=3.0):
                logger.error("EMERGENCY: Delta connect failed — clearing local state only")
                delta = None

            if delta:
                # Cancel ALL orders (no rate limit on cancels)
                try:
                    delta.cancel_all_orders_bulk()
                    logger.info("EMERGENCY: All orders cancelled")
                except Exception as cancel_err:
                    logger.warning("EMERGENCY: Cancel failed: %s", cancel_err)

                # Close all positions via bulk endpoint
                try:
                    result = delta.close_all_positions()
                    logger.info("EMERGENCY: Close-all result: %s", str(result)[:200])
                except Exception as close_err:
                    logger.warning("EMERGENCY: Close-all failed: %s", close_err)
        except Exception as e:
            logger.error("EMERGENCY: Exchange ops failed: %s", e)

        # Step 2: ALWAYS clear local state (even if exchange ops failed)
        for trade_id, t in list(self.real_trades.items()):
            try:
                entry_p = getattr(t, "entry_price", 0)
                self._record_closed_trade(t, entry_p, 0, f"emergency_{reason}", dry_run=self.dry_run)
            except Exception:
                pass

        self.real_trades.clear()
        self._open_positions.clear()
        self.paper_to_real.clear()
        self._save_state()

        logger.critical("EMERGENCY CLOSE-ALL completed: %d positions cleared | reason=%s",
                        n_trades, reason)

    # ==================================================================
    # WebSocket Event Handlers (Tier 1)
    # ==================================================================

    async def handle_exchange_fill(
        self, client_order_id: str, symbol: str,
        fill_price: float, side: str,
    ):
        """Handle instant SL/TP fill notification from private WS channel.

        Called when a reduce_only order fills on exchange (SL or TP hit).
        This replaces the slow REST polling in sync_exchange_positions().
        """
        try:
            logger.info(
                "WS FILL: %s %s @ %.4f | coid=%s",
                symbol, side, fill_price, client_order_id[:12] if client_order_id else "-",
            )

            # Find the matching real trade via client_order_id or paper_to_real
            real_trade_id = None
            matched_paper_id = None

            # Direct lookup: client_order_id == paper_trade_id[:32]
            if client_order_id:
                for pid, rid in list(self.paper_to_real.items()):
                    if pid[:32] == client_order_id[:32]:
                        real_trade_id = rid
                        matched_paper_id = pid
                        break

            # Fallback: search real_trades by client_order_id attribute
            if not real_trade_id and client_order_id:
                for tid, t in list(self.real_trades.items()):
                    coid = getattr(t, "client_order_id", "")
                    if coid and coid == client_order_id[:32]:
                        real_trade_id = tid
                        break

            # Fallback: search by symbol
            if not real_trade_id:
                for tid, t in list(self.real_trades.items()):
                    t_sym = getattr(t, "symbol", "") if not isinstance(t, dict) else t.get("symbol", "")
                    if t_sym == symbol:
                        real_trade_id = tid
                        break

            if not real_trade_id:
                logger.debug("WS FILL: No matching trade for coid=%s sym=%s",
                            client_order_id[:12] if client_order_id else "-", symbol)
                return

            trade = self.real_trades.get(real_trade_id)
            if not trade:
                return

            # Calculate PnL with normalized side
            if isinstance(trade, dict):
                side_str = _normalize_side(trade.get("side", "long"))
                entry_p = trade.get("entry_price", 0)
                margin = trade.get("margin", 0)
                leverage = trade.get("leverage", 1)
            else:
                side_str = _normalize_side(getattr(trade, "side", "long"))
                entry_p = getattr(trade, "entry_price", 0)
                margin = getattr(trade, "margin", 0)
                leverage = getattr(trade, "leverage", 1)

            if side_str == "long":
                pnl_pct = (fill_price - entry_p) / entry_p if entry_p else 0
            else:
                pnl_pct = (entry_p - fill_price) / entry_p if entry_p else 0

            notional = margin * leverage
            net_pnl = pnl_pct * notional - notional * DELTA_ROUND_TRIP_FEE_PCT

            reason = "ws_sl_hit" if net_pnl < 0 else "ws_tp_hit"

            logger.info(
                "WS EXIT: %s %s | entry=%.4f exit=%.4f | pnl=$%.2f | %s",
                symbol, side_str, entry_p, fill_price, net_pnl, reason,
            )

            # Record and clean up
            self.circuit_breaker.record_trade(net_pnl)
            self._record_closed_trade(trade, fill_price, net_pnl, reason, dry_run=self.dry_run)

            self.real_trades.pop(real_trade_id, None)
            self._open_positions.pop(real_trade_id, None)
            if matched_paper_id:
                self.paper_to_real.pop(matched_paper_id, None)
            self._save_state()
        except Exception as e:
            logger.error("WS FILL handler error: %s", e)

    async def handle_exchange_position_close(self, symbol: str, pnl: float):
        """Handle position close notification from WS (catches liquidations).

        Fallback for positions that close without matching a client_order_id.
        """
        try:
            for tid, t in list(self.real_trades.items()):
                t_sym = getattr(t, "symbol", "") if not isinstance(t, dict) else t.get("symbol", "")
                if t_sym == symbol:
                    entry_p = getattr(t, "entry_price", 0) if not isinstance(t, dict) else t.get("entry_price", 0)
                    logger.info(
                        "WS POS CLOSE: %s | entry=%.4f | exchange_pnl=%.4f",
                        symbol, entry_p, pnl,
                    )
                    self._record_closed_trade(t, entry_p, pnl, "exchange_closed", dry_run=self.dry_run)
                    self.real_trades.pop(tid, None)
                    self._open_positions.pop(tid, None)

                    for pid, rid in list(self.paper_to_real.items()):
                        if rid == tid:
                            self.paper_to_real.pop(pid, None)
                            break

                    self._save_state()
                    return

            logger.debug("WS POS CLOSE: No matching trade for %s (may be already cleaned up)", symbol)
        except Exception as e:
            logger.error("WS POS CLOSE handler error: %s", e)
