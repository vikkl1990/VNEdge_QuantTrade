"""
General-purpose utility functions for the crypto trading bot.

Provides timestamp formatting, price/quantity rounding, percentage
calculations, and retry decorators for both sync and async callables.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from typing import (
    Any,
    Callable,
    Optional,
    Sequence,
    Tuple,
    Type,
    TypeVar,
    Union,
)

from config.constants import (
    DEFAULT_PRICE_PRECISION,
    DEFAULT_QTY_PRECISION,
    DEFAULT_RETRY_ATTEMPTS,
    DEFAULT_RETRY_BACKOFF,
    DEFAULT_RETRY_DELAY,
    FILE_TS_FORMAT,
    ISO_FORMAT,
    LOG_TS_FORMAT,
)

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


# ---------------------------------------------------------------------------
# Timezone: IST (Indian Standard Time, UTC+5:30)
# ---------------------------------------------------------------------------

IST = timezone(timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def utc_now() -> datetime:
    """Return the current UTC time as an aware datetime."""
    return datetime.now(timezone.utc)


def ist_now() -> datetime:
    """Return the current IST time as an aware datetime."""
    return datetime.now(IST)


def to_ist(dt: datetime) -> datetime:
    """Convert any aware datetime to IST."""
    return dt.astimezone(IST)


def ts_to_iso(ts_ms: Union[int, float]) -> str:
    """Convert a millisecond Unix timestamp to an ISO-8601 string (IST)."""
    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=IST)
    return dt.strftime(ISO_FORMAT)


def iso_to_ts(iso_str: str) -> int:
    """Convert an ISO-8601 string to a millisecond Unix timestamp."""
    dt = datetime.strptime(iso_str, ISO_FORMAT).replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def format_log_ts(dt: Optional[datetime] = None) -> str:
    """Format a datetime for log output.  Defaults to *now* (IST)."""
    dt = dt or ist_now()
    return dt.strftime(LOG_TS_FORMAT)


def format_file_ts(dt: Optional[datetime] = None) -> str:
    """Format a datetime for filenames.  Defaults to *now* (IST)."""
    dt = dt or ist_now()
    return dt.strftime(FILE_TS_FORMAT)


def ms_since(start_ms: Union[int, float]) -> float:
    """Return elapsed milliseconds since *start_ms*."""
    return time.time() * 1000 - start_ms


# ---------------------------------------------------------------------------
# Price / quantity rounding
# ---------------------------------------------------------------------------

def round_price(
    price: float,
    precision: int = DEFAULT_PRICE_PRECISION,
) -> float:
    """Round *price* down to *precision* decimal places (truncate)."""
    if precision < 0:
        raise ValueError(f"precision must be >= 0, got {precision}")
    factor = 10 ** precision
    return math.floor(price * factor) / factor


def round_qty(
    qty: float,
    precision: int = DEFAULT_QTY_PRECISION,
) -> float:
    """Round *qty* down to *precision* decimal places (truncate)."""
    if precision < 0:
        raise ValueError(f"precision must be >= 0, got {precision}")
    d = Decimal(str(qty))
    quantize_str = Decimal(10) ** -precision
    return float(d.quantize(quantize_str, rounding=ROUND_DOWN))


def round_to_tick(value: float, tick_size: float) -> float:
    """Round *value* down to the nearest *tick_size* increment."""
    if tick_size <= 0:
        raise ValueError(f"tick_size must be > 0, got {tick_size}")
    return math.floor(value / tick_size) * tick_size


# ---------------------------------------------------------------------------
# Percentage / math helpers
# ---------------------------------------------------------------------------

def pct_change(old: float, new: float) -> float:
    """Return the percentage change from *old* to *new*."""
    if old == 0:
        return 0.0
    return ((new - old) / abs(old)) * 100.0


def pct_of(value: float, pct: float) -> float:
    """Return *pct* percent of *value*."""
    return value * (pct / 100.0)


def clamp(value: float, lo: float, hi: float) -> float:
    """Clamp *value* to the range [*lo*, *hi*]."""
    return max(lo, min(hi, value))


def safe_divide(numerator: float, denominator: float, default: float = 0.0) -> float:
    """Divide without raising on zero denominator."""
    if denominator == 0:
        return default
    return numerator / denominator


def risk_reward_ratio(
    entry: float,
    stop_loss: float,
    take_profit: float,
) -> float:
    """Calculate reward-to-risk ratio for a trade setup."""
    risk = abs(entry - stop_loss)
    if risk == 0:
        return 0.0
    reward = abs(take_profit - entry)
    return reward / risk


# ---------------------------------------------------------------------------
# Retry decorators
# ---------------------------------------------------------------------------

def retry(
    max_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    delay: float = DEFAULT_RETRY_DELAY,
    backoff: float = DEFAULT_RETRY_BACKOFF,
    exceptions: Tuple[Type[BaseException], ...] = (Exception,),
    on_retry: Optional[Callable[[int, BaseException], None]] = None,
) -> Callable[[F], F]:
    """
    Synchronous retry decorator with exponential backoff.

    Parameters
    ----------
    max_attempts:
        Total number of attempts (including the initial call).
    delay:
        Initial delay in seconds between retries.
    backoff:
        Multiplier applied to *delay* after each retry.
    exceptions:
        Tuple of exception types that trigger a retry.
    on_retry:
        Optional callback ``(attempt, exception)`` invoked before sleeping.
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            current_delay = delay
            last_exc: BaseException = RuntimeError("unreachable")
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt == max_attempts:
                        logger.error(
                            "%s failed after %d attempts: %s",
                            func.__qualname__, max_attempts, exc,
                        )
                        raise
                    if on_retry:
                        on_retry(attempt, exc)
                    logger.warning(
                        "%s attempt %d/%d failed (%s), retrying in %.1fs",
                        func.__qualname__, attempt, max_attempts, exc, current_delay,
                    )
                    time.sleep(current_delay)
                    current_delay *= backoff
            raise last_exc  # pragma: no cover

        return wrapper  # type: ignore[return-value]

    return decorator


def async_retry(
    max_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    delay: float = DEFAULT_RETRY_DELAY,
    backoff: float = DEFAULT_RETRY_BACKOFF,
    exceptions: Tuple[Type[BaseException], ...] = (Exception,),
    on_retry: Optional[Callable[[int, BaseException], None]] = None,
) -> Callable[[F], F]:
    """
    Asynchronous retry decorator with exponential backoff.

    Same parameters as :func:`retry` but wraps an ``async def`` function.
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            current_delay = delay
            last_exc: BaseException = RuntimeError("unreachable")
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt == max_attempts:
                        logger.error(
                            "%s failed after %d attempts: %s",
                            func.__qualname__, max_attempts, exc,
                        )
                        raise
                    if on_retry:
                        on_retry(attempt, exc)
                    logger.warning(
                        "%s attempt %d/%d failed (%s), retrying in %.1fs",
                        func.__qualname__, attempt, max_attempts, exc, current_delay,
                    )
                    await asyncio.sleep(current_delay)
                    current_delay *= backoff
            raise last_exc  # pragma: no cover

        return wrapper  # type: ignore[return-value]

    return decorator
