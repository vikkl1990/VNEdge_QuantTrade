"""Orderbook Cache — async background L2 poller (Phase 5.0c).

Polls delta_client.get_l2_orderbook() every poll_interval seconds for
each symbol. Stores the latest snapshot in an in-memory dict. Stale
detection: if cache age > stale_threshold, get() returns None.

Rate budget: 10 symbols × 1 req/10s = 60 req/min (within 150/min limit).
Memory: ~20 levels × 2 floats × 2 sides × 10 symbols ≈ 3 KB.

Usage:
    cache = OrderbookCache(delta_client, ["BTC/USDT", "ETH/USDT"], poll_interval=10)
    await cache.start()
    ob = cache.get("BTC/USDT")  # dict or None
    await cache.stop()
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("bot.orderbook_manager")


class OrderbookCache:
    """Background L2 orderbook poller with in-memory cache."""

    def __init__(
        self,
        delta_client,
        symbols: List[str],
        poll_interval: float = 10.0,
        stale_threshold: float = 30.0,
        depth: int = 20,
    ):
        self._delta = delta_client
        self._symbols = list(symbols)
        self._poll_interval = poll_interval
        self._stale_threshold = stale_threshold
        self._depth = depth
        self._cache: Dict[str, Dict[str, Any]] = {}  # symbol → {data, ts}
        self._task: Optional[asyncio.Task] = None
        self._fetch_count: int = 0
        self._error_count: int = 0

    async def start(self) -> None:
        """Create the background polling task."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._poll_loop(), name="orderbook_poller")
        logger.info(
            "OrderbookCache: started (symbols=%d, interval=%.0fs, depth=%d)",
            len(self._symbols), self._poll_interval, self._depth,
        )

    async def stop(self) -> None:
        """Cancel the polling task."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("OrderbookCache: stopped (%d fetches, %d errors)",
                       self._fetch_count, self._error_count)

    def get(self, symbol: str) -> Optional[Dict]:
        """Return cached L2 snapshot, or None if missing/stale.

        Returns dict with keys: buy (list of [price, size]), sell, symbol.
        """
        entry = self._cache.get(symbol)
        if entry is None:
            return None
        age = time.time() - entry["ts"]
        if age > self._stale_threshold:
            return None  # stale
        return entry["data"]

    @property
    def status(self) -> Dict[str, Any]:
        """Summary for dashboard / debugging."""
        now = time.time()
        cached = {}
        for sym, entry in self._cache.items():
            cached[sym] = {
                "age_sec": round(now - entry["ts"], 1),
                "bid_levels": len(entry["data"].get("buy", [])) if entry["data"] else 0,
                "ask_levels": len(entry["data"].get("sell", [])) if entry["data"] else 0,
            }

        task_alive = self._task is not None and not self._task.done()
        inner_client = getattr(self._delta, "_client", None)
        # Honest state label — dashboard was showing green "RUNNING" while
        # we were accumulating 45k errors. Now we distinguish:
        #   RUNNING  — task alive, client healthy, fetches succeeding
        #   DEGRADED — task alive but inner REST client is None (fetches fail)
        #   STALLED  — task alive, client healthy, but many consecutive fails
        #   STOPPED  — task not alive
        if not task_alive:
            state = "STOPPED"
        elif inner_client is None:
            state = "DEGRADED"
        elif self._error_count > 100 and self._fetch_count == 0:
            state = "DEGRADED"
        elif self._error_count > self._fetch_count * 5 and self._error_count > 50:
            state = "STALLED"
        else:
            state = "RUNNING"

        return {
            "running": task_alive,              # backward-compat
            "state": state,                     # new truthful label
            "symbols": len(self._symbols),
            "cached": len(self._cache),
            "fetch_count": self._fetch_count,
            "error_count": self._error_count,
            "error_rate": round(self._error_count / max(1, self._error_count + self._fetch_count), 3),
            "per_symbol": cached,
        }

    async def _poll_loop(self) -> None:
        """Background loop: poll each symbol sequentially with sleep between.

        Defensive circuit-breaker (2026-04-16): if the inner delta REST
        client is None, this loop was silently accumulating errors at ~2/sec
        (e.g. 45k errors in 6h). Now we detect it ONCE, log a WARNING, and
        slow the poll to 60s until the client is healthy.
        """
        try:
            # Stagger startup to avoid burst
            await asyncio.sleep(5)
            consecutive_errors = 0
            degraded_mode = False
            while True:
                # Circuit-breaker: check inner client health BEFORE each sweep.
                _inner = getattr(self._delta, "_client", None)
                if _inner is None:
                    if not degraded_mode:
                        logger.warning(
                            "OrderbookCache: inner REST client is None — "
                            "delta_client.connect() likely failed. Entering "
                            "degraded mode (60s poll). Fix upstream before expecting data."
                        )
                        degraded_mode = True
                    self._error_count += len(self._symbols)  # book-keeping
                    await asyncio.sleep(60)
                    continue
                else:
                    if degraded_mode:
                        logger.info("OrderbookCache: inner REST client recovered, resuming normal poll")
                        degraded_mode = False
                        consecutive_errors = 0

                for symbol in self._symbols:
                    try:
                        # Run sync DeltaClient in thread pool
                        data = await asyncio.to_thread(
                            self._delta.get_l2_orderbook, symbol, self._depth,
                        )
                        if data:
                            self._cache[symbol] = {"data": data, "ts": time.time()}
                            self._fetch_count += 1
                            consecutive_errors = 0
                        else:
                            self._error_count += 1
                            consecutive_errors += 1
                    except Exception as e:
                        self._error_count += 1
                        consecutive_errors += 1
                        logger.debug("OrderbookCache fetch %s failed: %s", symbol, e)

                    # If every symbol has errored for a full sweep, log once at WARNING
                    if consecutive_errors == len(self._symbols):
                        logger.warning(
                            "OrderbookCache: %d consecutive fetch failures — "
                            "check delta REST connectivity", consecutive_errors,
                        )

                    # Small sleep between symbols to spread rate load
                    await asyncio.sleep(self._poll_interval / max(1, len(self._symbols)))
        except asyncio.CancelledError:
            pass
