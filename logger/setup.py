"""Logging configuration with rotating file handlers and optional JSON formatting.

Usage:
    from logger import setup_logger
    logger = setup_logger("bot")          # uses default config
    logger = setup_logger("trades", config)  # explicit config dict
"""

import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from config import get_config

# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------

class JSONFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        log_obj: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1] is not None:
            log_obj["exception"] = self.formatException(record.exc_info)
        # Attach any extra structured fields
        for key in ("trade_id", "symbol", "signal_type", "pnl"):
            val = getattr(record, key, None)
            if val is not None:
                log_obj[key] = val
        return json.dumps(log_obj, default=str)


# ---------------------------------------------------------------------------
# Standard formatter
# ---------------------------------------------------------------------------

_CONSOLE_FMT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_CONSOLE_DATE_FMT = "%H:%M:%S"

_FILE_FMT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_FILE_DATE_FMT = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------------------
# Log file routing map: logger name prefix -> filename
# ---------------------------------------------------------------------------

_LOG_FILE_MAP = {
    "bot.trades": "trades.log",
    "bot.signals": "signals.log",
    "bot.errors": "errors.log",
    "bot": "bot.log",
}


def _resolve_log_file(logger_name: str) -> str:
    """Return the log filename to use for the given logger name."""
    for prefix, filename in _LOG_FILE_MAP.items():
        if logger_name.startswith(prefix):
            return filename
    return "bot.log"


# ---------------------------------------------------------------------------
# Cache of already-configured loggers to avoid duplicate handlers
# ---------------------------------------------------------------------------

_configured: Dict[str, logging.Logger] = {}


def setup_logger(
    name: str,
    config: Optional[Dict[str, Any]] = None,
) -> logging.Logger:
    """Configure and return a named logger.

    Parameters
    ----------
    name:
        Logger name.  Convention: ``"bot"``, ``"bot.trades"``,
        ``"bot.signals"``, etc.  The logger hierarchy follows standard
        Python logging rules.
    config:
        Full bot configuration dict.  If ``None`` the global config is
        loaded via :func:`config.get_config`.

    Returns
    -------
    logging.Logger
        A fully configured logger with console and (optionally) file
        handlers attached.  Calling ``setup_logger`` multiple times with
        the same *name* returns the cached instance.
    """
    if name in _configured:
        return _configured[name]

    cfg = (config or get_config()).get("logging", {})

    level_str = cfg.get("level", "INFO").upper()
    level = getattr(logging, level_str, logging.INFO)

    log_dir = cfg.get("log_dir", os.getenv("LOG_DIR", "logs"))
    file_enabled = cfg.get("file_enabled", True)
    console_enabled = cfg.get("console_enabled", True)
    max_bytes = cfg.get("max_file_size_mb", 50) * 1024 * 1024
    backup_count = cfg.get("backup_count", 10)
    use_json = cfg.get("json_format", False)

    log = logging.getLogger(name)
    log.setLevel(level)
    log.propagate = False  # prevent duplicate output on root logger

    # ---- Console handler ----
    if console_enabled and not _has_handler(log, logging.StreamHandler):
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        if use_json:
            console_handler.setFormatter(JSONFormatter())
        else:
            console_handler.setFormatter(
                logging.Formatter(_CONSOLE_FMT, datefmt=_CONSOLE_DATE_FMT)
            )
        log.addHandler(console_handler)

    # ---- File handler(s) ----
    if file_enabled:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)

        # Main file for this logger
        main_file = log_path / _resolve_log_file(name)
        if not _has_rotating_handler(log, str(main_file)):
            fh = logging.handlers.RotatingFileHandler(
                str(main_file),
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            fh.setLevel(level)
            if use_json:
                fh.setFormatter(JSONFormatter())
            else:
                fh.setFormatter(
                    logging.Formatter(_FILE_FMT, datefmt=_FILE_DATE_FMT)
                )
            log.addHandler(fh)

        # Always mirror ERROR+ to errors.log (unless this *is* the error logger)
        errors_file = log_path / "errors.log"
        if name != "bot.errors" and not _has_rotating_handler(log, str(errors_file)):
            eh = logging.handlers.RotatingFileHandler(
                str(errors_file),
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            eh.setLevel(logging.ERROR)
            if use_json:
                eh.setFormatter(JSONFormatter())
            else:
                eh.setFormatter(
                    logging.Formatter(_FILE_FMT, datefmt=_FILE_DATE_FMT)
                )
            log.addHandler(eh)

    _configured[name] = log
    return log


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _has_handler(log: logging.Logger, handler_type: type) -> bool:
    return any(isinstance(h, handler_type) for h in log.handlers)


def _has_rotating_handler(log: logging.Logger, filename: str) -> bool:
    for h in log.handlers:
        if isinstance(h, logging.handlers.RotatingFileHandler):
            if os.path.abspath(h.baseFilename) == os.path.abspath(filename):
                return True
    return False
