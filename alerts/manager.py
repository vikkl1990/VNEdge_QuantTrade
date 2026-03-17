"""Central alert manager that routes alerts to all enabled channels."""

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional, Protocol

from config import get_config
from config.constants import AlertLevel, AlertType

logger = logging.getLogger("bot.alerts.manager")


class AlertChannel(Protocol):
    """Minimal interface every alert channel must satisfy."""

    async def send_signal_alert(self, signal: Dict[str, Any]) -> bool: ...
    async def send_trade_alert(self, trade: Dict[str, Any], alert_type: AlertType) -> bool: ...
    async def send_system_alert(self, message: str, level: AlertLevel) -> bool: ...
    async def close(self) -> None: ...


class AlertManager:
    """Routes alerts to all registered and enabled alert channels.

    Channels are lazily initialised from configuration on first use.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or get_config()
        self._channels: List[AlertChannel] = []
        self._initialised = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialise(self) -> None:
        """Create and register alert channels based on configuration."""
        if self._initialised:
            return

        alerts_cfg = self.config.get("alerts", {})

        # -- Telegram --
        tg_cfg = alerts_cfg.get("telegram", {})
        if tg_cfg.get("enabled", False):
            bot_token = (
                self.config.get("telegram", {}).get("bot_token")
                or os.getenv("TELEGRAM_BOT_TOKEN", "")
            )
            chat_id = (
                self.config.get("telegram", {}).get("chat_id")
                or os.getenv("TELEGRAM_CHAT_ID", "")
            )
            if bot_token and chat_id:
                from alerts.telegram import TelegramAlerter
                channel = TelegramAlerter(bot_token, chat_id, tg_cfg)
                self._channels.append(channel)
                logger.info("Telegram alert channel enabled")
            else:
                logger.warning(
                    "Telegram alerting enabled in config but bot_token/chat_id missing"
                )

        # -- Console --
        console_cfg = alerts_cfg.get("console", {})
        if console_cfg.get("enabled", True):
            from alerts.console import ConsoleAlerter
            channel = ConsoleAlerter(console_cfg)
            self._channels.append(channel)
            logger.info("Console alert channel enabled")

        self._initialised = True
        logger.info("AlertManager initialised with %d channel(s)", len(self._channels))

    async def shutdown(self) -> None:
        """Gracefully close all alert channels."""
        for ch in self._channels:
            try:
                await ch.close()
            except Exception:
                logger.exception("Error closing alert channel %s", type(ch).__name__)
        self._channels.clear()
        self._initialised = False
        logger.info("AlertManager shut down")

    # ------------------------------------------------------------------
    # Ensure init before use
    # ------------------------------------------------------------------

    async def _ensure_init(self) -> None:
        if not self._initialised:
            await self.initialise()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def send_signal_alert(self, signal: Dict[str, Any]) -> None:
        """Send a signal alert to all enabled channels.

        Failures on individual channels are logged but do not propagate.
        """
        await self._ensure_init()
        if not self._channels:
            return

        results = await asyncio.gather(
            *(ch.send_signal_alert(signal) for ch in self._channels),
            return_exceptions=True,
        )
        for ch, result in zip(self._channels, results):
            if isinstance(result, Exception):
                logger.error(
                    "Signal alert failed on %s: %s", type(ch).__name__, result
                )
            elif not result:
                logger.warning(
                    "Signal alert returned failure on %s", type(ch).__name__
                )

    async def send_trade_alert(
        self, trade: Dict[str, Any], alert_type: AlertType
    ) -> None:
        """Send a trade alert to all enabled channels."""
        await self._ensure_init()
        if not self._channels:
            return

        results = await asyncio.gather(
            *(ch.send_trade_alert(trade, alert_type) for ch in self._channels),
            return_exceptions=True,
        )
        for ch, result in zip(self._channels, results):
            if isinstance(result, Exception):
                logger.error(
                    "Trade alert failed on %s: %s", type(ch).__name__, result
                )
            elif not result:
                logger.warning(
                    "Trade alert returned failure on %s", type(ch).__name__
                )

    async def send_system_alert(
        self, message: str, level: AlertLevel = AlertLevel.INFO
    ) -> None:
        """Send a system-level alert to all enabled channels."""
        await self._ensure_init()
        if not self._channels:
            return

        results = await asyncio.gather(
            *(ch.send_system_alert(message, level) for ch in self._channels),
            return_exceptions=True,
        )
        for ch, result in zip(self._channels, results):
            if isinstance(result, Exception):
                logger.error(
                    "System alert failed on %s: %s", type(ch).__name__, result
                )
            elif not result:
                logger.warning(
                    "System alert returned failure on %s", type(ch).__name__
                )

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    async def info(self, message: str) -> None:
        await self.send_system_alert(message, AlertLevel.INFO)

    async def warning(self, message: str) -> None:
        await self.send_system_alert(message, AlertLevel.WARNING)

    async def error(self, message: str) -> None:
        await self.send_system_alert(message, AlertLevel.ERROR)

    async def critical(self, message: str) -> None:
        await self.send_system_alert(message, AlertLevel.CRITICAL)
