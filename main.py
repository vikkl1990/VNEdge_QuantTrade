#!/usr/bin/env python3
"""
Crypto Trading Bot - Main Entry Point
======================================

Launches the trading bot in the specified operating mode.

Usage
-----
  # Signal-only mode (default) - generates alerts, no trades
  python main.py --mode signal_only

  # Paper trading with custom config
  python main.py --mode paper --config config/settings_paper.yaml

  # Live trading for specific symbols
  python main.py --mode live --symbols BTCUSDT,ETHUSDT

  # Backtest mode
  python main.py --mode backtest --symbols BTCUSDT

  # Forward test (paper trades on live data with full logging)
  python main.py --mode forward_test

Environment Variables
---------------------
  BOT_MODE        - Override operating mode
  LOG_LEVEL       - Override log level (DEBUG, INFO, WARNING, ERROR)
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID - Telegram alert credentials
"""

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path

# Ensure the project root is on sys.path so relative package imports work
# regardless of how the script is invoked.
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.loader import load_config as load_config_dict
from config.constants import BotMode
from logger import setup_logger
from bot.orchestrator import BotOrchestrator
from bot.heartbeat import HeartbeatMonitor
from exchange.factory import create_exchange_client
from data.manager import DataManager
from data.feed import DataFeed
from strategies.multi_strategy import MultiStrategy
from risk import RiskManager
from execution.engine import ExecutionEngine
from execution.paper_engine import PaperExecutionEngine
from alerts import AlertManager
from journal import TradeJournal
from dashboard import DashboardServer
from utils.state_manager import StateManager
from backtest import BacktestEngine


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="crypto-trading-bot",
        description="Crypto Trading Bot - multi-strategy signal generation and execution.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--mode",
        type=str,
        default=None,
        choices=[m.value for m in BotMode],
        help="Operating mode (default: from config or signal_only).",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to YAML configuration file (default: config/settings.yaml).",
    )
    parser.add_argument(
        "--symbols",
        type=str,
        default=None,
        help="Comma-separated list of trading symbols to override config.",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Component factory
# ---------------------------------------------------------------------------

def _resolve_mode(args, config):
    """Determine the bot mode from CLI args, env, or config (in priority order)."""
    if args.mode:
        return BotMode.from_str(args.mode)
    env_mode = os.environ.get("BOT_MODE")
    if env_mode:
        return BotMode.from_str(env_mode)
    cfg_mode = config.get("bot", {}).get("mode", "signal_only")
    return BotMode.from_str(cfg_mode)


def _resolve_symbols(args, config):
    """Determine the symbol list from CLI args or config."""
    if args.symbols:
        return [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    return config.get("symbols", config.get("bot", {}).get("symbols", ["BTCUSDT"]))


async def build_components(config, mode, symbols, logger):
    """Instantiate all subsystem components based on the operating mode.

    Returns a dict of component name -> instance, suitable for unpacking
    into the BotOrchestrator constructor.
    """
    logger.info("Building components for mode=%s, symbols=%s", mode.value, symbols)

    # Convert Config dataclass to dict if needed for modules that expect dicts
    if hasattr(config, '_to_dict'):
        config_dict = config._to_dict()
    elif isinstance(config, dict):
        config_dict = config
    else:
        config_dict = {}

    # -- Exchange --
    exchange = create_exchange_client()

    # -- Data layer --
    data_manager = DataManager()
    data_feed = DataFeed(data_manager)

    # -- Strategy (runs both investment + scalp) --
    strategy = MultiStrategy(config_dict)

    # -- Risk management --
    risk_manager = RiskManager(config_dict)

    # -- Execution engine (live vs paper) --
    if mode in (BotMode.LIVE,):
        execution_engine = ExecutionEngine(config_dict, exchange)
        logger.warning("*** LIVE EXECUTION ENGINE ACTIVE - real orders will be placed ***")
    else:
        execution_engine = PaperExecutionEngine(config_dict)
        logger.info("Paper execution engine active - no real orders.")

    # -- Alerts --
    alert_manager = AlertManager(config_dict)

    # -- Trade journal --
    journal = TradeJournal(config_dict)

    # -- Dashboard --
    dashboard = DashboardServer()

    # -- Persistent state --
    state_manager = StateManager()

    # -- Heartbeat --
    heartbeat = HeartbeatMonitor(
        config=config,
        logger=logger,
    )

    return {
        "exchange": exchange,
        "data_manager": data_manager,
        "data_feed": data_feed,
        "strategy": strategy,
        "risk_manager": risk_manager,
        "execution_engine": execution_engine,
        "alert_manager": alert_manager,
        "journal": journal,
        "dashboard": dashboard,
        "state_manager": state_manager,
        "heartbeat": heartbeat,
    }


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

async def async_main(args):
    """Async entry point: load config, build components, run orchestrator."""

    # -- Configuration (dict-based for universal module compatibility) --
    config = load_config_dict(args.config)
    mode = _resolve_mode(args, config)
    symbols = _resolve_symbols(args, config)

    # -- Logging --
    logger = setup_logger("bot", config=config)
    logger.info("=" * 60)
    logger.info("Crypto Trading Bot starting")
    logger.info("  Mode   : %s", mode.value)
    logger.info("  Symbols: %s", ", ".join(symbols))
    logger.info("  Config : %s", args.config or "config/settings.yaml")
    logger.info("  PID    : %d", os.getpid())
    logger.info("=" * 60)

    # -- Backtest short-circuit --
    if mode == BotMode.BACKTEST:
        logger.info("Running in BACKTEST mode - delegating to BacktestEngine")
        engine = BacktestEngine(config)
        results = await engine.run(symbols=symbols)
        logger.info("Backtest complete. Results summary:")
        for key, value in (results or {}).items():
            logger.info("  %s: %s", key, value)
        return 0

    # -- Build components --
    components = await build_components(config, mode, symbols, logger)

    # -- Orchestrator --
    orchestrator = BotOrchestrator(
        config=config_dict if hasattr(config, '_to_dict') else config,
        mode=mode,
        symbols=symbols,
        logger=logger,
        **components,
    )

    # -- Signal handling for graceful shutdown --
    shutdown_event = asyncio.Event()

    def _request_shutdown(sig_name):
        logger.info("Received %s - requesting graceful shutdown...", sig_name)
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _request_shutdown, sig.name)

    # -- Run --
    try:
        bot_task = asyncio.create_task(orchestrator.start(), name="orchestrator")

        # Wait for either the bot to finish or a shutdown signal
        shutdown_waiter = asyncio.create_task(shutdown_event.wait(), name="shutdown_waiter")
        done, _ = await asyncio.wait(
            {bot_task, shutdown_waiter},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if shutdown_waiter in done:
            logger.info("Shutdown signal received - stopping orchestrator...")
            await orchestrator.stop()
            # Give the bot task a moment to finish cleanly
            try:
                await asyncio.wait_for(bot_task, timeout=15.0)
            except asyncio.TimeoutError:
                logger.warning("Orchestrator did not stop within 15 s - cancelling task")
                bot_task.cancel()
                try:
                    await bot_task
                except asyncio.CancelledError:
                    pass
        else:
            # Bot task finished on its own (error or natural exit)
            exc = bot_task.exception() if not bot_task.cancelled() else None
            if exc:
                logger.error("Orchestrator exited with error: %s", exc, exc_info=exc)
                return 1

    except Exception:
        logger.exception("Fatal error in main loop")
        return 1
    finally:
        logger.info("Crypto Trading Bot shutdown complete.")

    return 0


def main():
    """Synchronous wrapper around the async entry point."""
    args = parse_args()
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
