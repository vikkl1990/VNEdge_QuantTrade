"""
DataManager: centralized OHLCV candle storage for multiple symbols and timeframes.

Stores candles in pandas DataFrames, handles gap detection / filling,
normalizes timestamps, and exposes a simple query API used by strategies
and the backtester.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from config import get_config

logger = logging.getLogger(__name__)

# Mapping of timeframe strings to pandas offset aliases
_TF_TO_OFFSET: Dict[str, str] = {
    "1m": "1min",
    "3m": "3min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "6h": "6h",
    "8h": "8h",
    "12h": "12h",
    "1d": "1D",
    "1w": "1W",
}

# Timeframe durations in seconds (for gap detection)
_TF_SECONDS: Dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "8h": 28800,
    "12h": 43200,
    "1d": 86400,
    "1w": 604800,
}

# Canonical column order for all candle DataFrames
CANDLE_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


def _empty_candle_df() -> pd.DataFrame:
    """Return an empty DataFrame with the canonical candle schema."""
    df = pd.DataFrame(columns=CANDLE_COLUMNS)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    return df


class _LRUCache(OrderedDict):
    """Simple LRU eviction dict with a configurable max size."""

    def __init__(self, maxsize: int = 128):
        super().__init__()
        self.maxsize = maxsize

    def __getitem__(self, key):
        self.move_to_end(key)
        return super().__getitem__(key)

    def __setitem__(self, key, value):
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        if len(self) > self.maxsize:
            oldest = next(iter(self))
            logger.debug("LRU evicting cache key: %s", oldest)
            del self[oldest]


class DataManager:
    """
    Thread-safe in-memory store for OHLCV candle data.

    Keyed by ``(symbol, timeframe)`` tuples.  Each entry is a pandas
    DataFrame sorted by timestamp with duplicates removed.

    Parameters
    ----------
    max_cache_entries : int
        Maximum number of (symbol, timeframe) pairs to keep in memory.
        Oldest-accessed entries are evicted when the limit is exceeded.
    max_candles_per_key : int
        Maximum rows retained per (symbol, timeframe) DataFrame.
        Older rows are trimmed on every write.
    """

    def __init__(
        self,
        max_cache_entries: int = 256,
        max_candles_per_key: int = 5000,
    ):
        cfg = get_config()
        self._symbols: List[str] = cfg.get("symbols", [])
        self._timeframes: Dict[str, str] = cfg.get("timeframes", {})
        self._max_candles = max_candles_per_key

        self._store: _LRUCache = _LRUCache(maxsize=max_cache_entries)
        self._lock = threading.RLock()

        # Latest price cache: symbol -> float
        self._latest_prices: Dict[str, float] = {}

        logger.info(
            "DataManager initialised – symbols=%s, timeframes=%s, "
            "max_cache_entries=%d, max_candles=%d",
            self._symbols,
            self._timeframes,
            max_cache_entries,
            max_candles_per_key,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _key(self, symbol: str, timeframe: str) -> Tuple[str, str]:
        return (symbol.upper(), timeframe.lower())

    def _get_or_create(self, symbol: str, timeframe: str) -> pd.DataFrame:
        """Return the DataFrame for *key*, creating one if absent."""
        key = self._key(symbol, timeframe)
        if key not in self._store:
            self._store[key] = _empty_candle_df()
        return self._store[key]

    @staticmethod
    def _normalize_timestamp(ts) -> pd.Timestamp:
        """Coerce various timestamp formats to a tz-aware UTC Timestamp."""
        if isinstance(ts, (int, float)):
            # Accept seconds or milliseconds
            if ts > 1e12:
                ts = ts / 1000.0
            return pd.Timestamp(ts, unit="s", tz="UTC")
        result = pd.Timestamp(ts)
        if result.tzinfo is None:
            result = result.tz_localize("UTC")
        return result.tz_convert("UTC")

    def _normalize_df(self, df: pd.DataFrame) -> pd.DataFrame:
        """Ensure consistent types, column order, and sort by timestamp."""
        df = df.copy()
        df["timestamp"] = df["timestamp"].apply(self._normalize_timestamp)
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df[CANDLE_COLUMNS]
        df = df.sort_values("timestamp").reset_index(drop=True)
        df = df.drop_duplicates(subset=["timestamp"], keep="last").reset_index(drop=True)
        return df

    def _trim(self, df: pd.DataFrame) -> pd.DataFrame:
        """Keep only the most recent ``max_candles`` rows."""
        if len(df) > self._max_candles:
            return df.iloc[-self._max_candles :].reset_index(drop=True)
        return df

    # ------------------------------------------------------------------
    # Gap detection & filling
    # ------------------------------------------------------------------

    def detect_gaps(
        self, symbol: str, timeframe: str
    ) -> List[Tuple[pd.Timestamp, pd.Timestamp]]:
        """Return a list of (start, end) timestamp pairs for missing candles."""
        with self._lock:
            df = self._get_or_create(symbol, timeframe)
        if len(df) < 2:
            return []

        tf_seconds = _TF_SECONDS.get(timeframe.lower())
        if tf_seconds is None:
            logger.warning("Unknown timeframe %s – skipping gap detection", timeframe)
            return []

        expected_delta = pd.Timedelta(seconds=tf_seconds)
        gaps: List[Tuple[pd.Timestamp, pd.Timestamp]] = []
        timestamps = df["timestamp"].values
        for i in range(1, len(timestamps)):
            delta = pd.Timestamp(timestamps[i]) - pd.Timestamp(timestamps[i - 1])
            if delta > expected_delta * 1.5:
                gaps.append(
                    (pd.Timestamp(timestamps[i - 1]), pd.Timestamp(timestamps[i]))
                )
        if gaps:
            logger.info(
                "%s/%s: detected %d gap(s) in candle data",
                symbol,
                timeframe,
                len(gaps),
            )
        return gaps

    def fill_gaps(self, symbol: str, timeframe: str) -> int:
        """
        Forward-fill missing candles using the previous close.

        Returns the number of rows inserted.
        """
        tf_key = timeframe.lower()
        offset = _TF_TO_OFFSET.get(tf_key)
        if offset is None:
            logger.warning("Cannot fill gaps for unknown timeframe: %s", timeframe)
            return 0

        with self._lock:
            df = self._get_or_create(symbol, timeframe)
            if len(df) < 2:
                return 0

            df = df.set_index("timestamp")
            full_idx = pd.date_range(
                start=df.index.min(), end=df.index.max(), freq=offset
            )
            before = len(df)
            df = df.reindex(full_idx)

            # Forward-fill OHLC with previous close; volume = 0 for synthetic bars
            df["close"] = df["close"].ffill()
            for col in ("open", "high", "low"):
                df[col] = df[col].fillna(df["close"])
            df["volume"] = df["volume"].fillna(0.0)
            df = df.reset_index().rename(columns={"index": "timestamp"})
            df = self._normalize_df(df)
            df = self._trim(df)

            key = self._key(symbol, timeframe)
            self._store[key] = df
            inserted = len(df) - before

        if inserted > 0:
            logger.info(
                "%s/%s: filled %d missing candle(s)", symbol, timeframe, inserted
            )
        return inserted

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------

    def get_candles(
        self,
        symbol: str,
        timeframe: str,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Return the most recent *limit* candles for a symbol/timeframe pair.

        If *limit* is ``None`` the full stored history is returned.
        Always returns a copy so callers cannot mutate internal state.
        """
        with self._lock:
            df = self._get_or_create(symbol, timeframe).copy()
        if limit is not None and len(df) > limit:
            df = df.iloc[-limit:].reset_index(drop=True)
        return df

    def get_latest_price(self, symbol: str) -> Optional[float]:
        """Return the most recent close price for *symbol*, or ``None``."""
        sym = symbol.upper()
        with self._lock:
            if sym in self._latest_prices:
                return self._latest_prices[sym]
            # Fallback: scan stored DataFrames for the smallest timeframe
            for tf in ("1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d"):
                key = (sym, tf)
                if key in self._store and len(self._store[key]) > 0:
                    return float(self._store[key]["close"].iloc[-1])
        return None

    def has_data(self, symbol: str, timeframe: str) -> bool:
        """Return ``True`` if at least one candle is stored."""
        with self._lock:
            key = self._key(symbol, timeframe)
            return key in self._store and len(self._store[key]) > 0

    def candle_count(self, symbol: str, timeframe: str) -> int:
        """Return the number of stored candles."""
        with self._lock:
            key = self._key(symbol, timeframe)
            if key in self._store:
                return len(self._store[key])
        return 0

    @property
    def symbols(self) -> List[str]:
        return list(self._symbols)

    @property
    def timeframes(self) -> Dict[str, str]:
        return dict(self._timeframes)

    # ------------------------------------------------------------------
    # Public write API
    # ------------------------------------------------------------------

    def update_candle(
        self,
        symbol: str,
        timeframe: str,
        candle_data: Dict[str, Any],
    ) -> None:
        """
        Insert or update a single candle.

        ``candle_data`` must contain at minimum:
        ``timestamp``, ``open``, ``high``, ``low``, ``close``, ``volume``.
        If a candle with the same timestamp already exists it is replaced.
        """
        row = {col: candle_data[col] for col in CANDLE_COLUMNS}
        new_row = pd.DataFrame([row])
        new_row = self._normalize_df(new_row)

        with self._lock:
            df = self._get_or_create(symbol, timeframe)
            ts = new_row["timestamp"].iloc[0]

            # Replace existing row for this timestamp or append
            mask = df["timestamp"] == ts
            if mask.any():
                idx = df.index[mask][0]
                for col in CANDLE_COLUMNS:
                    df.at[idx, col] = new_row[col].iloc[0]
            else:
                df = pd.concat([df, new_row], ignore_index=True)
                df = df.sort_values("timestamp").reset_index(drop=True)

            df = self._trim(df)
            key = self._key(symbol, timeframe)
            self._store[key] = df

            # Update latest price cache
            close = float(new_row["close"].iloc[0])
            self._latest_prices[symbol.upper()] = close

    def load_candles(
        self,
        symbol: str,
        timeframe: str,
        candles: List[Dict[str, Any]] | pd.DataFrame,
        replace: bool = False,
    ) -> int:
        """
        Bulk-load historical candles.

        Parameters
        ----------
        candles : list[dict] | DataFrame
            Candle rows.  Must contain the canonical columns.
        replace : bool
            If ``True`` the existing data is discarded before loading.

        Returns
        -------
        int
            Number of candles stored after loading.
        """
        if isinstance(candles, pd.DataFrame):
            incoming = candles.copy()
        else:
            incoming = pd.DataFrame(candles)

        if incoming.empty:
            return 0

        incoming = self._normalize_df(incoming)

        with self._lock:
            if replace:
                df = incoming
            else:
                existing = self._get_or_create(symbol, timeframe)
                df = pd.concat([existing, incoming], ignore_index=True)
                df = self._normalize_df(df)

            df = self._trim(df)
            key = self._key(symbol, timeframe)
            self._store[key] = df

            # Update latest price
            if len(df) > 0:
                self._latest_prices[symbol.upper()] = float(df["close"].iloc[-1])

            count = len(df)

        logger.info(
            "%s/%s: loaded %d candle(s) (total stored: %d)",
            symbol,
            timeframe,
            len(incoming),
            count,
        )
        return count

    def update_ticker_price(self, symbol: str, price: float) -> None:
        """Update the latest price cache from a ticker/trade stream."""
        with self._lock:
            self._latest_prices[symbol.upper()] = price

    def clear(self, symbol: Optional[str] = None, timeframe: Optional[str] = None) -> None:
        """
        Remove stored data.

        - No args: clear everything.
        - symbol only: clear all timeframes for that symbol.
        - symbol + timeframe: clear one specific key.
        """
        with self._lock:
            if symbol and timeframe:
                key = self._key(symbol, timeframe)
                self._store.pop(key, None)
            elif symbol:
                sym = symbol.upper()
                keys_to_drop = [k for k in self._store if k[0] == sym]
                for k in keys_to_drop:
                    del self._store[k]
                self._latest_prices.pop(sym, None)
            else:
                self._store.clear()
                self._latest_prices.clear()
        logger.info("DataManager cleared – symbol=%s, timeframe=%s", symbol, timeframe)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def summary(self) -> Dict[str, Any]:
        """Return a diagnostic summary of stored data."""
        with self._lock:
            entries = {}
            for (sym, tf), df in self._store.items():
                entries[f"{sym}/{tf}"] = {
                    "count": len(df),
                    "first": str(df["timestamp"].iloc[0]) if len(df) else None,
                    "last": str(df["timestamp"].iloc[-1]) if len(df) else None,
                }
            return {
                "cache_entries": len(self._store),
                "max_cache_entries": self._store.maxsize,
                "max_candles_per_key": self._max_candles,
                "latest_prices": dict(self._latest_prices),
                "data": entries,
            }
