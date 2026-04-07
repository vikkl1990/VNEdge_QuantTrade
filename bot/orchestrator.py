"""
BotOrchestrator - the central brain of the crypto trading bot.

Coordinates all subsystems: data feeds, strategy analysis, risk checks,
trade execution, alerting, journaling, and dashboard updates.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bot.decision_engine import DecisionEngine
from bot.safety import audit_log, backup_state_files, check_price_freshness
from bot.signal_learner import SignalLearner
from bot.signal_tracker import SignalTracker
from bot.trade_monitor import TradeMonitorAgent
from config.constants import AlertLevel, BotMode, SignalType

# Optional WebSocket for low-latency price feeds
try:
    from exchange.delta_ws import DeltaWebSocket
    _HAS_DELTA_WS = True
except ImportError:
    _HAS_DELTA_WS = False

# Optional Latency Arb engine (Binance vs Delta price dislocation)
try:
    from strategies.latency_arb import LatencyArbEngine
    _HAS_LATENCY_ARB = True
except ImportError:
    _HAS_LATENCY_ARB = False


class BotOrchestrator:
    """Orchestrates all trading bot components in a single async event loop.

    Parameters
    ----------
    config : dict
        Full application configuration.
    mode : BotMode
        Current operating mode (signal_only, paper, live, forward_test).
    symbols : list[str]
        Symbols this instance is responsible for.
    logger : logging.Logger
        Pre-configured logger.
    exchange, data_manager, data_feed, strategy, risk_manager,
    execution_engine, alert_manager, journal, dashboard, state_manager,
    heartbeat
        Subsystem component instances.
    """

    # How often to persist state (seconds)
    STATE_SAVE_INTERVAL = 60
    # How often to run position checks when no candle event fires (seconds)
    POSITION_CHECK_INTERVAL = 15
    # How often the fast trade monitor checks active signals (seconds)
    TRADE_MONITOR_INTERVAL = 5
    # Heartbeat log interval (seconds)
    HEARTBEAT_LOG_INTERVAL = 300

    def __init__(
        self,
        *,
        config: dict,
        mode: BotMode,
        symbols: list[str],
        logger: logging.Logger,
        exchange,
        data_manager,
        data_feed,
        strategy,
        risk_manager,
        execution_engine,
        alert_manager,
        journal,
        dashboard,
        state_manager,
        heartbeat,
        real_manager=None,
    ) -> None:
        self._config = config
        self._mode = mode
        self._symbols = symbols
        self._log = logger

        # Subsystems
        self._exchange = exchange
        self._data_manager = data_manager
        self._data_feed = data_feed
        self._strategy = strategy
        self._risk_manager = risk_manager
        if risk_manager:
            risk_manager.load_state()
        self._execution = execution_engine
        self._real_manager = real_manager
        self._alerts = alert_manager
        self._journal = journal
        self._dashboard = dashboard
        self._state = state_manager
        self._heartbeat = heartbeat

        # Internal state
        self._running = False
        self._start_time: Optional[float] = None
        self._tasks: list[asyncio.Task] = []
        self._stop_event = asyncio.Event()
        self._last_state_save: float = 0.0
        self._last_heartbeat_log: float = 0.0
        self._last_daily_reset: Optional[str] = None  # "YYYY-MM-DD"
        self._last_balance_fetch: float = 0.0  # unix timestamp
        self._balance_fetch_interval: float = 300.0  # fetch balance every 5 min

        # Per-symbol error counters for circuit-breaking
        self._symbol_errors: Dict[str, int] = {s: 0 for s in symbols}
        self._max_symbol_errors = config.get("bot", {}).get("max_symbol_errors", 10)

        # Signal tracker for TP/SL monitoring and P&L/WR stats
        self._signal_tracker = SignalTracker(config)

        # Grid Bot — PAUSED (loses money in downtrends due to stale cleanup)
        # Will re-enable when regime detection can auto-pause in downtrends
        from bot.grid_bot import GridBot
        self._grid_bot = GridBot(config)
        self._grid_bot_enabled = False  # ← DISABLED

        # AI learner for adaptive confidence adjustment
        self._signal_learner = SignalLearner()

        # Trade monitor agent for P&L analysis and loss categorization
        self._trade_monitor = TradeMonitorAgent()

        # Decision engine for TRADE/WAIT directive
        self._decision_engine = DecisionEngine()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def uptime(self) -> float:
        """Seconds since start(), or 0 if not running."""
        if self._start_time is None:
            return 0.0
        return time.monotonic() - self._start_time

    @property
    def status_summary(self) -> Dict[str, Any]:
        """Snapshot of the orchestrator's health for dashboards / logs."""
        return {
            "running": self._running,
            "mode": self._mode.value,
            "uptime_s": round(self.uptime, 1),
            "symbols": self._symbols,
            "symbol_errors": dict(self._symbol_errors),
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialise all components and enter the main loop."""
        if self._running:
            self._log.warning("Orchestrator.start() called while already running")
            return

        self._log.info("Orchestrator starting (mode=%s)...", self._mode.value)
        self._running = True
        self._start_time = time.monotonic()
        self._stop_event.clear()

        # Backup state files on startup
        try:
            backup_state_files(keep=10)
        except Exception as e:
            self._log.warning("Startup backup failed: %s", e)

        audit_log("BOT_START", {"mode": self._mode.value, "symbols": self._symbols})

        try:
            # 1. Connect to exchange
            self._log.info("Connecting to exchange...")
            await self._exchange.connect()

            # 2. Recover persisted state (open positions, signals, etc.)
            await self._recover_state()

            # 3. Start data feed and register candle callback
            # The DataFeed uses an event system: .on(event_name, async_callback)
            self._data_feed.on("candle_closed", self._on_candle_close)
            # Subscribe to symbols first, then start
            for sym in self._symbols:
                await self._data_feed.subscribe(sym)
            await self._data_feed.start()

            # 3b. Seed Grid Bot with historical candle data (no 8-hour wait)
            for sym in self._symbols:
                try:
                    candles = self._data_manager.get_candles(sym, "5m")
                    if candles is not None and len(candles) > 0:
                        # Handle both DataFrame and list formats
                        if hasattr(candles, 'to_dict'):
                            closes = candles["close"].astype(float).tolist()
                        elif isinstance(candles, list) and candles:
                            if isinstance(candles[0], dict):
                                closes = [float(c["close"]) for c in candles]
                            else:
                                closes = [float(c[4]) for c in candles]
                        else:
                            closes = []
                        if closes and getattr(self, '_grid_bot_enabled', False):
                            self._grid_bot.seed_history(sym, closes)
                        elif not getattr(self, '_grid_bot_enabled', False):
                            self._log.info("Grid Bot PAUSED — skipping seed for %s", sym)
                    else:
                        self._log.info("Grid seed: no candles yet for %s (will build from live)", sym)
                except Exception as exc:
                    self._log.warning("Grid seed failed for %s: %s", sym, exc)

            # 3c. Start WebSocket for real-time prices (reduces latency 5000ms → 100ms)
            self._delta_ws = None
            self._ws_prices: Dict[str, float] = {}
            if _HAS_DELTA_WS:
                try:
                    import os
                    # Pass API creds for private WS channels (orders, positions)
                    _dry_run = getattr(self._real_manager, "dry_run", True) if self._real_manager else True
                    _ws_api_key = os.getenv("DELTA_DEMO_API_KEY" if _dry_run else "DELTA_API_KEY", "")
                    _ws_api_secret = os.getenv("DELTA_DEMO_API_SECRET" if _dry_run else "DELTA_API_SECRET", "")

                    self._delta_ws = DeltaWebSocket(
                        symbols=self._symbols,
                        on_price=self._on_ws_price,
                        on_order_fill=self._on_ws_order_fill,
                        on_position_update=self._on_ws_position_update,
                        api_key=_ws_api_key,
                        api_secret=_ws_api_secret,
                        mode="demo" if _dry_run else "live",
                    )
                    await self._delta_ws.connect()
                    self._log.info("DeltaWebSocket started (prices + private channels)")
                except Exception as exc:
                    self._log.warning("DeltaWebSocket failed to start: %s (falling back to REST)", exc)
                    self._delta_ws = None

            # 3d. Start Latency Arb engine (Binance vs Delta price dislocation monitor)
            self._latency_arb = None
            if _HAS_LATENCY_ARB:
                try:
                    self._latency_arb = None  # DISABLED: negative edge, 2.4s latency, 0% tradeable
                    if False and LatencyArbEngine:  # keep import for future
                        self._latency_arb = LatencyArbEngine(
                        symbols=self._symbols,
                        on_signal=None,  # measure-only for now
                    )
                    self._tasks.append(
                        asyncio.create_task(
                            self._latency_arb.start(measure_only=True),
                            name="latency_arb",
                        )
                    )
                    self._log.info(
                        "LatencyArb engine started (measure_only) for %s",
                        self._symbols,
                    )
                except Exception as exc:
                    self._log.warning("LatencyArb failed to start: %s", exc)
                    pass  # end of disabled block
                    self._latency_arb = None

            # 4. Start heartbeat monitor
            await self._heartbeat.start()

            # 4b. Sync trade monitor with signal tracker (tracker is source of truth)
            try:
                existing_closed = self._signal_tracker.get_closed_signals(limit=5000)
                tracker_stats = self._signal_tracker.get_stats()
                self._trade_monitor.sync_with_tracker(tracker_stats, existing_closed)
                self._log.info(
                    "Trade Monitor synced with tracker: %d closed signals",
                    len(existing_closed) if existing_closed else 0,
                )
            except Exception as exc:
                self._log.warning("Trade Monitor sync failed: %s", exc)

            # 4c. Immediately sync real_manager with paper tracker to clear ghost positions
            try:
                if hasattr(self, '_real_manager') and self._real_manager and self._real_manager.enabled:
                    active_ids = {ts.trade_id for ts in self._signal_tracker._active.values()}
                    closed_paper = {}
                    if hasattr(self._signal_tracker, '_closed_recently'):
                        closed_paper = dict(self._signal_tracker._closed_recently)
                    self._real_manager.sync_with_paper(active_ids, closed_paper)
                    self._log.info(
                        "Real manager startup sync: %d active signals, %d open real trades",
                        len(active_ids), len(self._real_manager.real_trades),
                    )
            except Exception as exc:
                self._log.warning("Real manager startup sync failed: %s", exc)

            # 4d. STARTUP POSITION RECONCILIATION
            # Import any exchange positions not in local state (ghost prevention)
            try:
                if hasattr(self, '_real_manager') and self._real_manager and self._real_manager.enabled:
                    await self._real_manager.reconcile_exchange_positions()
            except Exception as exc:
                self._log.warning("Position reconciliation failed: %s", exc)

            # 5. Start dashboard (non-blocking)
            # Wire signal tracker and AI learner to dashboard for API access
            self._dashboard._signal_tracker = self._signal_tracker

            # RL Shadow Agent — logs sizing/trail suggestions (shadow mode)
            try:
                from ml_training.rl_shadow_agent import RLShadowAgent
                self._rl_agent = RLShadowAgent()
                self._log.info("RL Shadow Agent loaded (shadow_mode=%s)", self._rl_agent.shadow_mode)
            except Exception as _rl_err:
                self._rl_agent = None
                self._log.warning("RL Shadow Agent not available: %s", _rl_err)

            # RCA Agent — monitors performance and auto-tunes parameters
            try:
                from bot.rca_agent import RCAAgent
                self._rca_agent = RCAAgent(
                    signal_tracker=self._signal_tracker,
                    suggest_only=False,  # auto-apply within safe bounds
                )
                self._log.info("RCA Agent loaded (auto-tune enabled)")
            except Exception as _rca_err:
                self._rca_agent = None
                self._log.warning("RCA Agent not available: %s", _rca_err)
            # Wire signal tracker to real manager for smart WR lookup
            if hasattr(self, "_real_manager") and self._real_manager:
                self._real_manager._signal_tracker_ref = self._signal_tracker
            self._dashboard._signal_learner = self._signal_learner
            self._dashboard._trade_monitor = self._trade_monitor
            self._dashboard._strategy = self._strategy

            # Wire ML feedback: trade outcomes → training dataset
            # MultiStrategy wraps ScalpStrategy, so traverse sub-strategies
            td = getattr(self._strategy, '_training_dataset', None)
            if td is None:
                # Check sub-strategies (MultiStrategy has _scalp and _investment)
                for attr in ('_scalp', '_investment'):
                    sub = getattr(self._strategy, attr, None)
                    if sub is not None:
                        td = getattr(sub, '_training_dataset', None)
                        if td:
                            self._log.info("Found _training_dataset on %s sub-strategy", attr)
                            break
            if td:
                self._signal_tracker.set_training_dataset(td)
                self._log.info("ML feedback loop wired: signal_tracker → training_dataset")
            else:
                self._log.warning("ML feedback loop NOT wired: no _training_dataset found on strategy")
            self._dashboard._decision_engine = self._decision_engine
            self._dashboard._grid_bot = self._grid_bot
            self._dashboard._latency_arb = self._latency_arb
            dash_cfg = self._config.get("dashboard", {})
            dash_host = dash_cfg.get("host", "0.0.0.0") if isinstance(dash_cfg, dict) else "0.0.0.0"
            dash_port = dash_cfg.get("port", 8080) if isinstance(dash_cfg, dict) else 8080
            self._tasks.append(
                asyncio.create_task(
                    self._dashboard.start(dash_host, dash_port),
                    name="dashboard",
                )
            )

            # 6. Start alert manager
            await self._alerts.initialise()

            # 7. Send startup alert
            await self._alerts.send_system_alert(
                f"Bot Started — Mode: {self._mode.value}, Symbols: {', '.join(self._symbols)}",
                level=AlertLevel.INFO,
            )

            # 8. Start fast trade monitor loop (5s interval, higher priority than signal scan)
            self._tasks.append(
                asyncio.create_task(
                    self._fast_trade_monitor_loop(),
                    name="fast_trade_monitor",
                )
            )

            # 9. Enter main loop
            await self._main_loop()

        except asyncio.CancelledError:
            self._log.info("Orchestrator task cancelled")
        except Exception as exc:
            self._handle_exception(exc, context="start")
            raise
        finally:
            await self._shutdown_components()

    async def stop(self) -> None:
        """Request graceful shutdown."""
        self._log.info("Orchestrator stop requested")
        self._stop_event.set()

    async def _shutdown_components(self) -> None:
        """Tear down all subsystems in reverse order."""
        self._running = False
        self._log.info("Shutting down components...")

        # Close WebSocket
        if self._delta_ws:
            try:
                await self._delta_ws.close()
            except Exception as _shutdown_exc:
                self._log.debug("Shutdown cleanup: %s", _shutdown_exc)

        # Stop Latency Arb engine
        if self._latency_arb:
            try:
                await self._latency_arb.stop()
            except Exception as _shutdown_exc:
                self._log.debug("Shutdown cleanup: %s", _shutdown_exc)

        # Save final state
        try:
            await self._save_state()
        except Exception:
            self._log.exception("Failed to save state during shutdown")

        # Cancel background tasks
        for task in self._tasks:
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        # Shutdown subsystems (best-effort, log errors)
        shutdown_steps = [
            ("alerts", self._alerts.shutdown()),
            ("heartbeat", self._heartbeat.stop()),
            ("dashboard", self._dashboard.stop()),
            ("data_feed", self._data_feed.stop()),
            ("exchange", self._exchange.disconnect()),
        ]
        for name, coro in shutdown_steps:
            try:
                await coro
            except Exception:
                self._log.exception("Error shutting down %s", name)

        # Send final alert (best-effort, alerts may already be down)
        try:
            await self._alerts.send_system_alert(
                f"Bot Stopped — Uptime: {self.uptime:.0f}s",
                level=AlertLevel.WARNING,
            )
        except Exception:
            pass

        self._log.info("All components shut down.")

    # ------------------------------------------------------------------
    # Fast trade monitor (5s cycle — higher priority than signal scanning)
    # ------------------------------------------------------------------

    async def _on_ws_price(self, symbol: str, last: float, bid: float, ask: float, mark: float) -> None:
        """WebSocket price callback — fires every ~100ms per symbol."""
        self._ws_prices[symbol] = last

        # If we have active trades, update them immediately (real-time!)
        if self._signal_tracker.active_count > 0:
            events = self._signal_tracker.update_prices({symbol: last})
            for ev in events:
                msg = ev.get("message", "")
                ev_type = ev.get("type", "")
                level = AlertLevel.INFO if "TP" in ev_type.upper() else AlertLevel.WARNING
                try:
                    await self._alerts.send_system_alert(msg, level=level)
                except Exception:
                    pass

                # Mirror exit to real exchange for ANY close event from WS path
                if ev_type == "sl_updated":
                    # SL updates: sync to exchange
                    if hasattr(self, '_real_manager') and self._real_manager and self._real_manager.enabled:
                        try:
                            trade_id = ev.get("trade_id", "")
                            new_sl = ev.get("new_sl", 0)
                            ev_symbol = ev.get("symbol", "")
                            if trade_id and new_sl > 0:
                                # Only sync paper SL if trade is NOT independently managed
                                _sl_real_id = self._real_manager.paper_to_real.get(trade_id, "")
                                _sl_real_t = self._real_manager.real_trades.get(_sl_real_id)
                                if _sl_real_t and getattr(_sl_real_t, "independent_exit", False):
                                    pass  # independent exit handles its own SL
                                else:
                                    await self._real_manager.update_exchange_sl(trade_id, ev_symbol, new_sl)
                        except Exception:
                            pass
                else:
                    # Close events: mirror exit + AI learning + monitor
                    closed_sig = ev.get("signal", {})
                    if closed_sig:
                        # PARTIAL TP SYNC: when paper hits TP, partial close real too
                        if ev_type in ("tp1_hit", "tp2_hit") and hasattr(self, '_real_manager') and self._real_manager and self._real_manager.enabled:
                            try:
                                _tp_paper_id = closed_sig.get("trade_id", "")
                                _tp_level = 1 if ev_type == "tp1_hit" else 2
                                _tp_close_pct = 0.35
                                if _tp_paper_id:
                                    await self._real_manager.partial_close_real(_tp_paper_id, _tp_level, _tp_close_pct)
                            except Exception as _tp_err:
                                self._log.debug("TP sync failed: %s", _tp_err)

                        try:
                            self._signal_learner.learn_from_outcome(closed_sig)
                        except Exception:
                            pass
                        # RL agent learns from outcome
                        if hasattr(self, '_rl_agent') and self._rl_agent:
                            try:
                                r_mult = closed_sig.get("mfe_r", closed_sig.get("pnl_pct", 0))
                                self._rl_agent.record_outcome(closed_sig, float(r_mult))
                            except Exception:
                                pass
                        try:
                            self._trade_monitor.analyze_trade(closed_sig)
                        except Exception:
                            pass
                        # Mirror exit to real
                        if hasattr(self, '_real_manager') and self._real_manager and self._real_manager.enabled:
                            try:
                                paper_id = closed_sig.get("trade_id", "")
                                exit_price = closed_sig.get("metadata", {}).get("exit_price", 0) or closed_sig.get("exit_price", 0)
                                paper_slip = closed_sig.get("slippage_bps", 0) or 0
                                if paper_id and exit_price:
                                    # Check if real trade manages its own exit
                                    _real_id = self._real_manager.paper_to_real.get(paper_id, "")
                                    _real_trade = self._real_manager.real_trades.get(_real_id)
                                    if _real_trade and getattr(_real_trade, "independent_exit", False):
                                        self._log.debug("SKIP MIRROR: %s has independent exit", paper_id[:12])
                                    else:
                                        await self._real_manager.mirror_paper_exit(
                                        paper_id, exit_price, ev_type,
                                        paper_slippage_bps=float(paper_slip),
                                        symbol=closed_sig.get("symbol", ""),
                                        side=closed_sig.get("side", ""),
                                    )
                            except Exception as exc:
                                self._log.error("WS exit mirror failed: %s", exc)

    async def _on_ws_order_fill(
        self, symbol: str, order_id: str, client_order_id: str,
        fill_price: float, side: str, size: int,
    ) -> None:
        """Private WS callback: SL/TP filled on exchange (instant detection)."""
        self._log.info(
            "WS ORDER FILL: %s %s %d @ %.4f | coid=%s",
            symbol, side, size, fill_price,
            client_order_id[:12] if client_order_id else "-",
        )
        if self._real_manager and self._real_manager.enabled:
            try:
                await self._real_manager.handle_exchange_fill(
                    client_order_id=client_order_id,
                    symbol=symbol,
                    fill_price=fill_price,
                    side=side,
                )
            except Exception as exc:
                self._log.error("WS order fill handler failed: %s", exc)

    async def _on_ws_position_update(
        self, symbol: str, size: int, entry_price: float, pnl: float,
    ) -> None:
        """Private WS callback: position changed (catches liquidations)."""
        if size == 0 and self._real_manager and self._real_manager.enabled:
            self._log.info("WS POSITION CLOSED: %s | pnl=%.4f", symbol, pnl)
            try:
                await self._real_manager.handle_exchange_position_close(symbol, pnl)
            except Exception as exc:
                self._log.error("WS position close handler failed: %s", exc)

    async def _fast_trade_monitor_loop(self) -> None:
        """Dedicated loop for active trade monitoring.

        If WebSocket is connected: runs every 1s (WS provides real-time prices).
        If REST only: runs every 5s (polling fallback).
        """
        ws_active = self._delta_ws is not None and self._delta_ws.is_connected
        interval = 1 if ws_active else self.TRADE_MONITOR_INTERVAL

        self._log.info(
            "Fast trade monitor started (interval=%ds, ws=%s)",
            interval, "YES" if ws_active else "NO",
        )
        last_check = time.monotonic()
        _last_orphan_sync = time.monotonic()
        _ORPHAN_SYNC_INTERVAL = 300  # 5 minutes — NOT per-request

        while not self._stop_event.is_set():
            try:
                # Adaptive interval: 1s with WS, 5s without
                ws_active = self._delta_ws is not None and self._delta_ws.is_connected
                interval = 1 if ws_active else self.TRADE_MONITOR_INTERVAL
                await asyncio.sleep(interval)

                if not self._running:
                    continue

                # ── PERIODIC ORPHAN SYNC ──
                # IMPORTANT: Runs AFTER event processing (below) to give mirror_paper_exit
                # a chance to run first. This prevents the race condition where sync_with_paper
                # orphan-closes trades before mirror_paper_exit can properly close them.
                # Grid bot runs ALWAYS (even with 0 active trades)
                # Signal tracker only runs when there are active trades

                now = time.monotonic()
                time_since_last = now - last_check

                # Warn if monitoring fell behind schedule
                if time_since_last > interval * 3:
                    self._log.warning(
                        "Trade monitor DELAYED: %.1fs since last check (target: %ds)",
                        time_since_last, interval,
                    )

                # Gather current prices — prefer WebSocket, fallback to REST
                prices: dict = {}
                if ws_active and self._ws_prices:
                    prices = dict(self._ws_prices)
                else:
                    for sym in self._symbols:
                        price = self._data_manager.get_latest_price(sym)
                        if price is not None:
                            prices[sym] = price

                if not prices:
                    continue

                # ── GRID BOT: PAUSED — loses money in downtrends ──
                if getattr(self, '_grid_bot_enabled', False):
                    for sym, price in prices.items():
                        candle_data = self._data_manager.get_latest_candle(sym, "5m") if hasattr(self._data_manager, 'get_latest_candle') else None
                        high = candle_data.get("high", price) if candle_data else price
                        low = candle_data.get("low", price) if candle_data else price
                        grid_events = self._grid_bot.update(sym, price, high, low)
                        for gev in grid_events:
                            msg = gev.get("message", "")
                            if gev.get("type") == "grid_fill":
                                self._log.info(msg)

                # Check all active signals for TP/SL hits
                events = self._signal_tracker.update_prices(prices)
                last_check = now

                # PARALLEL REAL EXIT: run real trade exit logic independently
                if hasattr(self, '_real_manager') and self._real_manager and self._real_manager.enabled:
                    try:
                        await self._real_manager.update_real_trades(prices)
                    except Exception as _rte:
                        self._log.debug("Real trade update: %s", _rte)

                for ev in events:
                    msg = ev.get("message", "")
                    ev_type = ev.get("type", "")
                    level = AlertLevel.INFO if "TP" in ev_type.upper() else AlertLevel.WARNING

                    # Enhanced overshoot logging for SL events
                    sig = ev.get("signal", {})
                    if ev_type == "sl_hit":
                        intended_sl = sig.get("stop_loss", 0)
                        actual_exit = sig.get("exit_price", 0)
                        entry = sig.get("entry_price", 0)
                        if intended_sl > 0 and entry > 0:
                            overshoot = abs(actual_exit - intended_sl)
                            overshoot_pct = (overshoot / entry) * 100
                            self._log.warning(
                                "SL OVERSHOOT: %s | intended=%.2f actual=%.2f | "
                                "overshoot=%.2f (%.4f%%) | check_interval=%.1fs",
                                sig.get("symbol", ""), intended_sl, actual_exit,
                                overshoot, overshoot_pct, time_since_last,
                            )

                    # ── SL UPDATE → sync to exchange ──
                    if ev_type == "sl_updated" and hasattr(self, '_real_manager') and self._real_manager and self._real_manager.enabled:
                        try:
                            trade_id = ev.get("trade_id", "")
                            new_sl = ev.get("new_sl", 0)
                            symbol = ev.get("symbol", "")
                            if trade_id and new_sl > 0:
                                # Skip SL sync if real trade manages its own exit
                                _sl2_real_id = self._real_manager.paper_to_real.get(trade_id, "")
                                _sl2_real_t = self._real_manager.real_trades.get(_sl2_real_id)
                                if _sl2_real_t and getattr(_sl2_real_t, "independent_exit", False):
                                    pass  # independent exit handles its own SL
                                else:
                                    await self._real_manager.update_exchange_sl(trade_id, symbol, new_sl)
                        except Exception as exc:
                            self._log.debug("Exchange SL sync failed: %s", exc)
                        continue  # sl_updated is not a close event, skip rest

                    self._log.info("Fast Monitor: %s", msg)
                    try:
                        await self._alerts.send_system_alert(msg, level=level)
                    except Exception as exc:
                        self._log.debug("Alert send failed: %s", exc)

                    # AI learning + trade monitor + real exit mirror on ALL closures
                    closed_sig = ev.get("signal", {})
                    try:
                        self._signal_learner.learn_from_outcome(closed_sig)
                    except Exception as exc:
                        self._log.warning("Signal learner failed: %s", exc)
                    try:
                        self._trade_monitor.analyze_trade(closed_sig)
                    except Exception as exc:
                        self._log.warning("Trade monitor analysis failed: %s", exc)

                    # Notify strategy of trade close (per-symbol cooling)
                    try:
                        pnl = closed_sig.get("metadata", {}).get("pnl_usd", closed_sig.get("pnl_usd", 0))
                        sym = closed_sig.get("symbol", "")
                        if sym and hasattr(self._strategy, "notify_trade_close"):
                            self._strategy.notify_trade_close(sym, pnl or 0)
                    except Exception:
                        pass

                    # Mirror exit to real exchange — ALL exit types
                    if hasattr(self, '_real_manager') and self._real_manager and self._real_manager.enabled:
                        try:
                            paper_id = closed_sig.get("trade_id", "")
                            exit_price = closed_sig.get("metadata", {}).get("exit_price", 0) or closed_sig.get("exit_price", 0)
                            paper_slip = closed_sig.get("slippage_bps", closed_sig.get("metadata", {}).get("slippage_bps", 0)) or 0
                            if paper_id and exit_price:
                                # Check if real trade manages its own exit
                                _r2_id = self._real_manager.paper_to_real.get(paper_id, "")
                                _r2_trade = self._real_manager.real_trades.get(_r2_id)
                                if _r2_trade and getattr(_r2_trade, "independent_exit", False):
                                    self._log.debug("SKIP MIRROR (fast): %s has independent exit", paper_id[:12])
                                else:
                                    paper_symbol = closed_sig.get("symbol", "")
                                    paper_side_str = closed_sig.get("side", "")
                                    await self._real_manager.mirror_paper_exit(
                                        paper_id, exit_price, ev_type,
                                        paper_slippage_bps=float(paper_slip),
                                        symbol=paper_symbol,
                                        side=paper_side_str,
                                    )
                        except Exception as exc:
                            self._log.error("Real exit mirror failed: %s", exc)

                # Record heartbeat
                self._heartbeat.record_activity("fast_trade_monitor")

                # RCA Agent — periodic performance analysis (every 30 min)
                if hasattr(self, '_rca_agent') and self._rca_agent and self._rca_agent.should_run():
                    try:
                        rca_report = self._rca_agent.run_analysis()
                        if rca_report.get("applied"):
                            self._log.warning("RCA AUTO-TUNE: %d parameters adjusted", len(rca_report["applied"]))
                    except Exception as _rca_err:
                        self._log.debug("RCA analysis failed: %s", _rca_err)

                # ── PERIODIC ORPHAN SYNC (AFTER event processing) ──
                # Runs after mirror_paper_exit has had a chance to handle exits properly.
                # Only syncs every 5 minutes to avoid hammering.
                if (time.monotonic() - _last_orphan_sync) >= _ORPHAN_SYNC_INTERVAL:
                    _last_orphan_sync = time.monotonic()
                    if hasattr(self, '_real_manager') and self._real_manager and self._real_manager.enabled:
                        try:
                            active_ids = {ts.trade_id for ts in self._signal_tracker._active.values()}
                            # Build closed paper trades dict with exit prices
                            closed_paper = {}
                            if hasattr(self._signal_tracker, '_closed_recently'):
                                closed_paper = dict(self._signal_tracker._closed_recently)
                            self._real_manager.sync_with_paper(active_ids, closed_paper)
                        except Exception as exc:
                            self._log.debug("Periodic orphan sync failed: %s", exc)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log.debug("Fast trade monitor error: %s", exc)
                await asyncio.sleep(2)

        self._log.info("Fast trade monitor stopped")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _main_loop(self) -> None:
        """Core event loop.

        Waits on two recurring activities:
        1. Candle-close events (handled via ``_on_candle_close`` callback).
        2. Periodic position / housekeeping checks.
        """
        self._log.info("Entering main loop")

        while not self._stop_event.is_set():
            try:
                # Wait briefly, then run housekeeping
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self.POSITION_CHECK_INTERVAL,
                    )
                    # If we get here, stop was requested
                    break
                except asyncio.TimeoutError:
                    pass  # Normal timeout - proceed with housekeeping

                now = time.monotonic()

                # -- Check open positions (TP / SL / trailing stops) --
                await self._check_open_positions()

                # -- Periodic state save --
                if now - self._last_state_save >= self.STATE_SAVE_INTERVAL:
                    await self._save_state()
                    self._last_state_save = now

                # -- Daily risk counter reset at midnight UTC --
                await self._maybe_reset_daily_counters()

                # -- Heartbeat activity (record every cycle to avoid false "stale") --
                self._heartbeat.record_activity("main_loop")

                # -- Periodic heartbeat log (human-readable) --
                if now - self._last_heartbeat_log >= self.HEARTBEAT_LOG_INTERVAL:
                    mem_mb = self._get_memory_mb()
                    self._log.info(
                        "Heartbeat | uptime=%ds | symbols=%d | mode=%s | mem=%.0fMB",
                        int(self.uptime),
                        len(self._symbols),
                        self._mode.value,
                        mem_mb,
                    )
                    self._heartbeat.record_activity("heartbeat_log")
                    self._last_heartbeat_log = now

                # -- Update dashboard --
                try:
                    await self._update_dashboard()
                except Exception:
                    self._log.debug("Dashboard update failed", exc_info=True)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._handle_exception(exc, context="main_loop")
                # Back off before retrying the loop
                await asyncio.sleep(5)

        self._log.info("Main loop exited")

    # ------------------------------------------------------------------
    # Dashboard updates
    # ------------------------------------------------------------------

    @staticmethod
    def _get_memory_mb() -> float:
        """Return current process RSS in MB (Linux /proc/self/status)."""
        try:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) / 1024.0
        except Exception:
            pass
        # Fallback via os (less accurate but works everywhere)
        try:
            import resource
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
        except Exception:
            return 0.0

    async def _update_dashboard(self) -> None:
        """Push latest state to the dashboard server."""
        # -- Periodically fetch real exchange balance --
        now_ts = time.time()
        if now_ts - self._last_balance_fetch >= self._balance_fetch_interval:
            self._last_balance_fetch = now_ts
            try:
                bal = await self._exchange.fetch_balance()
                total = bal.total if hasattr(bal, 'total') else {}
                usd_total = total.get("USD") or total.get("USDT") or total.get("INR")
                if usd_total is not None and float(usd_total) > 0:
                    self._signal_tracker.set_exchange_balance(float(usd_total))
                    self._log.info("Exchange balance: $%.2f", float(usd_total))

                # --- Periodic reconciliation (every 5 min) ---
                _now_ts = time.time()
                if not hasattr(self, "_last_reconcile_ts"):
                    self._last_reconcile_ts = _now_ts
                if _now_ts - self._last_reconcile_ts >= 60:  # every 60s (was 300s)  # 5 minutes
                    self._last_reconcile_ts = _now_ts
                    if hasattr(self, "_real_manager") and self._real_manager:
                        try:
                            await self._real_manager.reconcile_exchange_positions()
                        except Exception as _rec_err:
                            self._log.warning("Periodic reconcile failed: %s", _rec_err)
            except Exception as exc:
                self._log.warning("Balance fetch failed: %s", exc)

        # Gather current prices from the data manager
        prices: dict = {}
        for sym in self._symbols:
            price = self._data_manager.get_latest_price(sym)
            if price is not None:
                prices[sym] = price

        if prices:
            await self._dashboard.update_prices(prices)

        # -- Check tracked signals for TP/SL hits --
        if prices and self._signal_tracker.active_count > 0:
            try:
                events = self._signal_tracker.update_prices(prices)
                for ev in events:
                    msg = ev.get("message", "")
                    ev_type = ev.get("type", "")
                    level = AlertLevel.INFO if "TP" in ev_type.upper() else AlertLevel.WARNING
                    self._log.info("Signal Tracker: %s", msg)
                    try:
                        await self._alerts.send_system_alert(msg, level=level)
                    except Exception as exc:
                        self._log.debug("Alert send failed: %s", exc)

                    # -- AI Learning: learn from closed signals (SL, TP3, expired) --
                    if ev_type in ("sl_hit", "tp3_hit", "expired"):
                        try:
                            closed_sig = ev.get("signal", {})
                            self._signal_learner.learn_from_outcome(closed_sig)
                        except Exception as exc:
                            self._log.debug("AI learning failed: %s", exc)

                        # -- Trade Monitor: analyze P&L and categorize losses --
                        try:
                            self._trade_monitor.analyze_trade(closed_sig)
                        except Exception as exc:
                            self._log.debug("Trade monitor analysis failed: %s", exc)

            except Exception as exc:
                self._log.debug("Signal tracker update failed: %s", exc)

        # Update dashboard with tracker stats (always, not just when active signals exist)
        try:
            stats = self._signal_tracker.get_stats()

            # Calculate today's stats from daily_pnl breakdown
            from datetime import datetime, timezone
            _today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            _daily = stats.get("daily_pnl", {}).get(_today_str, {})
            _today_trades = _daily.get("trades", 0)
            _today_pnl = _daily.get("net_pnl", 0.0)

            # Max drawdown from monitor
            _max_dd = 0.0
            try:
                _max_dd = self._trade_monitor._metrics.get("max_drawdown", 0.0)
            except Exception:
                pass

            await self._dashboard.update_performance(
                win_rate=stats.get("win_rate", 0),
                total_pnl=stats.get("total_pnl", 0),
                trades_today=_today_trades,
                daily_pnl=_today_pnl,
                max_drawdown=_max_dd,
                wins=stats.get("wins", 0),
                losses=stats.get("losses", 0),
            )

            # Update scanner weights from R-performance data
            try:
                by_setup = stats.get("by_setup", {})
                if by_setup and hasattr(self._strategy, '_scalp'):
                    wm = self._strategy._scalp._weight_manager
                    wm.update_weights(by_setup)
                    # Check shadow recovery for suppressed scanners
                    try:
                        recoveries = wm.check_shadow_recovery()
                        if recoveries:
                            self._log.info("Shadow recoveries: %s", recoveries)
                    except Exception:
                        pass
            except Exception:
                pass
        except Exception as exc:
            self._log.debug("Tracker stats update failed: %s", exc)

        # -- Update Decision Engine --
        try:
            # Gather inputs for decision engine
            regime_info = {}
            scan_results = []
            scanner_health = []
            funnel = {}
            session = ""
            signals_hr = 0

            if hasattr(self._strategy, '_scalp'):
                scalp = self._strategy._scalp
                regime_info = getattr(scalp, '_last_regime_info', {})
                funnel = getattr(scalp, '_funnel', {})
                session = getattr(scalp, '_current_session', '')
                signals_hr = len(getattr(scalp, '_signal_count_hr', []))
                # Get scan results from last_scan_status
                for sym, status in scalp.last_scan_status.items():
                    for s in status.get("setups_checked", []):
                        s["symbol"] = sym
                        scan_results.append(s)
                if hasattr(scalp, '_weight_manager'):
                    scanner_health = scalp._weight_manager.get_dashboard_summary()

            r_metrics = {}
            if self._signal_tracker:
                stats = self._signal_tracker.get_stats()
                r_metrics = stats.get("r_metrics", {})

            risk_guard = {}
            try:
                should_pause, reason = self._trade_monitor.should_pause_trading()
                risk_guard = {
                    "should_pause": should_pause,
                    "reason": reason,
                    "drawdown": self._trade_monitor._metrics.get("current_drawdown", 0),
                }
            except Exception as _shutdown_exc:
                self._log.debug("Shutdown cleanup: %s", _shutdown_exc)

            # Get EV data from strategy's EV engine
            ev_data = {}
            if hasattr(self._strategy, '_scalp'):
                scalp = self._strategy._scalp
                if hasattr(scalp, '_ev_engine'):
                    ev_data = scalp._ev_engine.get_dashboard_summary()
                # Feed by_setup stats to strategy for EV computation
                if hasattr(scalp, '_cached_by_setup'):
                    stats = self._signal_tracker.get_stats() if self._signal_tracker else {}
                    scalp._cached_by_setup = stats.get("by_setup", {})

            self._decision_engine.update(
                scan_results=scan_results,
                regime_info=regime_info,
                r_metrics=r_metrics,
                risk_guard=risk_guard,
                scanner_health=scanner_health,
                session=session,
                signals_this_hour=signals_hr,
                funnel=funnel,
                ev_data=ev_data,
            )
        except Exception as exc:
            self._log.debug("Decision engine update failed: %s", exc)

        # System health with real memory tracking
        from datetime import timedelta
        _IST = timezone(timedelta(hours=5, minutes=30))
        now_str = datetime.now(_IST).strftime("%H:%M:%S IST")
        mem_mb = self._get_memory_mb()
        await self._dashboard.update_system_health(
            last_data_update=now_str,
            memory_mb=round(mem_mb, 1),
        )

        # Bot state
        strat = self._config.get("strategy", {})
        active_strat = strat.get("active", "multi_indicator_confluence") if isinstance(strat, dict) else "multi_indicator_confluence"
        await self._dashboard.set_state(
            bot_status="running" if self._running else "stopped",
            exchange_status="connected",
            active_strategy=active_strat,
            symbols=self._symbols,
            mode=self._mode.value,
        )

    # ------------------------------------------------------------------
    # Candle close handler
    # ------------------------------------------------------------------

    async def _safe_mirror_trade(self, symbol, sig_dict, paper_trade_id):
        """Mirror trade to real exchange — runs as background task."""
        try:
            await self._real_manager.mirror_paper_trade(
                symbol, sig_dict, paper_trade_id,
            )
        except Exception as exc:
            self._log.error("Real trade mirror failed (background): %s", exc)

    async def _on_candle_close(self, *, symbol: str, timeframe: str, candle: dict) -> None:
        """Callback invoked by the DataFeed when a candle closes.

        This is the primary trigger for strategy evaluation and trade decisions.
        """
        if not self._running:
            return

        # Circuit-breaker: skip symbols with too many consecutive errors
        if self._symbol_errors.get(symbol, 0) >= self._max_symbol_errors:
            self._log.warning(
                "Symbol %s circuit-breaker open (%d errors) - skipping",
                symbol,
                self._symbol_errors[symbol],
            )
            return

        self._heartbeat.record_activity(f"candle_close:{symbol}")

        try:
            self._log.info(
                "Candle close: %s %s | C=%.2f V=%.4f",
                symbol,
                timeframe,
                candle.get("close", 0),
                candle.get("volume", 0),
            )

            # 1. Update candle in data manager (sync method)
            self._data_manager.update_candle(symbol, timeframe, candle)

            # 2. Build candles dict from data manager for strategy
            tf_cfg = self._config.get("timeframes", {})
            timeframes_list = list(dict.fromkeys(
                v for v in (tf_cfg if isinstance(tf_cfg, dict) else {}).values()
                if isinstance(v, str)
            )) or [timeframe]
            candles_dict = {}
            for tf in timeframes_list:
                df = self._data_manager.get_candles(symbol, tf)
                if df is not None and len(df) > 0:
                    candles_dict[tf] = df

            # 3. Run strategy analysis (sync method)
            # Upgrade 2: feed candles to signal tracker for Chandelier Exit
            _df5m = candles_dict.get("5m")
            if _df5m is not None and hasattr(self._signal_tracker, 'update_candles'):
                self._signal_tracker.update_candles(symbol, _df5m)

            signals = self._strategy.analyze(symbol, candles_dict)

            if not signals:
                self._log.info("No signals for %s on %s (candles: %s)", symbol, timeframe,
                              {tf: len(df) for tf, df in candles_dict.items()})
                # Reset error counter on success
                self._symbol_errors[symbol] = 0
                return

            # 3. Process each signal
            for sig in signals:
                await self._process_signal(symbol, sig)

            # Reset error counter on success
            self._symbol_errors[symbol] = 0

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._symbol_errors[symbol] = self._symbol_errors.get(symbol, 0) + 1
            self._handle_exception(
                exc,
                context=f"on_candle_close({symbol}, {timeframe})",
            )

    # ------------------------------------------------------------------
    # Signal processing
    # ------------------------------------------------------------------

    async def _process_signal(self, symbol: str, signal) -> None:
        """Evaluate a single signal: risk-check, execute, alert, journal."""
        # Signal may be a Signal dataclass or a dict; normalise to dict
        # Prefer to_dict() which handles serialization of enums/datetime
        if hasattr(signal, 'to_dict'):
            sig_dict = signal.to_dict()
        elif hasattr(signal, '__dict__') and not isinstance(signal, dict):
            sig_dict = {}
            for k, v in signal.__dict__.items():
                if hasattr(v, 'value'):  # enum
                    sig_dict[k] = v.value
                elif hasattr(v, 'isoformat'):  # datetime
                    sig_dict[k] = v.isoformat()
                else:
                    sig_dict[k] = v
        else:
            sig_dict = dict(signal) if isinstance(signal, dict) else {"type": str(signal)}

        sig_dict.setdefault("symbol", symbol)
        signal_type = sig_dict.get("type", "unknown")
        if hasattr(signal_type, 'value'):
            signal_type = signal_type.value

        # -- HARD BLOCKS: momentum_trend + dead zone + zero confidence --
        meta = sig_dict.get("metadata", {})
        scanner = meta.get("setup_type", meta.get("scanner", ""))

        # Block momentum_trend / investment strategy signals
        if scanner in ("momentum_trend", "simple_bias", "investment") or signal_type == "investment":
            self._log.info("BLOCKED: %s %s — momentum_trend/investment not allowed", symbol, scanner)
            return

        # Block zero-confidence signals (unattributed)
        if sig_dict.get("confidence", 0) <= 0 and scanner not in ("structure_bounce", "bos_choch", "liquidity_sweep", "cvd_divergence"):
            self._log.info("BLOCKED: %s — zero confidence, scanner=%s", symbol, scanner)
            return

        # Dead zone filter: UTC 21-05 requires higher confidence
        import datetime as _dt
        _utc_hour = _dt.datetime.now(_dt.timezone.utc).hour
        if _utc_hour >= 22 or _utc_hour < 3:  # IST 3:30AM-8:30AM (was 2:30AM-10:30AM — blocked India morning)
            _dead_zone_min_conf = 75  # require higher confidence in dead hours
            if sig_dict.get("confidence", 0) < _dead_zone_min_conf:
                self._log.info("BLOCKED: %s — dead zone (UTC %d:00) conf=%d < %d",
                             symbol, _utc_hour, sig_dict.get("confidence", 0), _dead_zone_min_conf)
                return

        # -- Sync loss streak to AI learner for confidence reduction --
        try:
            self._signal_learner.set_current_streak(
                self._trade_monitor._metrics.get("current_streak", 0)
            )
        except Exception:
            pass

        # -- AI Learning: Adjust confidence based on historical performance --
        original_conf = sig_dict.get("confidence", 0)
        try:
            adjusted_conf, ai_reason = self._signal_learner.adjust_confidence(sig_dict)
            if adjusted_conf != original_conf:
                sig_dict["confidence"] = adjusted_conf
                sig_dict.setdefault("metadata", {})["ai_adjustment"] = ai_reason
                sig_dict["metadata"]["original_confidence"] = original_conf
                self._log.info(
                    "AI adjusted confidence: %s %s | %d → %d | %s",
                    symbol, signal_type, original_conf, adjusted_conf, ai_reason,
                )
                # Recalculate grade based on boosted confidence
                from config.constants import confidence_to_grade
                new_grade = confidence_to_grade(adjusted_conf)
                old_grade = sig_dict.get("grade", "")
                if str(new_grade.value) != str(old_grade):
                    sig_dict["grade"] = new_grade.value if hasattr(new_grade, "value") else str(new_grade)
                    self._log.info(
                        "Grade upgraded: %s %s | %s → %s (AI boosted conf %d → %d)",
                        symbol, signal_type, old_grade, sig_dict["grade"], original_conf, adjusted_conf,
                    )
        except Exception:
            pass

        self._log.info(
            "Signal: %s %s | grade=%s confidence=%.2f",
            symbol,
            signal_type,
            sig_dict.get("grade", "?"),
            sig_dict.get("confidence", 0),
        )

        # -- Risk check --
        approved, reason = self._risk_manager.check_entry_allowed(sig_dict)
        if not approved:
            self._log.info(
                "Signal REJECTED by risk manager: %s %s - %s",
                symbol,
                signal_type,
                reason,
            )
            await self._alerts.send_system_alert(
                f"Signal Rejected: {symbol} {signal_type} — {reason}",
                level=AlertLevel.WARNING,
            )
            return

        # -- Inject drawdown level for graduated defense --
        try:
            sig_dict["_dd_pct"] = self._risk_manager.current_dd_pct
        except Exception:
            sig_dict["_dd_pct"] = 0.0

        # -- Push signal to dashboard --
        try:
            await self._dashboard.add_signal(sig_dict)
        except Exception as exc:
            self._log.debug("Failed to push signal to dashboard: %s", exc)

        # -- Track signal for TP/SL closure and P&L --
        try:
            # Pass order_type so from_signal can compute fees correctly
            sig_dict["_order_type"] = getattr(self._signal_tracker, "_order_type", "maker")
            self._signal_tracker.track_signal(sig_dict)

            # RL Shadow Agent — log sizing/trail suggestion
            if hasattr(self, '_rl_agent') and self._rl_agent:
                try:
                    rl_pred = self._rl_agent.predict(sig_dict)
                    self._log.info(
                        "RL SHADOW: %s %s | sizing=%.2fx trail=%.2f | %s",
                        symbol, sig_dict.get("side", "?"),
                        rl_pred["sizing_mult"], rl_pred["trail_aggression"],
                        "APPLIED" if not rl_pred["shadow_mode"] else "shadow_only",
                    )
                except Exception:
                    pass
        except Exception as exc:
            self._log.error("Failed to track signal: %s", exc, exc_info=True)

        # -- Alert on signal --
        try:
            await self._alerts.send_signal_alert(sig_dict)
        except Exception as exc:
            self._log.debug("Failed to send signal alert: %s", exc)

        # -- Stale price check --
        is_fresh, age = check_price_freshness(sig_dict)
        if not is_fresh:
            self._log.warning("STALE PRICE: %s signal is %.1fs old — skipping", symbol, age)
            return

        # -- Emergency stop check --
        if hasattr(self, '_dashboard') and getattr(self._dashboard, '_emergency_stop', False):
            self._log.critical("EMERGENCY STOP active — blocking %s %s", symbol, signal_type)
            return

        # -- Execute (if mode permits) --
        order_result = None
        if self._mode in (BotMode.PAPER, BotMode.LIVE, BotMode.FORWARD_TEST):
            try:
                order_result = await self._execution.execute(symbol, sig_dict)
                self._log.info(
                    "Order executed: %s %s -> %s",
                    symbol,
                    signal_type,
                    order_result,
                )
                # Audit trail — immutable record
                audit_log("TRADE_ENTRY", {
                    "symbol": symbol,
                    "side": sig_dict.get("side"),
                    "entry_price": sig_dict.get("entry_price"),
                    "stop_loss": sig_dict.get("stop_loss"),
                    "confidence": sig_dict.get("confidence"),
                    "scanner": sig_dict.get("metadata", {}).get("setup_type"),
                    "mode": self._mode.value,
                })
            except Exception as exc:
                self._handle_exception(exc, context=f"execute({symbol})")
                await self._alerts.send_system_alert(
                    f"Execution Error: {symbol} {signal_type} — {exc}",
                    level=AlertLevel.ERROR,
                )
                return

        # -- Fire real trade in parallel (don't wait for paper to complete first) --
        # This was sequential before — real trade started 300-700ms AFTER signal.
        # Now both fire simultaneously, reducing entry delay to near-zero.
        if hasattr(self, '_real_manager') and self._real_manager and self._real_manager.enabled:
            try:
                paper_trade_id = None
                if order_result and hasattr(order_result, 'trade_id'):
                    paper_trade_id = order_result.trade_id
                elif isinstance(order_result, dict):
                    paper_trade_id = order_result.get('trade_id')
                # Fire and forget — don't await, let it run in background
                import asyncio
                asyncio.create_task(
                    self._safe_mirror_trade(symbol, sig_dict, paper_trade_id)
                )
            except Exception as exc:
                self._log.error("Real trade mirror failed (paper unaffected): %s", exc)

        # -- Journal --
        try:
            self._journal.record_trade({
                "symbol": symbol,
                "signal_type": signal_type,
                "signal": sig_dict,
                "order_result": order_result,
                "mode": self._mode.value,
            })
        except Exception as exc:
            self._log.debug("Journal record failed: %s", exc)

    # ------------------------------------------------------------------
    # Open position management
    # ------------------------------------------------------------------

    async def _check_open_positions(self) -> None:
        """Monitor open positions for TP/SL hits and trailing stop updates."""
        try:
            positions_dict = self._state.load_positions()
        except Exception:
            self._log.debug("Could not load open positions", exc_info=True)
            return

        if not positions_dict:
            return

        # load_positions() returns {symbol: position_dict}
        positions = []
        for sym, pos in positions_dict.items():
            if sym.startswith("_"):
                continue  # skip metadata entries like _meta
            if isinstance(pos, dict):
                pos.setdefault("symbol", sym)
                positions.append(pos)

        for position in positions:
            symbol = position.get("symbol")
            if not symbol:
                continue

            try:
                await self._evaluate_position(position)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._handle_exception(
                    exc, context=f"check_position({symbol})"
                )

    async def _evaluate_position(self, position: dict) -> None:
        """Evaluate a single open position for exit conditions."""
        symbol = position["symbol"]
        side = position.get("side", "long")

        # Get current price
        try:
            current_price = await self._exchange.get_price(symbol)
        except Exception:
            self._log.warning("Could not fetch price for %s", symbol)
            return

        entry_price = position.get("entry_price", 0)
        stop_loss = position.get("stop_loss")
        take_profits = position.get("take_profits", [])
        trailing_stop = position.get("trailing_stop")

        if not entry_price:
            return

        # Determine P&L direction
        is_long = side == "long"
        pnl_pct = ((current_price - entry_price) / entry_price) * (1 if is_long else -1)

        # -- Check stop loss --
        if stop_loss:
            sl_hit = (current_price <= stop_loss) if is_long else (current_price >= stop_loss)
            if sl_hit:
                self._log.warning(
                    "STOP LOSS HIT: %s @ %.4f (SL=%.4f, entry=%.4f, PnL=%.2f%%)",
                    symbol, current_price, stop_loss, entry_price, pnl_pct * 100,
                )
                await self._execute_exit(position, "stop_loss", current_price)
                return

        # -- Check take profit levels --
        for i, tp in enumerate(take_profits):
            tp_price = tp.get("price", 0)
            tp_hit_already = tp.get("hit", False)
            if tp_hit_already or not tp_price:
                continue
            tp_hit = (current_price >= tp_price) if is_long else (current_price <= tp_price)
            if tp_hit:
                tp_label = f"TP{i + 1}"
                self._log.info(
                    "%s HIT: %s @ %.4f (target=%.4f, PnL=%.2f%%)",
                    tp_label, symbol, current_price, tp_price, pnl_pct * 100,
                )
                tp["hit"] = True
                tp_pct = tp.get("close_pct", 0.33)  # fraction of position to close
                await self._execute_partial_exit(
                    position, tp_label, current_price, tp_pct
                )

        # -- Update trailing stop --
        if trailing_stop:
            await self._update_trailing_stop(position, current_price, is_long)

    async def _execute_exit(
        self, position: dict, reason: str, price: float
    ) -> None:
        """Fully close a position."""
        symbol = position["symbol"]
        if self._mode in (BotMode.PAPER, BotMode.LIVE, BotMode.FORWARD_TEST):
            try:
                result = await self._execution.close_position(symbol, position)
                self._log.info("Position closed: %s reason=%s result=%s", symbol, reason, result)
            except Exception as exc:
                self._handle_exception(exc, context=f"close_position({symbol})")
                return

        await self._alerts.send_system_alert(
            f"Position Closed: {symbol} | Reason: {reason} | Price: {price}",
            level=AlertLevel.WARNING,
        )
        # Remove from state (sync)
        try:
            positions = self._state.load_positions()
            if symbol in positions:
                del positions[symbol]
                self._state.save_all()
        except Exception:
            self._log.debug("Failed to remove position from state", exc_info=True)

    async def _execute_partial_exit(
        self, position: dict, reason: str, price: float, close_pct: float
    ) -> None:
        """Partially close a position at a take-profit level."""
        symbol = position["symbol"]
        if self._mode in (BotMode.PAPER, BotMode.LIVE, BotMode.FORWARD_TEST):
            try:
                result = await self._execution.partial_close(
                    symbol, position, close_pct
                )
                self._log.info(
                    "Partial close (%.0f%%): %s reason=%s result=%s",
                    close_pct * 100, symbol, reason, result,
                )
            except Exception as exc:
                self._handle_exception(exc, context=f"partial_close({symbol})")
                return

        await self._alerts.send_system_alert(
            f"Partial Exit: {symbol} ({reason}) | Close: {close_pct:.0%} | Price: {price}",
            level=AlertLevel.INFO,
        )

    async def _update_trailing_stop(
        self, position: dict, current_price: float, is_long: bool
    ) -> None:
        """Ratchet the trailing stop in the direction of profit."""
        trailing = position.get("trailing_stop", {})
        callback_pct = trailing.get("callback_pct", 0.01)  # e.g. 1%
        current_stop = trailing.get("stop_price")
        highest = trailing.get("highest", current_price)
        lowest = trailing.get("lowest", current_price)

        if is_long:
            if current_price > highest:
                trailing["highest"] = current_price
                new_stop = current_price * (1 - callback_pct)
                if current_stop is None or new_stop > current_stop:
                    trailing["stop_price"] = new_stop
                    self._log.debug(
                        "Trailing stop updated: %s -> %.4f",
                        position["symbol"], new_stop,
                    )

            # Check if trailing stop hit
            if current_stop and current_price <= current_stop:
                self._log.warning(
                    "TRAILING STOP HIT: %s @ %.4f (trail=%.4f)",
                    position["symbol"], current_price, current_stop,
                )
                await self._execute_exit(position, "trailing_stop", current_price)
        else:
            if current_price < lowest:
                trailing["lowest"] = current_price
                new_stop = current_price * (1 + callback_pct)
                if current_stop is None or new_stop < current_stop:
                    trailing["stop_price"] = new_stop
                    self._log.debug(
                        "Trailing stop updated: %s -> %.4f",
                        position["symbol"], new_stop,
                    )

            if current_stop and current_price >= current_stop:
                self._log.warning(
                    "TRAILING STOP HIT: %s @ %.4f (trail=%.4f)",
                    position["symbol"], current_price, current_stop,
                )
                await self._execute_exit(position, "trailing_stop", current_price)

        position["trailing_stop"] = trailing

    # ------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------

    async def _recover_state(self) -> None:
        """Restore state from the last checkpoint after a restart."""
        self._log.info("Recovering state from last checkpoint...")
        try:
            positions = self._state.load_positions()
            if positions:
                # Filter out metadata entries (keys starting with _)
                real_positions = {
                    sym: pos for sym, pos in positions.items()
                    if not sym.startswith("_") and isinstance(pos, dict)
                }
                if real_positions:
                    self._log.info(
                        "Recovered %d open position(s) from state",
                        len(real_positions),
                    )
                    for sym, pos in real_positions.items():
                        self._log.info(
                            "  Restored position: %s %s entry=%.4f",
                            sym,
                            pos.get("side", "?"),
                            pos.get("entry_price", 0),
                        )
                else:
                    self._log.info("No open positions found - starting fresh")
            else:
                self._log.info("No previous state found - starting fresh")
        except Exception:
            self._log.exception("State recovery failed - starting fresh")

    async def _save_state(self) -> None:
        """Persist current state to disk for crash recovery."""
        try:
            self._state.save_bot_state({
                "mode": self._mode.value,
                "symbols": self._symbols,
                "uptime": self.uptime,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            self._state.save_all()
            positions = self._state.load_positions()
            self._log.debug("State saved (%d positions)", len(positions or {}))
        except Exception:
            self._log.exception("Failed to save state")

    async def _maybe_reset_daily_counters(self) -> None:
        """Reset daily risk counters at midnight UTC."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._last_daily_reset == today:
            return
        self._last_daily_reset = today
        try:
            self._risk_manager.reset_daily()
            self._log.info("Daily risk counters reset for %s", today)
        except Exception:
            self._log.exception("Failed to reset daily counters")

    # ------------------------------------------------------------------
    # Error handling
    # ------------------------------------------------------------------

    def _handle_exception(self, exc: Exception, context: str = "") -> None:
        """Log an exception safely without crashing the bot."""
        self._log.error(
            "Exception in %s: %s\n%s",
            context or "unknown",
            exc,
            traceback.format_exc(),
        )
        self._heartbeat.record_error(context, str(exc))
