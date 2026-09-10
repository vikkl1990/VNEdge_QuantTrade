"""Console alert channel with colored output using ANSI escape codes."""

import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from config.constants import AlertLevel, AlertType, SignalType, confidence_to_grade

logger = logging.getLogger("bot.alerts.console")


# ---------------------------------------------------------------------------
# ANSI color helpers (no external dependency needed)
# ---------------------------------------------------------------------------

class _Color:
    """ANSI 256-color escape sequences."""
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    GREEN = "\033[92m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    MAGENTA = "\033[95m"
    YELLOW = "\033[93m"
    WHITE = "\033[97m"
    BLUE = "\033[94m"
    GRAY = "\033[90m"

    BG_GREEN = "\033[42m"
    BG_RED = "\033[41m"
    BG_CYAN = "\033[46m"
    BG_MAGENTA = "\033[45m"
    BG_YELLOW = "\033[43m"


def _supports_color() -> bool:
    """Check whether the terminal supports ANSI colors."""
    if not hasattr(sys.stdout, "isatty"):
        return False
    return sys.stdout.isatty()


class ConsoleAlerter:
    """Prints formatted, colored alerts to the terminal."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.colored = self.config.get("colored", True) and _supports_color()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _c(self, code: str, text: str) -> str:
        """Wrap *text* in an ANSI color code if colors are enabled."""
        if not self.colored:
            return text
        return f"{code}{text}{_Color.RESET}"

    def _separator(self, char: str = "-", width: int = 50) -> str:
        return self._c(_Color.DIM, char * width)

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

    def _print(self, text: str) -> None:
        print(text, flush=True)

    # ------------------------------------------------------------------
    # Public API (matches TelegramAlerter interface)
    # ------------------------------------------------------------------

    async def send_signal_alert(self, signal: Dict[str, Any]) -> bool:
        """Print a signal alert to the console."""
        try:
            self._print(self._format_signal(signal))
            return True
        except Exception:
            logger.exception("Failed to print signal alert to console")
            return False

    async def send_trade_alert(
        self, trade: Dict[str, Any], alert_type: AlertType
    ) -> bool:
        """Print a trade alert to the console."""
        try:
            self._print(self._format_trade(trade, alert_type))
            return True
        except Exception:
            logger.exception("Failed to print trade alert to console")
            return False

    async def send_system_alert(self, message: str, level: AlertLevel) -> bool:
        """Print a system alert to the console."""
        try:
            self._print(self._format_system(message, level))
            return True
        except Exception:
            logger.exception("Failed to print system alert to console")
            return False

    async def close(self) -> None:
        """No-op for console; present for interface consistency."""

    # ------------------------------------------------------------------
    # Formatters
    # ------------------------------------------------------------------

    def _format_signal(self, sig: Dict[str, Any]) -> str:
        signal_type = sig.get("signal_type", sig.get("type", "UNKNOWN"))
        if isinstance(signal_type, SignalType):
            signal_type = signal_type.value

        is_buy = signal_type.upper() in ("BUY", "PRE_BUY")
        color = _Color.GREEN if is_buy else _Color.RED
        direction = signal_type.upper().replace("_", " ")

        # Signal.to_dict() emits "entry_price"; "price" is the legacy key.
        price = sig.get("price") or sig.get("entry_price") or 0.0
        sl = sig.get("stop_loss", sig.get("sl"))
        tp1 = sig.get("tp1", sig.get("take_profit_1"))
        tp2 = sig.get("tp2", sig.get("take_profit_2"))
        tp3 = sig.get("tp3", sig.get("take_profit_3"))
        confidence = sig.get("confidence", 0)
        grade = sig.get("grade", confidence_to_grade(confidence).value)
        reason = sig.get("reason", "")
        symbol = sig.get("symbol", "???")
        timeframe = sig.get("timeframe", "")
        trade_id = sig.get("trade_id", sig.get("signal_id", ""))
        timestamp = sig.get("timestamp", datetime.now(timezone.utc).isoformat())

        # R:R
        rr_parts = []
        if sl and price:
            risk = abs(price - sl)
            if risk > 0:
                for label, tp in [("TP1", tp1), ("TP2", tp2), ("TP3", tp3)]:
                    if tp:
                        reward = abs(tp - price)
                        rr_parts.append(f"1:{reward / risk:.1f}")

        lines = [
            self._separator("="),
            self._c(color + _Color.BOLD, f"  {direction} CONFIRMED  "),
            self._separator("="),
            f"  {self._c(_Color.WHITE, 'Symbol:')}    {self._c(_Color.BOLD, symbol)}",
        ]
        if timeframe:
            lines.append(f"  {self._c(_Color.WHITE, 'Timeframe:')} {timeframe}")
        lines.append(f"  {self._c(_Color.WHITE, 'Price:')}     {self._fmt_price(price)}")

        if sl is not None:
            lines.append(
                f"  {self._c(_Color.MAGENTA, 'SL:')}        "
                f"{self._fmt_price(sl)} ({self._pct_change(price, sl)})"
            )
        if tp1 is not None:
            lines.append(
                f"  {self._c(_Color.CYAN, 'TP1:')}       "
                f"{self._fmt_price(tp1)} ({self._pct_change(price, tp1)})"
            )
        if tp2 is not None:
            lines.append(
                f"  {self._c(_Color.CYAN, 'TP2:')}       "
                f"{self._fmt_price(tp2)} ({self._pct_change(price, tp2)})"
            )
        if tp3 is not None:
            lines.append(
                f"  {self._c(_Color.CYAN, 'TP3:')}       "
                f"{self._fmt_price(tp3)} ({self._pct_change(price, tp3)})"
            )

        conf_color = _Color.GREEN if confidence >= 80 else (
            _Color.YELLOW if confidence >= 60 else _Color.RED
        )
        lines.append(
            f"  {self._c(_Color.WHITE, 'Confidence:')} "
            f"{self._c(conf_color, f'{confidence}/100 ({grade})')}"
        )

        if reason:
            lines.append(f"  {self._c(_Color.WHITE, 'Reason:')}    {reason}")
        if rr_parts:
            lines.append(
                f"  {self._c(_Color.WHITE, 'R:R:')}       {' / '.join(rr_parts)}"
            )
        if trade_id:
            lines.append(
                f"  {self._c(_Color.GRAY, 'ID:')}        {trade_id}"
            )
        lines.append(f"  {self._c(_Color.GRAY, 'Time:')}      {timestamp}")
        lines.append(self._separator("="))

        return "\n".join(lines)

    def _format_trade(self, trade: Dict[str, Any], alert_type: AlertType) -> str:
        symbol = trade.get("symbol", "???")
        side = trade.get("side", "").upper()
        trade_id = trade.get("trade_id", "")

        color_map = {
            AlertType.ENTRY: _Color.GREEN,
            AlertType.EXIT: _Color.BLUE,
            AlertType.TP_HIT: _Color.CYAN,
            AlertType.SL_HIT: _Color.MAGENTA,
            AlertType.TRAILING_STOP: _Color.MAGENTA,
            AlertType.BREAK_EVEN: _Color.YELLOW,
            AlertType.PARTIAL_CLOSE: _Color.CYAN,
        }
        color = color_map.get(alert_type, _Color.WHITE)

        title = alert_type.value.replace("_", " ")
        lines = [
            self._separator("-"),
            self._c(color + _Color.BOLD, f"  {title}"),
            f"  {self._c(_Color.WHITE, 'Symbol:')} {symbol}  |  {self._c(_Color.WHITE, 'Side:')} {side}",
        ]

        entry_price = trade.get("entry_price")
        exit_price = trade.get("exit_price")
        quantity = trade.get("quantity", trade.get("size"))

        if entry_price is not None:
            lines.append(f"  {self._c(_Color.WHITE, 'Entry:')}  {self._fmt_price(entry_price)}")
        if exit_price is not None:
            lines.append(f"  {self._c(_Color.WHITE, 'Exit:')}   {self._fmt_price(exit_price)}")
        if quantity is not None:
            lines.append(f"  {self._c(_Color.WHITE, 'Size:')}   {quantity}")

        pnl = trade.get("pnl", trade.get("realized_pnl"))
        pnl_pct = trade.get("pnl_pct", trade.get("return_pct"))
        if pnl is not None:
            pnl_color = _Color.GREEN if pnl >= 0 else _Color.RED
            lines.append(
                f"  {self._c(_Color.WHITE, 'PnL:')}    {self._c(pnl_color, f'${pnl:+,.2f}')}"
            )
        if pnl_pct is not None:
            pnl_color = _Color.GREEN if pnl_pct >= 0 else _Color.RED
            lines.append(
                f"  {self._c(_Color.WHITE, 'Return:')} {self._c(pnl_color, f'{pnl_pct:+.2f}%')}"
            )

        duration = trade.get("duration")
        if duration:
            lines.append(f"  {self._c(_Color.GRAY, 'Duration:')} {duration}")
        if trade_id:
            lines.append(f"  {self._c(_Color.GRAY, 'ID:')}       {trade_id}")

        lines.append(self._separator("-"))
        return "\n".join(lines)

    def _format_system(self, message: str, level: AlertLevel) -> str:
        color_map = {
            AlertLevel.INFO: _Color.BLUE,
            AlertLevel.WARNING: _Color.YELLOW,
            AlertLevel.ERROR: _Color.RED,
            AlertLevel.CRITICAL: _Color.RED + _Color.BOLD,
        }
        color = color_map.get(level, _Color.WHITE)
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")

        return (
            f"{self._c(color, f'[{level.value}]')} "
            f"{self._c(_Color.GRAY, ts)} "
            f"{message}"
        )
