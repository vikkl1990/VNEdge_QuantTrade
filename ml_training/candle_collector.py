"""
Candle Data Collector
=====================
Downloads and stores 90-180 days of historical candle data across multiple
timeframes (1m, 3m, 5m, 15m, 1h, 4h, 1d) from the exchange.

Stores as parquet files in storage/candle_cache/ for fast retrieval.
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

STORAGE_DIR = Path(__file__).resolve().parent.parent / "storage" / "candle_cache"
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

# Timeframe → milliseconds
TF_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

# Default collection timeframes
DEFAULT_TIMEFRAMES = ["1m", "3m", "5m", "15m", "1h", "4h", "1d"]

# How many days per timeframe (1m needs more API calls)
TF_DAYS = {
    "1m": 90,
    "3m": 120,
    "5m": 150,
    "15m": 180,
    "1h": 180,
    "4h": 180,
    "1d": 365,
}


class CandleCollector:
    """Downloads and caches historical candle data."""

    def __init__(self, exchange_client, symbols: List[str],
                 timeframes: Optional[List[str]] = None):
        self._exchange = exchange_client
        # Get the raw ccxt exchange for historical fetches (supports `since` param)
        self._raw_exchange = getattr(exchange_client, '_exchange', None)
        self._symbols = symbols
        self._timeframes = timeframes or DEFAULT_TIMEFRAMES
        self._progress: Dict[str, Dict[str, dict]] = {}  # symbol → tf → status

    def get_progress(self) -> Dict:
        return self._progress

    def _cache_path(self, symbol: str, timeframe: str) -> Path:
        safe_symbol = symbol.replace("/", "_").replace(":", "_")
        return STORAGE_DIR / f"{safe_symbol}_{timeframe}.parquet"

    def _csv_path(self, symbol: str, timeframe: str) -> Path:
        safe_symbol = symbol.replace("/", "_").replace(":", "_")
        return STORAGE_DIR / f"{safe_symbol}_{timeframe}.csv"

    def load_cached(self, symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
        """Load cached candle data if it exists."""
        pq_path = self._cache_path(symbol, timeframe)
        csv_path = self._csv_path(symbol, timeframe)

        if pq_path.exists():
            try:
                df = pd.read_parquet(pq_path)
                if not df.empty:
                    return df
            except Exception:
                pass

        if csv_path.exists():
            try:
                df = pd.read_csv(csv_path, parse_dates=["datetime"])
                df.set_index("datetime", inplace=True)
                if not df.empty:
                    return df
            except Exception:
                pass

        return None

    async def collect_symbol_tf(self, symbol: str, timeframe: str,
                                 days: Optional[int] = None) -> pd.DataFrame:
        """Download candle data for one symbol+timeframe."""
        days = days or TF_DAYS.get(timeframe, 90)
        tf_ms = TF_MS[timeframe]
        candles_per_request = 1000
        total_candles_needed = int((days * 86_400_000) / tf_ms)

        key = f"{symbol}_{timeframe}"
        self._progress.setdefault(symbol, {})[timeframe] = {
            "status": "downloading",
            "total_needed": total_candles_needed,
            "downloaded": 0,
            "pct": 0,
        }

        # Check existing cache and only fetch what's missing
        existing_df = self.load_cached(symbol, timeframe)
        if existing_df is not None and len(existing_df) > 0:
            last_ts = existing_df.index.max()
            if last_ts.tzinfo is None:
                last_ts = last_ts.tz_localize("UTC")
            gap_ms = int((datetime.now(timezone.utc) - last_ts).total_seconds() * 1000)
            if gap_ms < tf_ms * 2:
                logger.info("Cache fresh for %s %s (%d candles)", symbol, timeframe, len(existing_df))
                self._progress[symbol][timeframe] = {
                    "status": "cached",
                    "total_needed": total_candles_needed,
                    "downloaded": len(existing_df),
                    "pct": 100,
                }
                return existing_df
            # Resume from last timestamp
            since_ms = int(last_ts.timestamp() * 1000) + tf_ms
        else:
            existing_df = None
            end_dt = datetime.now(timezone.utc)
            start_dt = end_dt - timedelta(days=days)
            since_ms = int(start_dt.timestamp() * 1000)

        all_rows = []
        request_count = 0
        max_requests = (total_candles_needed // candles_per_request) + 5

        # Resolve exchange symbol (e.g. BTC/USDT → BTC/USD:USD for Delta)
        if hasattr(self._exchange, '_to_exchange_symbol'):
            ex_symbol = self._exchange._to_exchange_symbol(symbol)
        else:
            ex_symbol = symbol

        while request_count < max_requests:
            try:
                # Use raw ccxt exchange which supports `since` parameter
                if self._raw_exchange is not None:
                    raw = await self._raw_exchange.fetch_ohlcv(
                        ex_symbol, timeframe, since=since_ms, limit=candles_per_request
                    )
                else:
                    # Fallback: use wrapper (no since, just get latest)
                    raw_ohlcv = await self._exchange.fetch_ohlcv(
                        symbol, timeframe, limit=candles_per_request
                    )
                    raw = []
                    for c in raw_ohlcv:
                        if hasattr(c, 'timestamp'):
                            raw.append([c.timestamp, c.open, c.high, c.low, c.close, c.volume])
                        else:
                            raw.append(list(c[:6]))

                if not raw:
                    break

                # Delta's candle API pads `since`+`limit` ranges with flat,
                # zero-volume placeholder bars stamped in the FUTURE. Drop them
                # or the training set (and the pagination cursor) gets poisoned.
                _now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                raw = [c for c in raw
                       if (c.timestamp if hasattr(c, 'timestamp') else c[0]) <= _now_ms]
                if not raw:
                    break

                # Handle both list and OHLCV dataclass
                for candle in raw:
                    if hasattr(candle, 'timestamp'):
                        row = [candle.timestamp, candle.open, candle.high,
                               candle.low, candle.close, candle.volume]
                    elif isinstance(candle, (list, tuple)):
                        row = list(candle[:6])
                    else:
                        continue
                    all_rows.append(row)

                last_ts_ms = all_rows[-1][0]
                if last_ts_ms >= int(datetime.now(timezone.utc).timestamp() * 1000) - tf_ms:
                    break

                since_ms = last_ts_ms + tf_ms
                request_count += 1

                self._progress[symbol][timeframe]["downloaded"] = len(all_rows)
                self._progress[symbol][timeframe]["pct"] = min(
                    100, int(len(all_rows) / max(total_candles_needed, 1) * 100)
                )

                # Rate limit
                await asyncio.sleep(0.15)

            except Exception as e:
                error_str = str(e)
                if "does not have market symbol" in error_str:
                    logger.warning("Symbol %s not available on exchange, skipping", symbol)
                    break
                logger.warning("Fetch error %s %s: %s, retrying...", symbol, timeframe, e)
                await asyncio.sleep(2.0)
                request_count += 1

        if not all_rows:
            logger.warning("No new candles for %s %s", symbol, timeframe)
            if existing_df is not None:
                return existing_df
            return pd.DataFrame()

        # Build DataFrame
        new_df = pd.DataFrame(all_rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        new_df["datetime"] = pd.to_datetime(new_df["timestamp"], unit="ms", utc=True)
        new_df.set_index("datetime", inplace=True)
        new_df.drop_duplicates(keep="last", inplace=True)
        new_df.sort_index(inplace=True)

        # Merge with existing
        if existing_df is not None:
            combined = pd.concat([existing_df, new_df])
            combined = combined[~combined.index.duplicated(keep="last")]
            combined.sort_index(inplace=True)
        else:
            combined = new_df

        # Save cache
        try:
            combined.to_parquet(self._cache_path(symbol, timeframe))
        except Exception:
            combined.to_csv(self._csv_path(symbol, timeframe))

        self._progress[symbol][timeframe] = {
            "status": "complete",
            "total_needed": total_candles_needed,
            "downloaded": len(combined),
            "pct": 100,
        }

        logger.info("Collected %d candles for %s %s", len(combined), symbol, timeframe)
        return combined

    def collect_yfinance(self, symbol: str, timeframe: str, months: int = 6) -> Optional[pd.DataFrame]:
        """Fetch historical data from yfinance as supplemental training data.

        Maps Delta symbols (BTC/USDT) to yfinance tickers (BTC-USD).
        Returns OHLCV DataFrame or None if unavailable.
        """
        try:
            import yfinance as yf
        except ImportError:
            logger.debug("yfinance not installed — skipping supplemental data")
            return None

        # Map symbol to yfinance ticker
        YFINANCE_MAP = {
            "BTC/USDT": "BTC-USD", "ETH/USDT": "ETH-USD", "SOL/USDT": "SOL-USD",
            "XRP/USDT": "XRP-USD", "DOGE/USDT": "DOGE-USD", "ADA/USDT": "ADA-USD",
            "LINK/USDT": "LINK-USD", "DOT/USDT": "DOT-USD", "LTC/USDT": "LTC-USD",
            "AVAX/USDT": "AVAX-USD", "BNB/USDT": "BNB-USD", "TAO/USDT": "TAO-USD",
        }
        yf_ticker = YFINANCE_MAP.get(symbol)
        if not yf_ticker:
            return None

        # Map timeframe to yfinance interval
        TF_MAP = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}
        interval = TF_MAP.get(timeframe)
        if not interval:
            return None

        # yfinance limits: 1m=7d, 5m=60d, 15m=60d, 1h=730d
        period_map = {"1m": "7d", "5m": "60d", "15m": "60d", "1h": f"{months * 30}d", "4h": f"{months * 30}d", "1d": f"{months * 30}d"}
        period = period_map.get(timeframe, "60d")

        try:
            ticker = yf.Ticker(yf_ticker)
            df = ticker.history(period=period, interval=interval)
            if df.empty:
                return None

            # Normalize column names to match our format
            df = df.rename(columns={
                "Open": "open", "High": "high", "Low": "low",
                "Close": "close", "Volume": "volume",
            })
            df = df[["open", "high", "low", "close", "volume"]].copy()
            df.index.name = "datetime"

            logger.info("yfinance: %d candles for %s %s (%s)", len(df), symbol, timeframe, period)
            return df
        except Exception as e:
            logger.debug("yfinance fetch failed for %s: %s", symbol, e)
            return None

    async def collect_all(self) -> Dict[str, Dict[str, pd.DataFrame]]:
        """Download all symbols × all timeframes."""
        result = {}
        total = len(self._symbols) * len(self._timeframes)
        done = 0

        for symbol in self._symbols:
            result[symbol] = {}
            for tf in self._timeframes:
                logger.info("Collecting %s %s (%d/%d)...", symbol, tf, done + 1, total)
                df = await self.collect_symbol_tf(symbol, tf)
                result[symbol][tf] = df
                done += 1

        return result
