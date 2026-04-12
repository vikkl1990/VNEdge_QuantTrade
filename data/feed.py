"""
DataFeed: asynchronous market data ingestion via websocket and REST fallback.

Connects to a cryptocurrency exchange using ccxt (async) for websocket
streaming of OHLCV candles and ticker prices.  Falls back to periodic
REST polling when the websocket connection is unavailable or unhealthy.

Usage
-----
::

    feed = DataFeed(data_manager)
    await feed.start()
    ...
    await feed.stop()

Events
------
The feed exposes an event system so that strategies can react to new data:

- ``candle_closed``  : emitted when a complete candle is finalised
- ``candle_update``  : emitted on every intra-candle tick
- ``price_update``   : emitted on ticker / trade price changes
- ``feed_error``     : emitted on connection / data errors
- ``feed_stale``     : emitted when no data received for too long
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set

import ccxt.async_support as ccxt

from config import get_config
from data.manager import DataManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

EventCallback = Callable[..., Coroutine[Any, Any, None]]


class FeedState(Enum):
    IDLE = auto()
    CONNECTING = auto()
    STREAMING = auto()
    POLLING = auto()
    RECONNECTING = auto()
    STOPPED = auto()


@dataclass
class _Subscription:
    """Tracks per-symbol subscription metadata."""
    symbol: str
    timeframes: List[str]
    last_candle_ts: Dict[str, float] = field(default_factory=dict)
    last_data_time: float = 0.0


# ---------------------------------------------------------------------------
# DataFeed
# ---------------------------------------------------------------------------

class DataFeed:
    """
    Async market data feed with websocket-first, REST-fallback architecture.

    Parameters
    ----------
    data_manager : DataManager
        Shared data store where incoming candles are written.
    stale_threshold_s : float
        Seconds without data before the feed is considered stale.
    poll_interval_s : float
        REST polling interval when websocket is unavailable.
    """

    def __init__(
        self,
        data_manager: DataManager,
        stale_threshold_s: float = 60.0,
        poll_interval_s: float = 10.0,
    ):
        self._dm = data_manager
        self._cfg = get_config()

        # Exchange config
        self._exchange_name: str = self._cfg["exchange"]["name"]
        self._use_ws: bool = self._cfg["exchange"].get("websocket", True)
        self._rest_fallback: bool = self._cfg["exchange"].get("rest_fallback", True)
        self._reconnect_attempts: int = self._cfg["exchange"].get("reconnect_attempts", 10)
        self._reconnect_delay: int = self._cfg["exchange"].get("reconnect_delay", 5)

        # Symbols & timeframes
        self._symbols: List[str] = self._cfg.get("symbols", [])
        tf_cfg = self._cfg.get("timeframes", {})
        self._timeframes: List[str] = list(
            dict.fromkeys(v for v in tf_cfg.values() if isinstance(v, str))
        )

        # Thresholds
        self._stale_threshold = stale_threshold_s
        self._poll_interval = poll_interval_s

        # State
        self._state = FeedState.IDLE
        self._exchange: Optional[ccxt.Exchange] = None
        self._subscriptions: Dict[str, _Subscription] = {}
        self._tasks: List[asyncio.Task] = []
        self._stop_event = asyncio.Event()
        self._consecutive_errors = 0
        self._max_consecutive_errors = self._cfg["exchange"].get("reconnect_attempts", 10)

        # Event listeners: event_name -> [callbacks]
        self._listeners: Dict[str, List[EventCallback]] = {}

        logger.info(
            "DataFeed created – exchange=%s, symbols=%s, timeframes=%s, ws=%s",
            self._exchange_name,
            self._symbols,
            self._timeframes,
            self._use_ws,
        )

    # ------------------------------------------------------------------
    # Event system
    # ------------------------------------------------------------------

    def on(self, event: str, callback: EventCallback) -> None:
        """Register an async callback for *event*."""
        self._listeners.setdefault(event, []).append(callback)

    def off(self, event: str, callback: EventCallback) -> None:
        """Remove a previously registered callback."""
        cbs = self._listeners.get(event, [])
        if callback in cbs:
            cbs.remove(callback)

    async def _emit(self, event: str, **kwargs) -> None:
        """Fire all callbacks registered for *event*."""
        for cb in self._listeners.get(event, []):
            try:
                await cb(**kwargs)
            except Exception:
                logger.exception("Error in %s listener", event)

    # ------------------------------------------------------------------
    # Exchange factory
    # ------------------------------------------------------------------

    def _create_exchange(self) -> ccxt.Exchange:
        """Instantiate and configure the ccxt async exchange."""
        import os

        exchange_cls = getattr(ccxt, self._exchange_name, None)
        if exchange_cls is None:
            raise ValueError(f"Unsupported exchange: {self._exchange_name}")

        prefix = self._exchange_name.upper()
        api_key = os.environ.get(f"{prefix}_API_KEY", "")
        api_secret = os.environ.get(f"{prefix}_API_SECRET", "")

        options: Dict[str, Any] = {"defaultType": self._cfg["exchange"].get("market_type", "spot")}

        exchange_opts = {
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": self._cfg["exchange"].get("rate_limit", True),
            "options": options,
        }

        # Delta India region support — override API URLs
        region = self._cfg["exchange"].get("region", "").lower()
        if self._exchange_name == "delta" and region == "india":
            exchange_opts["urls"] = {
                "api": {
                    "public": "https://api.india.delta.exchange",
                    "private": "https://api.india.delta.exchange",
                },
            }

        exchange = exchange_cls(exchange_opts)

        if self._cfg["exchange"].get("testnet", False):
            exchange.set_sandbox_mode(True)

        return exchange

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the data feed.  Connects to the exchange and begins streaming."""
        if self._state not in (FeedState.IDLE, FeedState.STOPPED):
            logger.warning("DataFeed.start() called in state %s – ignoring", self._state)
            return

        self._stop_event.clear()
        self._state = FeedState.CONNECTING
        self._exchange = self._create_exchange()

        # Build subscriptions
        for symbol in self._symbols:
            self._subscriptions[symbol] = _Subscription(
                symbol=symbol, timeframes=list(self._timeframes)
            )

        # Load initial historical data
        await self._load_history()

        # Check if the exchange actually supports websocket OHLCV streaming.
        # Many exchanges (e.g. Delta Exchange) don't support watchOHLCV,
        # so we skip directly to REST polling instead of retrying forever.
        use_ws = self._use_ws
        if use_ws and self._exchange is not None:
            has_watch = getattr(self._exchange, 'has', {})
            if not has_watch.get('watchOHLCV', False):
                logger.info(
                    "Exchange '%s' does not support watchOHLCV – "
                    "using REST polling instead of websocket.",
                    self._exchange_name,
                )
                use_ws = False

        # Launch background workers
        if use_ws:
            self._tasks.append(asyncio.create_task(self._ws_loop()))
        else:
            self._tasks.append(asyncio.create_task(self._poll_loop()))

        self._tasks.append(asyncio.create_task(self._stale_checker()))

        logger.info("DataFeed started (%s)", "websocket" if use_ws else "polling")

    async def stop(self) -> None:
        """Gracefully shut down the feed."""
        logger.info("DataFeed stopping ...")
        self._stop_event.set()
        self._state = FeedState.STOPPED

        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        if self._exchange:
            try:
                await self._exchange.close()
            except Exception:
                logger.exception("Error closing exchange connection")
            self._exchange = None

        logger.info("DataFeed stopped.")

    @property
    def state(self) -> FeedState:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._state in (FeedState.STREAMING, FeedState.POLLING)

    # ------------------------------------------------------------------
    # Historical bootstrap
    # ------------------------------------------------------------------

    def _to_exchange_symbol(self, symbol: str) -> str:
        """Convert canonical symbol to exchange format."""
        region = self._cfg["exchange"].get("region", "").lower()
        if self._exchange_name == "delta" and region == "india":
            base = symbol.split("/")[0]
            return f"{base}/USD:USD"
        return symbol

    async def _load_history(self, limit: int = 200) -> None:
        """Fetch recent historical candles for all subscriptions via REST."""
        assert self._exchange is not None
        for sub in self._subscriptions.values():
            ex_symbol = self._to_exchange_symbol(sub.symbol)
            for tf in sub.timeframes:
                try:
                    ohlcv = await self._exchange.fetch_ohlcv(
                        ex_symbol, timeframe=tf, limit=limit
                    )
                    candles = [
                        {
                            "timestamp": row[0],
                            "open": row[1],
                            "high": row[2],
                            "low": row[3],
                            "close": row[4],
                            "volume": row[5],
                        }
                        for row in ohlcv
                    ]
                    self._dm.load_candles(sub.symbol, tf, candles, replace=True)
                    if candles:
                        sub.last_candle_ts[tf] = candles[-1]["timestamp"]
                        sub.last_data_time = time.monotonic()
                    logger.info(
                        "Loaded %d historical candles for %s/%s",
                        len(candles),
                        sub.symbol,
                        tf,
                    )
                except Exception:
                    logger.exception(
                        "Failed to load history for %s/%s", sub.symbol, tf
                    )

    # ------------------------------------------------------------------
    # Websocket streaming
    # ------------------------------------------------------------------

    async def _ws_loop(self) -> None:
        """Main websocket loop with automatic reconnection."""
        retries = 0
        while not self._stop_event.is_set():
            try:
                self._state = FeedState.STREAMING
                retries = 0
                self._consecutive_errors = 0
                await self._ws_stream()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                retries += 1
                self._consecutive_errors += 1

                # If the exchange flat-out doesn't support websockets,
                # skip straight to REST polling instead of retrying.
                err_msg = str(exc).lower()
                if "not supported" in err_msg or "not available" in err_msg:
                    logger.warning(
                        "Websocket not supported by exchange: %s – "
                        "switching to REST polling immediately.",
                        exc,
                    )
                    if self._rest_fallback:
                        await self._poll_loop()
                        return
                    else:
                        self._state = FeedState.STOPPED
                        return

                logger.error(
                    "Websocket error (attempt %d/%d): %s",
                    retries,
                    self._reconnect_attempts,
                    exc,
                )
                await self._emit(feed_error=str(exc), event="feed_error")

                if retries >= self._reconnect_attempts:
                    if self._rest_fallback:
                        logger.warning(
                            "Max WS retries reached – falling back to REST polling"
                        )
                        await self._poll_loop()
                        return
                    else:
                        logger.critical(
                            "Max WS retries reached and REST fallback disabled"
                        )
                        self._state = FeedState.STOPPED
                        return

                self._state = FeedState.RECONNECTING
                delay = min(self._reconnect_delay * (2 ** (retries - 1)), 120)
                logger.info("Reconnecting in %.1f s ...", delay)
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=delay
                    )
                    return  # stop_event was set
                except asyncio.TimeoutError:
                    pass

    async def _ws_stream(self) -> None:
        """
        Stream candle data via ccxt's watch_ohlcv.

        This inner method runs until an exception is raised or stop is
        requested.
        """
        assert self._exchange is not None

        async def _watch_symbol(sub: _Subscription, tf: str):
            while not self._stop_event.is_set():
                try:
                    ohlcv = await self._exchange.watch_ohlcv(sub.symbol, tf)
                except Exception:
                    raise  # let outer loop handle reconnection

                for row in ohlcv:
                    candle = {
                        "timestamp": row[0],
                        "open": row[1],
                        "high": row[2],
                        "low": row[3],
                        "close": row[4],
                        "volume": row[5],
                    }
                    self._dm.update_candle(sub.symbol, tf, candle)
                    sub.last_data_time = time.monotonic()

                    # Detect closed candle
                    prev_ts = sub.last_candle_ts.get(tf, 0)
                    if candle["timestamp"] > prev_ts and prev_ts > 0:
                        sub.last_candle_ts[tf] = candle["timestamp"]
                        await self._emit(
                            event="candle_closed",
                            symbol=sub.symbol,
                            timeframe=tf,
                            candle=candle,
                        )
                    else:
                        sub.last_candle_ts[tf] = candle["timestamp"]
                        await self._emit(
                            event="candle_update",
                            symbol=sub.symbol,
                            timeframe=tf,
                            candle=candle,
                        )

                    # Price update
                    await self._emit(
                        event="price_update",
                        symbol=sub.symbol,
                        price=candle["close"],
                    )

        # Launch one coroutine per (symbol, timeframe) pair
        watchers = []
        for sub in self._subscriptions.values():
            for tf in sub.timeframes:
                watchers.append(_watch_symbol(sub, tf))

        await asyncio.gather(*watchers)

    # ------------------------------------------------------------------
    # REST polling fallback
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Periodically fetch candles via REST when websocket is unavailable."""
        self._state = FeedState.POLLING
        logger.info("REST polling active – interval=%.1f s", self._poll_interval)

        # Per-symbol error tracking (prevents one bad symbol from killing all feeds)
        _sym_errors: Dict[str, int] = {}
        _sym_disabled: set = set()

        while not self._stop_event.is_set():
            for sub in self._subscriptions.values():
                # Skip symbols that have been disabled due to persistent errors
                if sub.symbol in _sym_disabled:
                    continue
                for tf in sub.timeframes:
                    try:
                        await self._poll_once(sub, tf)
                        # Success: reset this symbol's error count
                        _sym_errors[sub.symbol] = 0
                    except asyncio.CancelledError:
                        return
                    except Exception:
                        logger.warning(
                            "REST poll error for %s/%s", sub.symbol, tf
                        )
                        _sym_errors[sub.symbol] = _sym_errors.get(sub.symbol, 0) + 1
                        # Per-symbol circuit breaker: disable after 15 consecutive errors
                        # (3 poll cycles × 5 timeframes = 15). Other symbols unaffected.
                        if _sym_errors[sub.symbol] >= 15:
                            logger.error(
                                "REST DISABLED for %s: %d consecutive errors — "
                                "skipping until restart (other symbols unaffected)",
                                sub.symbol, _sym_errors[sub.symbol],
                            )
                            _sym_disabled.add(sub.symbol)
                            break  # skip remaining TFs for this symbol

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._poll_interval
                )
                return
            except asyncio.TimeoutError:
                pass

    async def _poll_once(self, sub: _Subscription, tf: str) -> None:
        """Fetch the latest candles for one (symbol, tf) pair via REST."""
        assert self._exchange is not None
        ex_symbol = self._to_exchange_symbol(sub.symbol)
        since = sub.last_candle_ts.get(tf)
        ohlcv = await self._exchange.fetch_ohlcv(
            ex_symbol, timeframe=tf, since=int(since) if since else None, limit=10
        )
        if not ohlcv:
            return

        self._consecutive_errors = 0
        sub.last_data_time = time.monotonic()

        for row in ohlcv:
            candle = {
                "timestamp": row[0],
                "open": row[1],
                "high": row[2],
                "low": row[3],
                "close": row[4],
                "volume": row[5],
            }
            prev_ts = sub.last_candle_ts.get(tf, 0)
            self._dm.update_candle(sub.symbol, tf, candle)

            if candle["timestamp"] > prev_ts and prev_ts > 0:
                await self._emit(
                    event="candle_closed",
                    symbol=sub.symbol,
                    timeframe=tf,
                    candle=candle,
                )
            sub.last_candle_ts[tf] = candle["timestamp"]

        # Price from last candle
        last_close = ohlcv[-1][4]
        self._dm.update_ticker_price(sub.symbol, last_close)
        await self._emit(
            event="price_update", symbol=sub.symbol, price=last_close
        )

    # ------------------------------------------------------------------
    # Stale data detection
    # ------------------------------------------------------------------

    async def _stale_checker(self) -> None:
        """Periodically check for stale subscriptions."""
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._stale_threshold
                )
                return
            except asyncio.TimeoutError:
                pass

            now = time.monotonic()
            for sub in self._subscriptions.values():
                if sub.last_data_time == 0:
                    continue
                age = now - sub.last_data_time
                if age > self._stale_threshold:
                    logger.warning(
                        "Stale data for %s – last update %.1f s ago",
                        sub.symbol,
                        age,
                    )
                    await self._emit(
                        event="feed_stale",
                        symbol=sub.symbol,
                        age_seconds=age,
                    )

    # ------------------------------------------------------------------
    # Dynamic subscription management
    # ------------------------------------------------------------------

    async def subscribe(self, symbol: str, timeframes: Optional[List[str]] = None) -> None:
        """Add a symbol subscription at runtime."""
        tfs = timeframes or list(self._timeframes)
        if symbol in self._subscriptions:
            logger.debug("Already subscribed to %s", symbol)
            return
        self._subscriptions[symbol] = _Subscription(symbol=symbol, timeframes=tfs)
        # Load history for the new subscription
        if self._exchange is not None:
            sub = self._subscriptions[symbol]
            for tf in tfs:
                try:
                    await self._poll_once(sub, tf)
                except Exception:
                    logger.exception("Failed initial poll for new sub %s/%s", symbol, tf)
        logger.info("Subscribed to %s (timeframes: %s)", symbol, tfs)

    async def unsubscribe(self, symbol: str) -> None:
        """Remove a symbol subscription."""
        self._subscriptions.pop(symbol, None)
        logger.info("Unsubscribed from %s", symbol)

    @property
    def subscribed_symbols(self) -> Set[str]:
        return set(self._subscriptions.keys())
