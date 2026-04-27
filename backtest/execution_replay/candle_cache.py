"""
Candle cache loader for the execution-replay backtest engine.

Provides 1-minute candle windows for trade-replay simulation. Local parquet
cache (~47MB at /home/opc/crypto-trading-bot/storage/candle_cache/) covers
~13 priority symbols with hundreds of thousands of rows each. For symbols
NOT in cache, falls through to Delta India REST history endpoint.

API
---
    load_candles_from_cache(symbol, resolution="1m") -> Optional[DataFrame]
        Local parquet load. Returns None on cache miss.

    fetch_candles_rest(symbol, start_unix, end_unix) -> Optional[DataFrame]
        Delta India REST fetch. Caches result on success.

    get_candles_window(symbol, entry_unix_ts, max_window_sec=3600)
        High-level: returns 1m candles from entry_ts to entry_ts + max_window.
        Cache-first; REST fallback. Returns None on no data.

Schema
------
Returned DataFrame columns:
    timestamp  (int64, ms since epoch)
    open       (float64)
    high       (float64)
    low        (float64)
    close      (float64)
    volume     (float64)

Author: Backtest Engineer (Agent 13) + architect
Implements: gap (a) candle-cache helper for exit-logic replay.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger("backtest.candle_cache")

CACHE_DIR = Path("/home/opc/crypto-trading-bot/storage/candle_cache")


def _normalize_symbol(symbol: str) -> str:
    """Convert 'BTC/USDT' -> 'BTC_USDT' for filename."""
    return symbol.replace("/", "_")


def load_candles_from_cache(symbol: str, resolution: str = "1m") -> Optional[pd.DataFrame]:
    """Load OHLCV candles from local parquet cache.

    Returns None if cache miss. Cache miss is normal for tail symbols
    (PEPE/USDT, MEME/USDT, etc.); the REST fallback handles those.
    """
    if not symbol or not CACHE_DIR.exists():
        return None
    path = CACHE_DIR / f"{_normalize_symbol(symbol)}_{resolution}.parquet"
    if not path.exists():
        return None
    # 2026-04-27 corruption guard — fetch_candles_rest used to overwrite
    # cache files on every REST call, which destroyed our hand-curated
    # multi-month 1m parquet (147k rows → 60 rows). Files under 50KB
    # are almost certainly the corrupted single-window snapshots; treat
    # them as misses so REST fallback kicks in. Real cache files for
    # 1m × 3 months are 5-7MB; 5m/15m/1h files are 200KB+.
    try:
        if path.stat().st_size < 50_000:
            return None
    except Exception:
        pass
    try:
        df = pd.read_parquet(path)
        # Cache schema may use a 'datetime' index — flatten if so.
        if df.index.name == "datetime":
            df = df.reset_index(drop=False)
        # Required columns present?
        required = {"timestamp", "open", "high", "low", "close"}
        if not required.issubset(set(df.columns)):
            logger.warning(
                "candle cache missing required columns for %s: have %s",
                symbol, list(df.columns),
            )
            return None
        return df
    except Exception as e:
        logger.warning("candle cache load failed for %s: %s", symbol, e)
        return None


def fetch_candles_rest(
    symbol: str,
    start_unix: int,
    end_unix: int,
    resolution: str = "1m",
) -> Optional[pd.DataFrame]:
    """Fetch candles from Delta India REST and cache to parquet on success.

    Used when local cache misses (long-tail symbols). Caches result for
    future runs to avoid repeated REST hits.
    """
    try:
        from delta_rest_client import DeltaRestClient
        # PRODUCT_MAP: "SOL/USDT" -> "SOLUSDT"
        from exchange.delta_client import PRODUCT_MAP

        info = PRODUCT_MAP.get(symbol)
        if not info:
            return None
        delta_sym = info.get("symbol", symbol.replace("/", ""))

        client = DeltaRestClient(
            base_url="https://api.india.delta.exchange",
            api_key="",
            api_secret="",
        )
        r = client.request("GET", "/v2/history/candles", query={
            "symbol":     delta_sym,
            "resolution": resolution,
            "start":      int(start_unix),
            "end":        int(end_unix),
        })
        result = r.json().get("result", []) if hasattr(r, "json") else r.get("result", [])
        if not result:
            return None

        rows = []
        for c in result:
            try:
                rows.append({
                    "timestamp": int(c.get("time", 0) or c.get("t", 0)) * 1000,
                    "open":      float(c.get("open", 0)),
                    "high":      float(c.get("high", 0)),
                    "low":       float(c.get("low", 0)),
                    "close":     float(c.get("close", 0)),
                    "volume":    float(c.get("volume", 0)),
                })
            except (TypeError, ValueError):
                continue
        if not rows:
            return None
        df = pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)

        # 2026-04-27 — DISABLED cache write on REST fetch. Previously
        # this overwrote multi-month curated parquet (147k rows) with
        # the small per-signal window (~60 rows) on every REST call,
        # destroying the historical cache. To re-enable safely, the
        # write must MERGE into an existing cache (read+concat+dedupe
        # by timestamp+sort+write_atomic), not overwrite. Until that
        # refactor, REST results are returned in-memory only.
        # See storage/candle_cache/*_1m.parquet for damage evidence.
        return df

    except Exception as e:
        logger.warning("REST candle fetch failed for %s: %s", symbol, e)
        return None


def get_candles_window(
    symbol: str,
    entry_unix_ts: int,
    max_window_sec: int = 3600,
    resolution: str = "1m",
) -> Optional[pd.DataFrame]:
    """Get candles in the window [entry_unix_ts, entry_unix_ts + max_window_sec].

    Cache-first; REST fallback if cache miss OR cache doesn't cover window.
    Returns None if neither source has data.

    `entry_unix_ts` is in seconds (not milliseconds).
    """
    if not symbol or entry_unix_ts <= 0:
        return None

    end_unix = entry_unix_ts + max_window_sec
    entry_ms = entry_unix_ts * 1000
    end_ms = end_unix * 1000

    # Try local cache
    df = load_candles_from_cache(symbol, resolution)
    if df is not None:
        # Filter to window
        window = df[(df["timestamp"] >= entry_ms) & (df["timestamp"] <= end_ms)].copy()
        if len(window) >= 2:
            window = window.reset_index(drop=True)
            return window
        # Cache exists but window not covered — fall through to REST

    # REST fallback
    return fetch_candles_rest(symbol, entry_unix_ts, end_unix, resolution)


__all__ = [
    "load_candles_from_cache",
    "fetch_candles_rest",
    "get_candles_window",
    "CACHE_DIR",
]
