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
        return {
            "running": self._task is not None and not self._task.done(),
            "symbols": len(self._symbols),
            "cached": len(self._cache),
            "fetch_count": self._fetch_count,
            "error_count": self._error_count,
            "per_symbol": cached,
        }

    async def _poll_loop(self) -> None:
        """Background loop: poll each symbol sequentially with sleep between."""
        try:
            # Stagger startup to avoid burst
            await asyncio.sleep(5)
            while True:
                for symbol in self._symbols:
                    try:
                        # Run sync DeltaClient in thread pool
                        data = await asyncio.to_thread(
                            self._delta.get_l2_orderbook, symbol, self._depth,
                        )
                        if data:
                            self._cache[symbol] = {"data": data, "ts": time.time()}
                            self._fetch_count += 1
                        else:
                            self._error_count += 1
                    except Exception as e:
                        self._error_count += 1
                        logger.debug("OrderbookCache fetch %s failed: %s", symbol, e)
                    # Small sleep between symbols to spread rate load
                    await asyncio.sleep(self._poll_interval / max(1, len(self._symbols)))
        except asyncio.CancelledError:
            pass
