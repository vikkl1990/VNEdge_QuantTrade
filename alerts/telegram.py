"""Telegram alert channel using the Telegram Bot API via aiohttp."""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import aiohttp

from config.constants import AlertLevel, AlertType, SignalType, confidence_to_grade

logger = logging.getLogger("bot.alerts.telegram")

# Telegram Bot API rate limit: ~30 messages per second to different chats,
# but only ~1 msg/s to the same chat.  We enforce a conservative interval.
_MIN_SEND_INTERVAL = 1.1  # seconds between messages to same chat
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 2.0
_TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}"


class TelegramAlerter:
    """Sends formatted HTML alerts to a Telegram chat via the Bot API."""

    def __init__(self, bot_token: str, chat_id: str, config: Optional[Dict[str, Any]] = None):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.config = config or {}
        self._base_url = _TELEGRAM_API_BASE.format(token=bot_token)
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_send_ts: float = 0.0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Low-level send with rate limiting and retries
    # ------------------------------------------------------------------

    async def send_to_user(self, user_chat_id: str, text: str, parse_mode: str = "HTML") -> bool:
        """Send a message to a SPECIFIC user's chat (overrides default chat_id)."""
        if not user_chat_id or not self.bot_token:
            return False
        try:
            session = await self._get_session()
            url = f"{self._base_url}/sendMessage"
            payload = {"chat_id": user_chat_id, "text": text, "parse_mode": parse_mode, "disable_web_page_preview": True}
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
                return r.status == 200
        except Exception as e:
            logger.debug("Telegram per-user send failed: %s", e)
            return False

    async def send_message(
        self,
        text: str,
        parse_mode: str = "HTML",
        disable_preview: bool = True,
    ) -> bool:
        """Send a message to the configured Telegram chat.

        Returns True on success, False on permanent failure.
        """
        async with self._lock:
            # Enforce per-chat rate limit
            now = asyncio.get_event_loop().time()
            wait = _MIN_SEND_INTERVAL - (now - self._last_send_ts)
            if wait > 0:
                await asyncio.sleep(wait)

            session = await self._get_session()
            url = f"{self._base_url}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": disable_preview,
            }

            for attempt in range(1, _MAX_RETRIES + 1):
                try:
                    async with session.post(url, json=payload) as resp:
                        self._last_send_ts = asyncio.get_event_loop().time()

                        if resp.status == 200:
                            return True

                        body = await resp.json()

                        # Handle Telegram rate-limit (429)
                        if resp.status == 429:
                            retry_after = body.get("parameters", {}).get(
                                "retry_after", _RETRY_BACKOFF_BASE ** attempt
                            )
                            logger.warning(
                                "Telegram rate limited, retry after %ss (attempt %d/%d)",
                                retry_after, attempt, _MAX_RETRIES,
                            )
                            await asyncio.sleep(retry_after)
                            continue

                        # Non-retryable client errors
                        if 400 <= resp.status < 500:
                            logger.error(
                                "Telegram API client error %d: %s",
                                resp.status, body.get("description", "unknown"),
                            )
                            return False

                        # Server errors -- retry
                        logger.warning(
                            "Telegram API error %d (attempt %d/%d): %s",
                            resp.status, attempt, _MAX_RETRIES,
                            body.get("description", ""),
                        )

                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    logger.warning(
                        "Telegram send failed (attempt %d/%d): %s",
                        attempt, _MAX_RETRIES, exc,
                    )

                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_BACKOFF_BASE ** attempt)

            logger.error("Telegram send exhausted all %d retries", _MAX_RETRIES)
            return False

    # ------------------------------------------------------------------
    # High-level formatters
    # ------------------------------------------------------------------

    async def send_signal_alert(self, signal: Dict[str, Any]) -> bool:
        """Format and send a trading signal alert."""
        text = self._format_signal(signal)
        return await self.send_message(text)

    async def send_trade_alert(
        self, trade: Dict[str, Any], alert_type: AlertType
    ) -> bool:
        """Format and send a trade lifecycle alert (entry, exit, TP, SL)."""
        text = self._format_trade(trade, alert_type)
        return await self.send_message(text)

    async def send_system_alert(self, message: str, level: AlertLevel) -> bool:
        """Format and send a system-level alert."""
        text = self._format_system(message, level)
        return await self.send_message(text)

    # ------------------------------------------------------------------
    # Formatters (private)
    # ------------------------------------------------------------------

    @staticmethod
    def _fmt_price(price: Optional[float]) -> str:
        if price is None:
            return "N/A"
        if price >= 1.0:
            return f"${price:,.2f}"
        return f"${price:.6f}"

    @staticmethod
    def _pct_change(entry: float, target: float) -> str:
        if entry == 0:
            return "0.00%"
        pct = ((target - entry) / entry) * 100
        sign = "+" if pct >= 0 else ""
        return f"{sign}{pct:.2f}%"

    def _format_signal(self, sig: Dict[str, Any]) -> str:
        signal_type = sig.get("signal_type", sig.get("type", "UNKNOWN"))
        if isinstance(signal_type, SignalType):
            signal_type = signal_type.value

        is_buy = signal_type.upper() in ("BUY", "PRE_BUY")
        icon = "\U0001f7e2" if is_buy else "\U0001f534"  # green / red circle
        direction = signal_type.upper().replace("_", " ")

        # Support both "price" and "entry_price" keys
        price = sig.get("price") or sig.get("entry_price", 0.0)
        sl = sig.get("stop_loss", sig.get("sl"))

        # Support both individual tp keys and take_profits list
        tps = sig.get("take_profits", [])
        tp1 = sig.get("tp1") or sig.get("take_profit_1") or (tps[0] if len(tps) > 0 else None)
        tp2 = sig.get("tp2") or sig.get("take_profit_2") or (tps[1] if len(tps) > 1 else None)
        tp3 = sig.get("tp3") or sig.get("take_profit_3") or (tps[2] if len(tps) > 2 else None)
        confidence = sig.get("confidence", 0)
        grade = sig.get("grade", confidence_to_grade(confidence).value)
        reason = sig.get("reason", "")
        symbol = sig.get("symbol", "???")
        timeframe = sig.get("timeframe", "")
        trade_id = sig.get("trade_id", sig.get("signal_id", ""))
        timestamp = sig.get("timestamp", datetime.now(timezone.utc).isoformat())

        # Risk:reward ratios
        rr_parts = []
        if sl and price:
            risk = abs(price - sl)
            if risk > 0:
                for label, tp in [("TP1", tp1), ("TP2", tp2), ("TP3", tp3)]:
                    if tp:
                        reward = abs(tp - price)
                        rr_parts.append(f"1:{reward / risk:.1f}")

        # Strategy type tag (scalp vs investment)
        meta = sig.get("metadata", {})
        strat_type = meta.get("strategy_type", "")
        setup_type = meta.get("setup_type", "")
        strat_tag = ""
        if strat_type == "scalp":
            strat_tag = " \u26a1 SCALP"
        elif strat_type == "investment":
            strat_tag = " \U0001f4ca INVEST"

        lines = [
            f"{icon} <b>{direction} CONFIRMED</b>{strat_tag}",
            f"<b>Symbol:</b> {symbol}",
        ]
        if setup_type:
            lines.append(f"<b>Setup:</b> {setup_type}")
        if timeframe:
            lines.append(f"<b>Timeframe:</b> {timeframe}")
        lines.append(f"<b>Entry:</b> {self._fmt_price(price)}")

        if sl is not None:
            lines.append(
                f"<b>SL:</b> {self._fmt_price(sl)} ({self._pct_change(price, sl)})"
            )
        if tp1 is not None:
            lines.append(
                f"<b>TP1:</b> {self._fmt_price(tp1)} ({self._pct_change(price, tp1)})"
            )
        if tp2 is not None:
            lines.append(
                f"<b>TP2:</b> {self._fmt_price(tp2)} ({self._pct_change(price, tp2)})"
            )
        if tp3 is not None:
            lines.append(
                f"<b>TP3:</b> {self._fmt_price(tp3)} ({self._pct_change(price, tp3)})"
            )

        lines.append(f"<b>Confidence:</b> {confidence}/100 ({grade})")

        if reason:
            lines.append(f"<b>Reason:</b> {reason}")
        if rr_parts:
            lines.append(f"<b>R:R:</b> {' / '.join(rr_parts)}")
        if trade_id:
            lines.append(f"<b>ID:</b> <code>{trade_id}</code>")

        lines.append(f"<b>Time:</b> {timestamp}")

        return "\n".join(lines)

    def _format_trade(self, trade: Dict[str, Any], alert_type: AlertType) -> str:
        symbol = trade.get("symbol", "???")
        side = trade.get("side", "").upper()
        trade_id = trade.get("trade_id", "")

        icon_map = {
            AlertType.ENTRY: "\U0001f4b0",       # money bag
            AlertType.EXIT: "\U0001f6aa",         # door
            AlertType.TP_HIT: "\U0001f3af",       # bullseye
            AlertType.SL_HIT: "\U0001f6d1",       # stop sign
            AlertType.TRAILING_STOP: "\U0001f6d1",
            AlertType.BREAK_EVEN: "\u2696\ufe0f", # balance scale
            AlertType.PARTIAL_CLOSE: "\u2702\ufe0f",  # scissors
        }
        icon = icon_map.get(alert_type, "\U0001f514")  # bell default

        lines = [
            f"{icon} <b>{alert_type.value.replace('_', ' ')}</b>",
            f"<b>Symbol:</b> {symbol}",
            f"<b>Side:</b> {side}",
        ]

        entry_price = trade.get("entry_price")
        exit_price = trade.get("exit_price")
        quantity = trade.get("quantity", trade.get("size"))

        if entry_price is not None:
            lines.append(f"<b>Entry:</b> {self._fmt_price(entry_price)}")
        if exit_price is not None:
            lines.append(f"<b>Exit:</b> {self._fmt_price(exit_price)}")
        if quantity is not None:
            lines.append(f"<b>Size:</b> {quantity}")

        pnl = trade.get("pnl", trade.get("realized_pnl"))
        pnl_pct = trade.get("pnl_pct", trade.get("return_pct"))
        if pnl is not None:
            pnl_icon = "\U0001f4c8" if pnl >= 0 else "\U0001f4c9"  # chart up/down
            lines.append(f"<b>PnL:</b> {pnl_icon} ${pnl:+,.2f}")
        if pnl_pct is not None:
            lines.append(f"<b>Return:</b> {pnl_pct:+.2f}%")

        duration = trade.get("duration")
        if duration:
            lines.append(f"<b>Duration:</b> {duration}")

        if trade_id:
            lines.append(f"<b>ID:</b> <code>{trade_id}</code>")

        return "\n".join(lines)

    @staticmethod
    def _format_system(message: str, level: AlertLevel) -> str:
        icon_map = {
            AlertLevel.INFO: "\u2139\ufe0f",       # info
            AlertLevel.WARNING: "\u26a0\ufe0f",     # warning
            AlertLevel.ERROR: "\u274c",             # red X
            AlertLevel.CRITICAL: "\U0001f6a8",      # rotating light
        }
        icon = icon_map.get(level, "\U0001f514")
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        return (
            f"{icon} <b>SYSTEM {level.value}</b>\n"
            f"{message}\n"
            f"<i>{ts}</i>"
        )
