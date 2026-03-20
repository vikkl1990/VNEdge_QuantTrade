#!/usr/bin/env python3
"""
ML Training Runner — VM2 Entry Point
=====================================
Launches the training pipeline + dashboard on VM2.

Dashboard runs in a SEPARATE PROCESS so CPU-bound training
doesn't block dashboard HTTP responses.

Usage:
    python ml_training/run_trainer.py                          # Dashboard only (port 8081)
    python ml_training/run_trainer.py --train                  # Train + Dashboard
    python ml_training/run_trainer.py --train --symbols BTC/USDT,ETH/USDT
    python ml_training/run_trainer.py --train --timeframes 5m,15m
"""

import argparse
import asyncio
import logging
import multiprocessing
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(PROJECT_ROOT / "ml_training.log"),
    ],
)
logger = logging.getLogger("ml_trainer")


def parse_args():
    parser = argparse.ArgumentParser(description="VN Edge ML Training System")
    parser.add_argument("--train", action="store_true", help="Start training immediately")
    parser.add_argument("--symbols", type=str, default=None,
                        help="Comma-separated symbols (default: BTC/USDT,ETH/USDT,AVAX/USDT)")
    parser.add_argument("--timeframes", type=str, default=None,
                        help="Comma-separated timeframes (default: 1m,5m,15m)")
    parser.add_argument("--port", type=int, default=8081, help="Dashboard port")
    return parser.parse_args()


def _run_dashboard(port: int):
    """Run dashboard in a separate process — never blocked by training."""
    import asyncio as _asyncio
    from ml_training.dashboard import MLDashboard

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    _logger = logging.getLogger("ml_dashboard")

    async def _serve():
        dashboard = MLDashboard(trainer=None, port=port)
        await dashboard.start()
        _logger.info("Dashboard process running on http://0.0.0.0:%d", port)
        while True:
            await _asyncio.sleep(3600)

    _asyncio.run(_serve())


async def main():
    args = parse_args()

    # Start dashboard in a separate process so it's never blocked
    dash_proc = multiprocessing.Process(
        target=_run_dashboard, args=(args.port,), daemon=True
    )
    dash_proc.start()
    logger.info("Dashboard started in separate process (PID %d) on port %d",
                dash_proc.pid, args.port)

    if args.train:
        from config.loader import load_config as load_config_dict
        from exchange.factory import create_exchange_client
        from ml_training.trainer import TrainingOrchestrator

        config = load_config_dict()
        exchange = create_exchange_client()
        try:
            await exchange.connect()
            logger.info("Exchange connected: %s", type(exchange).__name__)
        except Exception as e:
            logger.error("Failed to connect exchange: %s", e)
            return

        symbols = [s.strip() for s in args.symbols.split(",")] if args.symbols else None
        timeframes = [t.strip() for t in args.timeframes.split(",")] if args.timeframes else None

        trainer = TrainingOrchestrator(exchange, config)

        logger.info("Starting training pipeline...")
        logger.info("  Symbols: %s", symbols or "default (all 11 symbols)")
        logger.info("  Timeframes: %s", timeframes or "default (1m, 5m, 15m)")

        try:
            await trainer.run_full_pipeline(symbols, timeframes)
            logger.info("=== TRAINING PIPELINE COMPLETE ===")
        except Exception as e:
            logger.exception("Training pipeline failed: %s", e)

    # Keep running (dashboard process stays alive)
    logger.info("ML Training system ready. Dashboard at http://0.0.0.0:%d", args.port)
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Shutting down ML trainer...")
        dash_proc.terminate()


if __name__ == "__main__":
    asyncio.run(main())
