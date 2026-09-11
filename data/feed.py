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
    # Most recent candle dict per timeframe — emitted as the COMPLETED bar
    # when a newer timestamp appears (candle_closed must carry the finished
    # bar, not the one that just opened).
    last_candle: Dict[str, dict] = field(default_factory=dict)
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

    # Delta India uses 1000x-multiplier prefixes for thin-price memes
    # so contract size stays reasonable. Keep in sync with
    # exchange/ccxt_client.py._DELTA_BASE_OVERRIDE (2026-04-19).
    _DELTA_BASE_OVERRIDE = {
        "PEPE": "1000PEPE",
        "SHIB": "1000SHIB",
        "BONK": "1000BONK",
        "FLOKI": "1000FLOKI",
        "BABYDOGE": "1MBABYDOGE",
    }

    def _to_exchange_symbol(self, symbol: str) -> str:
        """Convert canonical symbol to exchange format.

        Delta India: BTC/USDT → BTC/USD:USD (futures).
        Meme 1000x contracts: PEPE/USDT → 1000PEPE/USD:USD.
        """
        region = self._cfg["exchange"].get("region", "").lower()
        if self._exchange_name == "delta" and region == "india":
            base = symbol.split("/")[0]
            base = self._DELTA_BASE_OVERRIDE.get(base, base)
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
                        sub.last_candle[tf] = candles[-1]
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

                    # Detect closed candle: a newer timestamp means the
                    # PREVIOUS bar is complete — emit that one, not the new
                    # (still-forming) bar.
                    prev_ts = sub.last_candle_ts.get(tf, 0)
                    if candle["timestamp"] > prev_ts and prev_ts > 0:
                        closed = sub.last_candle.get(tf)
                        sub.last_candle_ts[tf] = candle["timestamp"]
                        sub.last_candle[tf] = candle
                        if closed is not None:
                            await self._emit(
                                event="candle_closed",
                                symbol=sub.symbol,
                                timeframe=tf,
                                candle=closed,
                            )
                    else:
                        sub.last_candle_ts[tf] = candle["timestamp"]
                        sub.last_candle[tf] = candle
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

    # Timeframe length in ms — used to schedule polls right after each bar closes.
    _TF_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
              "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000}
    _POLL_GRACE_MS = 1_500          # fetch this long after the boundary (exchange finalises the bar)
    _POLL_PARALLEL = 8              # concurrent REST requests
    _POLL_RETRY_MS = 5_000          # retry a failed (symbol, tf) after this long
    _POLL_SAFETY_MS = 60_000        # 1m/5m frames: also re-poll at least this often (catches missed closes)

    def _next_boundary_ms(self, tf: str, now_ms: int) -> int:
        tf_ms = self._TF_MS.get(tf, 60_000)
        return (now_ms // tf_ms + 1) * tf_ms

    async def _poll_loop(self) -> None:
        """Boundary-aligned, parallel REST candle polling.

        Old behaviour: 100 sequential requests (20 symbols x 5 frames, ~300 ms
        each) then a 10 s sleep — a ~41 s cycle, so a 5m close was noticed
        0-41 s late. New behaviour: every (symbol, tf) is polled ~1.5 s after
        its own bar boundary, up to 8 requests in flight at once. 1m/5m frames
        are additionally refreshed at least once a minute; 15m/1h/4h only at
        their boundaries. The Delta WebSocket candlestick channels (see
        ingest_candle) usually deliver the close first; this loop is the
        backstop.
        """
        self._state = FeedState.POLLING
        logger.info("REST polling active — boundary-aligned, %d parallel", self._POLL_PARALLEL)
        _sym_errors: Dict[str, int] = {}
        _sym_disabled: set = set()
        next_due: Dict[tuple, int] = {}
        sem = asyncio.Semaphore(self._POLL_PARALLEL)

        async def _one(sub: _Subscription, tf: str, now_ms: int) -> None:
            async with sem:
                try:
                    await self._poll_once(sub, tf)
                    _sym_errors[sub.symbol] = 0
                    boundary = self._next_boundary_ms(tf, now_ms) + self._POLL_GRACE_MS
                    if tf in ("1m", "3m", "5m"):
                        boundary = min(boundary, now_ms + self._POLL_SAFETY_MS)
                    next_due[(sub.symbol, tf)] = boundary
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("REST poll error for %s/%s", sub.symbol, tf)
                    _sym_errors[sub.symbol] = _sym_errors.get(sub.symbol, 0) + 1
                    next_due[(sub.symbol, tf)] = now_ms + self._POLL_RETRY_MS
                    # Per-symbol circuit breaker: disable after 15 consecutive errors
                    # (3 poll cycles x 5 timeframes = 15). Other symbols unaffected.
                    if _sym_errors[sub.symbol] >= 15:
                        logger.error(
                            "REST DISABLED for %s: %d consecutive errors — "
                            "skipping until restart (other symbols unaffected)",
                            sub.symbol, _sym_errors[sub.symbol],
                        )
                        _sym_disabled.add(sub.symbol)

        while not self._stop_event.is_set():
            now_ms = int(time.time() * 1000)
            due = []
            for sub in list(self._subscriptions.values()):
                if sub.symbol in _sym_disabled:
                    continue
                for tf in sub.timeframes:
                    if now_ms >= next_due.get((sub.symbol, tf), 0):
                        due.append((sub, tf))
            if due:
                await asyncio.gather(*(_one(sub, tf, now_ms) for sub, tf in due), return_exceptions=True)
            # Sleep until the earliest due time (max 1 s so stop() stays responsive)
            pending = [t for k, t in next_due.items() if k[0] not in _sym_disabled]
            wait_ms = min([t - int(time.time() * 1000) for t in pending] + [1000]) if pending else 1000
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=max(0.05, min(wait_ms, 1000) / 1000))
                return
            except asyncio.TimeoutError:
                pass

    async def ingest_candle(self, symbol: str, tf: str, candle: Dict[str, Any]) -> None:
        """Feed a candle that arrived from the WebSocket candlestick channel.

        Same store + closed-bar detection as the REST poller, so a close is
        emitted exactly once whichever source sees it first. ``candle`` uses
        the REST shape: timestamp (ms), open, high, low, close, volume.
        """
        sub = self._subscriptions.get(symbol)
        if sub is None or tf not in sub.timeframes:
            return
        try:
            ts_ms = int(candle["timestamp"])
        except Exception:
            return
        if ts_ms > int(time.time() * 1000):
            return  # never store a bar that hasn't started
        prev_ts = sub.last_candle_ts.get(tf, 0)
        if ts_ms < prev_ts:
            return  # stale replay
        self._dm.update_candle(symbol, tf, candle)
        sub.last_data_time = time.monotonic()
        closed = sub.last_candle.get(tf) if (ts_ms > prev_ts and prev_ts > 0) else None
        # Advance the watermark BEFORE awaiting listeners: while a listener is
        # suspended another producer (REST vs WS) could otherwise observe the
        # old watermark and emit the same close twice.
        sub.last_candle_ts[tf] = ts_ms
        sub.last_candle[tf] = candle
        if closed is not None:
            await self._emit(event="candle_closed", symbol=symbol, timeframe=tf, candle=closed)

    async def _poll_once(self, sub: _Subscription, tf: str) -> None:
        """Fetch the latest candles for one (symbol, tf) pair via REST.

        Primary: CCXT fetch_ohlcv (works for BTC/ETH/SOL etc.)
        Fallback: Delta India native REST API using PRODUCT_MAP symbol mapping
        (works for 1000SHIBUSD, WIFUSD, SUIUSD etc. that CCXT can't resolve).
        """
        assert self._exchange is not None
        ex_symbol = self._to_exchange_symbol(sub.symbol)
        since = sub.last_candle_ts.get(tf)

        ohlcv = None
        try:
            ohlcv = await self._exchange.fetch_ohlcv(
                ex_symbol, timeframe=tf, since=int(since) if since else None, limit=10
            )
        except Exception:
            # CCXT failed — try Delta native candle API
            ohlcv = await self._delta_native_candles(sub.symbol, tf, limit=10)

        if not ohlcv:
            # CCXT returned nothing (thin Delta contracts omit bars for
            # minutes with no trades, and `since`-based requests come back
            # empty). Try the native endpoint before declaring the symbol stale.
            ohlcv = await self._delta_native_candles(sub.symbol, tf, limit=10)

        if not ohlcv:
            return

        # Delta's candle API pads a `since`+`limit` request with flat,
        # zero-volume placeholder bars stamped in the FUTURE (verified
        # 2026-09-09: 15m request returned bars up to +109 min ahead).
        # Storing them corrupts ATR / volume / regime for every strategy.
        # Keep only bars whose open time has already started.
        _now_ms = int(time.time() * 1000)
        ohlcv = [row for row in ohlcv if int(row[0]) <= _now_ms]
        if not ohlcv:
            return

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

            # A newer timestamp means the previous bar is COMPLETE. Emit the
            # finished bar; the new one is still forming (near-zero body and
            # volume) and must not drive analysis. The watermark is advanced
            # BEFORE awaiting listeners so a concurrent producer cannot emit
            # the same close a second time.
            closed = None
            if candle["timestamp"] > prev_ts and prev_ts > 0:
                closed = sub.last_candle.get(tf)
            if candle["timestamp"] >= prev_ts:
                sub.last_candle_ts[tf] = candle["timestamp"]
                sub.last_candle[tf] = candle
            if closed is not None:
                await self._emit(
                    event="candle_closed",
                    symbol=sub.symbol,
                    timeframe=tf,
                    candle=closed,
                )

        # Price from last candle
        last_close = ohlcv[-1][4]
        self._dm.update_ticker_price(sub.symbol, last_close)
        await self._emit(
            event="price_update", symbol=sub.symbol, price=last_close
        )

    # ------------------------------------------------------------------
    # Delta India native candle fetcher (bypass CCXT for unmapped symbols)
    # ------------------------------------------------------------------

    _TF_TO_RESOLUTION = {
        "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
        "1h": "1h", "2h": "2h", "4h": "4h", "6h": "6h", "1d": "1d", "1w": "1w",
    }

    async def _delta_native_candles(self, symbol: str, tf: str, limit: int = 10):
        """Fetch candles from Delta India REST API directly (not CCXT).

        Uses GET /v2/history/candles?resolution=X&symbol=Y&start=Z&end=W
        Works for ALL Delta symbols including 1000SHIBUSD, WIFUSD etc.
        Returns OHLCV in the same [[ts, o, h, l, c, v], ...] format as CCXT.
        """
        try:
            from exchange.delta_client import PRODUCT_MAP
            import aiohttp

            pinfo = PRODUCT_MAP.get(symbol)
            if not pinfo:
                return None

            delta_symbol = pinfo.get("symbol", "")  # e.g., "1000SHIBUSD"
            if not delta_symbol:
                return None

            resolution = self._TF_TO_RESOLUTION.get(tf, tf)
            now = int(time.time())
            # Estimate start time from limit + timeframe
            tf_seconds = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800,
                          "1h": 3600, "4h": 14400, "1d": 86400}.get(tf, 300)
            start = now - (limit * tf_seconds * 2)  # 2× for safety margin

            url = (
                f"https://api.india.delta.exchange/v2/history/candles"
                f"?resolution={resolution}&symbol={delta_symbol}&start={start}&end={now}"
            )

            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()

            if not data.get("success"):
                return None

            result = data.get("result", [])
            if not result:
                return None

            # Delta returns candles NEWEST-FIRST. Sort ascending before slicing,
            # otherwise result[-limit:] keeps the OLDEST bars and the latest
            # candles are never ingested (symbols on this path went stale).
            result = sorted(result, key=lambda c: int(c.get("time", 0)))

            # Convert to CCXT format: [[timestamp_ms, open, high, low, close, volume], ...]
            ohlcv = []
            for c in result[-limit:]:
                ohlcv.append([
                    int(c["time"]) * 1000,  # Delta returns seconds, CCXT uses milliseconds
                    float(c["open"]),
                    float(c["high"]),
                    float(c["low"]),
                    float(c["close"]),
                    float(c.get("volume", 0)),
                ])

            if ohlcv:
                logger.debug(
                    "Delta native candles: %s %s → %d candles (symbol=%s)",
                    symbol, tf, len(ohlcv), delta_symbol,
                )
            return ohlcv if ohlcv else None

        except Exception as e:
            logger.debug("Delta native candle fetch failed for %s/%s: %s", symbol, tf, e)
            return None

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

    def freshness(self) -> Dict[str, Any]:
        """Per-symbol data age, for the dashboard health chip and the
        staleness gates in the orchestrator.

        2026-09-11: the whole candle feed died for 17 minutes and the WS
        dropped ten times in a day; trades were opened on frozen frames and
        judged on frozen prices, with 1-2% "slippage" when data resumed. A
        single global last_data_update hid all of it.
        """
        now_mono = time.monotonic()
        now_s = time.time()

        def _tf_s(tf: str) -> int:
            try:
                n, u = int(tf[:-1]), tf[-1]
                return n * {"m": 60, "h": 3600, "d": 86400}[u]
            except Exception:
                return 300

        out: Dict[str, Any] = {"symbols": {}, "worst_age_s": 0.0, "stale": []}
        for sym, sub in self._subscriptions.items():
            data_age = (now_mono - sub.last_data_time) if sub.last_data_time else None
            candles = {}
            worst = data_age or 0.0
            for tf in sub.timeframes:
                ts = sub.last_candle_ts.get(tf)
                if ts:
                    ts_s = float(ts) / (1000.0 if float(ts) > 1e11 else 1.0)
                    # age of the bar that should currently be forming
                    age = now_s - ts_s - _tf_s(tf)
                    candles[tf] = {"last_open_ts": ts_s, "age_s": round(age, 1), "bars_behind": round(age / _tf_s(tf), 2)}
                    if tf in ("1m", "5m"):
                        worst = max(worst, age)
                else:
                    candles[tf] = None
            out["symbols"][sym] = {"data_age_s": round(data_age, 1) if data_age is not None else None, "candles": candles}
            if worst > 2 * 300:  # more than two 5m bars behind
                out["stale"].append(sym)
            out["worst_age_s"] = max(out["worst_age_s"], worst)
        out["worst_age_s"] = round(out["worst_age_s"], 1)
        return out

    @property
    def subscribed_symbols(self) -> Set[str]:
        return set(self._subscriptions.keys())
