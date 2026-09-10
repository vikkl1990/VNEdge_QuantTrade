"""
ML Training Orchestrator
========================
Coordinates the full training pipeline:
1. Collect candle data (90-180 days, all timeframes)
2. Run each scanner against historical data
3. Train ML probability models
4. Walk-forward validate
5. Generate calibrated probabilities
6. Serve results via dashboard API

Runs on VM2 (backtest server).
"""

import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ml_training.candle_collector import CandleCollector
from ml_training.feature_builder import build_features, build_labels, compute_indicators
from ml_training.scanner_backtester import ScannerBacktester, RESULTS_DIR
from ml_training.ml_model import MLProbabilityModel
from ml_training.backtest_tracker import BacktestTracker
from ml_training.candidate_trainer import CandidateTrainer

logger = logging.getLogger(__name__)

STORAGE_DIR = PROJECT_ROOT / "storage"
STATUS_FILE = STORAGE_DIR / "ml_training_status.json"

# Symbols to train on
TRAINING_SYMBOLS = [
    # Delta India available pairs (verified)
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "AVAX/USDT",
    "LINK/USDT", "DOGE/USDT",
    # NOT on Delta India: BONK, PEPE, SHIB, SUI, WIF
]

# Timeframes for comparison
COMPARISON_TIMEFRAMES = ["1m", "5m", "15m"]

# Scanner definitions (simplified versions for backtesting)
def _scan_structure_bounce(idx: int, df: pd.DataFrame, symbol: str) -> Optional[dict]:
    """Structure bounce scanner — matches live logic."""
    if idx < 50:
        return None
    row = df.iloc[idx]
    c, h, l, o = float(row["close"]), float(row["high"]), float(row["low"]), float(row["open"])
    atr = float(row.get("atr_14", 0))
    if atr <= 0:
        return None

    # Support/Resistance from recent swing highs/lows
    recent = df.iloc[max(0, idx-20):idx]
    swing_high = recent["high"].max()
    swing_low = recent["low"].min()

    # Long: price near support with rejection wick
    lower_wick = min(c, o) - l
    upper_wick = h - max(c, o)
    body = abs(c - o)
    candle_range = h - l
    if candle_range == 0:
        return None

    # Long setup: price near swing low, lower wick > 50% of range
    if abs(l - swing_low) < atr * 0.5 and lower_wick > candle_range * 0.4 and c > o:
        return {"side": "long", "entry_price": c, "confidence": 60, "grade": "B"}

    # Short setup: price near swing high, upper wick > 50% of range
    if abs(h - swing_high) < atr * 0.5 and upper_wick > candle_range * 0.4 and c < o:
        return {"side": "short", "entry_price": c, "confidence": 60, "grade": "B"}

    return None


def _scan_ema_momentum(idx: int, df: pd.DataFrame, symbol: str) -> Optional[dict]:
    """EMA momentum scanner."""
    if idx < 50:
        return None
    row = df.iloc[idx]
    prev = df.iloc[idx - 1]

    ema8 = float(row.get("ema_8", 0))
    ema21 = float(row.get("ema_21", 0))
    prev_ema8 = float(prev.get("ema_8", 0))
    prev_ema21 = float(prev.get("ema_21", 0))
    c = float(row["close"])
    vol = float(row.get("rel_vol", 1))

    if ema8 == 0 or ema21 == 0:
        return None

    # Bullish cross
    if prev_ema8 <= prev_ema21 and ema8 > ema21 and vol > 1.0:
        return {"side": "long", "entry_price": c, "confidence": 55, "grade": "B"}

    # Bearish cross
    if prev_ema8 >= prev_ema21 and ema8 < ema21 and vol > 1.0:
        return {"side": "short", "entry_price": c, "confidence": 55, "grade": "B"}

    return None


def _scan_bb_squeeze(idx: int, df: pd.DataFrame, symbol: str) -> Optional[dict]:
    """Bollinger Band squeeze scanner."""
    if idx < 50:
        return None
    row = df.iloc[idx]
    prev = df.iloc[idx - 1]

    bb_width = float(row.get("bb_width", 0))
    prev_bb_width = float(prev.get("bb_width", 0))
    c = float(row["close"])
    o = float(row["open"])
    vol = float(row.get("rel_vol", 1))
    macd_hist = float(row.get("macd_hist", 0))

    if bb_width == 0:
        return None

    # Check for squeeze (bb_width below 20th percentile of last 50 bars)
    recent_bbw = df["bb_width"].iloc[max(0, idx-50):idx]
    if len(recent_bbw) < 20:
        return None
    squeeze_threshold = recent_bbw.quantile(0.2)

    # Squeeze breakout: width was compressed, now expanding
    if prev_bb_width < squeeze_threshold and bb_width > squeeze_threshold:
        body = abs(c - o)
        if body > float(row.get("atr_14", 0)) * 0.5 and vol > 1.2:
            if c > o and macd_hist > 0:
                return {"side": "long", "entry_price": c, "confidence": 65, "grade": "B"}
            elif c < o and macd_hist < 0:
                return {"side": "short", "entry_price": c, "confidence": 65, "grade": "B"}

    return None


def _scan_vwap_mean_revert(idx: int, df: pd.DataFrame, symbol: str) -> Optional[dict]:
    """VWAP mean reversion scanner."""
    if idx < 50:
        return None
    row = df.iloc[idx]
    c = float(row["close"])
    vwap = float(row.get("vwap", 0))
    atr = float(row.get("atr_14", 0))
    rsi = float(row.get("rsi_14", 50))

    if vwap == 0 or atr == 0:
        return None

    dist = (c - vwap) / atr

    # Oversold: price far below VWAP + RSI low
    if dist < -1.5 and rsi < 35:
        return {"side": "long", "entry_price": c, "confidence": 55, "grade": "B"}

    # Overbought: price far above VWAP + RSI high
    if dist > 1.5 and rsi > 65:
        return {"side": "short", "entry_price": c, "confidence": 55, "grade": "B"}

    return None


def _scan_rsi_divergence(idx: int, df: pd.DataFrame, symbol: str) -> Optional[dict]:
    """RSI divergence scanner."""
    if idx < 30:
        return None
    row = df.iloc[idx]
    c = float(row["close"])
    rsi = float(row.get("rsi_14", 50))
    atr = float(row.get("atr_14", 0))

    if atr == 0:
        return None

    # Look back 10-20 bars for divergence
    lookback = df.iloc[max(0, idx-20):idx+1]
    if len(lookback) < 10:
        return None

    prices = lookback["close"].astype(float)
    rsis = lookback["rsi_14"].astype(float).dropna()
    if len(rsis) < 10:
        return None

    # Bullish divergence: price making lower lows, RSI making higher lows
    price_low_now = prices.iloc[-5:].min()
    price_low_prev = prices.iloc[:10].min()
    rsi_low_now = rsis.iloc[-5:].min()
    rsi_low_prev = rsis.iloc[:10].min()

    if price_low_now < price_low_prev and rsi_low_now > rsi_low_prev + 3 and rsi < 40:
        return {"side": "long", "entry_price": c, "confidence": 60, "grade": "B"}

    # Bearish divergence
    price_high_now = prices.iloc[-5:].max()
    price_high_prev = prices.iloc[:10].max()
    rsi_high_now = rsis.iloc[-5:].max()
    rsi_high_prev = rsis.iloc[:10].max()

    if price_high_now > price_high_prev and rsi_high_now < rsi_high_prev - 3 and rsi > 60:
        return {"side": "short", "entry_price": c, "confidence": 60, "grade": "B"}

    return None


def _scan_trend_continuation(idx: int, df: pd.DataFrame, symbol: str) -> Optional[dict]:
    """Trend continuation (pullback entry in established trend)."""
    if idx < 50:
        return None
    row = df.iloc[idx]
    c = float(row["close"])
    ema21 = float(row.get("ema_21", 0))
    ema50 = float(row.get("ema_50", 0))
    atr = float(row.get("atr_14", 0))
    rsi = float(row.get("rsi_14", 50))

    if ema21 == 0 or ema50 == 0 or atr == 0:
        return None

    # Uptrend: EMA21 > EMA50, price pulled back to EMA21
    if ema21 > ema50 and abs(c - ema21) < atr * 0.5 and c > ema50:
        if rsi > 40 and rsi < 65:  # not overbought
            return {"side": "long", "entry_price": c, "confidence": 60, "grade": "B"}

    # Downtrend: EMA21 < EMA50, price rallied to EMA21
    if ema21 < ema50 and abs(c - ema21) < atr * 0.5 and c < ema50:
        if rsi < 60 and rsi > 35:
            return {"side": "short", "entry_price": c, "confidence": 60, "grade": "B"}

    return None


def _scan_liquidity_sweep(idx: int, df: pd.DataFrame, symbol: str) -> Optional[dict]:
    """Liquidity sweep scanner — price raids equal highs/lows then reclaims."""
    if idx < 30:
        return None
    row = df.iloc[idx]
    c, h, l, o = float(row["close"]), float(row["high"]), float(row["low"]), float(row["open"])
    atr = float(row.get("atr_14", 0))
    if atr <= 0:
        return None

    lookback = min(50, idx)
    window = df.iloc[max(0, idx - lookback):idx]
    highs = window["high"].values
    lows = window["low"].values
    tolerance = atr * 0.15

    # Find equal highs cluster (2+ touches)
    eq_high_level = 0.0
    for i in range(len(highs) - 1, max(-1, len(highs) - 30), -1):
        touches = sum(1 for j in range(len(highs)) if j != i and abs(highs[j] - highs[i]) < tolerance)
        if touches >= 2:
            eq_high_level = highs[i]
            break

    # Find equal lows cluster
    eq_low_level = 0.0
    for i in range(len(lows) - 1, max(-1, len(lows) - 30), -1):
        touches = sum(1 for j in range(len(lows)) if j != i and abs(lows[j] - lows[i]) < tolerance)
        if touches >= 2:
            eq_low_level = lows[i]
            break

    # Bullish sweep: price dips below equal lows, closes back above
    if eq_low_level > 0 and l < eq_low_level and c > eq_low_level:
        body = abs(c - o)
        displacement = body / atr if atr > 0 else 0
        if displacement > 0.3 and c > o:  # bullish close with real body
            return {"side": "long", "entry_price": c, "confidence": 65, "grade": "B"}

    # Bearish sweep: price spikes above equal highs, closes back below
    if eq_high_level > 0 and h > eq_high_level and c < eq_high_level:
        body = abs(c - o)
        displacement = body / atr if atr > 0 else 0
        if displacement > 0.3 and c < o:  # bearish close with real body
            return {"side": "short", "entry_price": c, "confidence": 65, "grade": "B"}

    return None


def _scan_bos_choch(idx: int, df: pd.DataFrame, symbol: str) -> Optional[dict]:
    """BOS/CHOCH scanner — break of structure with displacement."""
    if idx < 30:
        return None
    row = df.iloc[idx]
    prev = df.iloc[idx - 1]
    c, h, l, o = float(row["close"]), float(row["high"]), float(row["low"]), float(row["open"])
    prev_c = float(prev["close"])
    atr = float(row.get("atr_14", 0))
    if atr <= 0:
        return None

    lookback = min(30, idx - 5)
    window = df.iloc[max(0, idx - lookback):idx]
    highs_w = window["high"].values
    lows_w = window["low"].values

    # Find swing high (local max in 5-bar window)
    swing_high = 0.0
    for i in range(2, len(highs_w) - 2):
        if highs_w[i] >= max(highs_w[max(0, i-2):i]) and highs_w[i] >= max(highs_w[i+1:min(len(highs_w), i+3)]):
            if highs_w[i] > swing_high:
                swing_high = highs_w[i]

    # Find swing low
    swing_low = float('inf')
    for i in range(2, len(lows_w) - 2):
        if lows_w[i] <= min(lows_w[max(0, i-2):i]) and lows_w[i] <= min(lows_w[i+1:min(len(lows_w), i+3)]):
            if lows_w[i] < swing_low:
                swing_low = lows_w[i]

    body = abs(c - o)
    displacement = body / atr if atr > 0 else 0

    # Bullish BOS: close breaks above swing high with displacement
    if swing_high > 0 and c > swing_high and prev_c <= swing_high and displacement > 0.5:
        body_ratio = body / (h - l) if h > l else 0
        if body_ratio > 0.4:  # clean break candle
            return {"side": "long", "entry_price": c, "confidence": 60, "grade": "B"}

    # Bearish BOS: close breaks below swing low with displacement
    if swing_low < float('inf') and c < swing_low and prev_c >= swing_low and displacement > 0.5:
        body_ratio = body / (h - l) if h > l else 0
        if body_ratio > 0.4:
            return {"side": "short", "entry_price": c, "confidence": 60, "grade": "B"}

    return None


SCANNERS = {
    "structure_bounce": _scan_structure_bounce,
    "ema_momentum": _scan_ema_momentum,
    "vwap_mean_revert": _scan_vwap_mean_revert,
    "rsi_divergence": _scan_rsi_divergence,
    "trend_continuation": _scan_trend_continuation,
    "liquidity_sweep": _scan_liquidity_sweep,
    "bos_choch": _scan_bos_choch,
    # DROPPED: bb_squeeze (51% WR, -$16 EV, negative expectancy)
}


class TrainingOrchestrator:
    """Coordinates the full ML training pipeline."""

    def __init__(self, exchange_client, config: dict):
        self._exchange = exchange_client
        self._config = config
        self._collector = CandleCollector(exchange_client, TRAINING_SYMBOLS)
        self._backtester = ScannerBacktester(config)
        self._models: Dict[str, MLProbabilityModel] = {}
        self._status: Dict = {
            "phase": "idle",
            "started_at": None,
            "progress": {},
            "results": {},
        }
        self._candle_data: Dict[str, Dict[str, pd.DataFrame]] = {}
        self._tracker = BacktestTracker()

    def get_status(self) -> Dict:
        return {
            **self._status,
            "collector_progress": self._collector.get_progress(),
            "backtester_progress": self._backtester.get_progress(),
            "models": {k: m.get_status() for k, m in self._models.items()},
        }

    async def run_full_pipeline(self, symbols: Optional[List[str]] = None,
                                 timeframes: Optional[List[str]] = None):
        """Run the complete training pipeline."""
        symbols = symbols or TRAINING_SYMBOLS  # all 11 symbols
        timeframes = timeframes or COMPARISON_TIMEFRAMES

        self._status["phase"] = "collecting"
        self._status["started_at"] = datetime.now(timezone.utc).isoformat()
        self._save_status()

        try:
            import gc

            # Phase 1+2+3: Process one symbol at a time to save memory
            logger.info("=== PHASE 1+2+3: Collect → Backtest → Train (per symbol) ===")
            all_results = {}

            for sym_idx, symbol in enumerate(symbols):
                logger.info("--- Processing %s (%d/%d) ---", symbol, sym_idx + 1, len(symbols))

                # Collect candles for this symbol only
                self._status["phase"] = "collecting"
                self._status["progress"] = {"symbol": symbol, "step": f"{sym_idx+1}/{len(symbols)}"}
                self._save_status()

                collector = CandleCollector(self._exchange, [symbol], timeframes)
                sym_data = await collector.collect_all()
                del collector

                self._status["phase"] = "backtesting"
                self._save_status()

                # Extract per-TF data and release sym_data early
                tf_frames = {}
                for tf in timeframes:
                    raw = sym_data.get(symbol, {}).get(tf)
                    if raw is not None and not raw.empty:
                        tf_frames[tf] = raw
                del sym_data
                gc.collect()

                for tf in timeframes:
                    df = tf_frames.get(tf)
                    if df is None:
                        continue

                    # Cap candle data to prevent OOM on 1GB VM
                    # 1m: 30k rows (~20 days), 5m: 15k, 15m: full
                    max_rows = {"1m": 30000, "5m": 15000}.get(tf, 50000)
                    if len(df) > max_rows:
                        logger.info("Capping %s %s from %d to %d rows", symbol, tf, len(df), max_rows)
                        df = df.iloc[-max_rows:].copy()

                    df = compute_indicators(df)

                    # Phase 2: Backtest all scanners on this symbol×tf
                    for scanner_name, scanner_func in SCANNERS.items():
                        key = f"{scanner_name}_{symbol}_{tf}"
                        logger.info("Backtesting %s on %s %s...", scanner_name, symbol, tf)

                        await asyncio.sleep(0)

                        trades = self._backtester.backtest_scanner(
                            scanner_func, scanner_name, df, symbol
                        )

                        if not trades:
                            continue

                        metrics = self._backtester._calc_metrics(pd.DataFrame(trades))
                        wf = self._backtester.walk_forward_validate(trades)
                        probs = self._backtester.compute_calibrated_probabilities(trades)

                        all_results[key] = {
                            "scanner": scanner_name,
                            "symbol": symbol,
                            "timeframe": tf,
                            "metrics": metrics,
                            "walk_forward": wf,
                            "calibrated_probabilities": probs,
                            "trade_count": len(trades),
                        }

                        self._backtester.save_results(scanner_name, f"{symbol}_{tf}", trades, metrics)

                        logger.info(
                            "  %s %s %s: %d trades, WR=%.1f%%, Exp=%.3fR, WF=%s",
                            scanner_name, symbol, tf,
                            metrics.get("trades", 0),
                            metrics.get("win_rate", 0),
                            metrics.get("expectancy_r", 0),
                            wf.get("verdict", "?"),
                        )

                    # Phase 3: Skip per-TF ML training here.
                    # ML training is now done in Phase 5 as candidate models
                    # on 5m data (most stable timeframe for candle-only features).
                    # 1m is too noisy for ML, 15m is used for context only.

                    # Free this timeframe's data and gc between TFs
                    del df
                    gc.collect()

                # Save backtest results incrementally
                self._status["results"]["backtest"] = all_results
                self._save_status()

                # Release this symbol's candle data to free memory
                del tf_frames
                gc.collect()
                logger.info("Released memory for %s. GC done.", symbol)

            # Phase 4: Generate comparison report
            logger.info("=== PHASE 4: Generating comparison report ===")
            comparison = self._generate_comparison(all_results)
            self._status["results"]["comparison"] = comparison
            self._save_status()

            # Phase 5: Candidate training on 5m data (most stable for candle-only ML)
            # Phase 4.4 (2026-04-11): switched from realized_r (regression on continuous R)
            # to MFE binary labels. Rationale:
            #   - realized_r couples ML tightly to simulator exit logic (hard to change exits
            #     without retraining; model "learns" the simulator's bugs)
            #   - MFE (Max Favorable Excursion) labels are SL-agnostic: they just ask
            #     "did price move ≥ threshold_r within lookahead bars?" — pure candle math
            #   - With Phase 4.3 label-aware purge, the lookahead directly sets the purge gap
            logger.info("=== PHASE 5: Candidate training on 5m (MFE labels, all scanners) ===")
            self._status["phase"] = "candidate_training"
            self._save_status()

            # Phase 5.0a — load BTC once, share across all symbols in Phase 5 + 6
            # BTC is the cross-asset anchor: every alt's ML score benefits from
            # knowing what BTC is doing right now. We load it here so a single
            # collector call serves both phases and we don't hammer the exchange.
            logger.info("Phase 5.0a: loading BTC/USDT 5m for cross-asset features")
            try:
                btc_collector = CandleCollector(self._exchange, ["BTC/USDT"], ["5m"])
                btc_data = await btc_collector.collect_all()
                btc_df_5m = btc_data.get("BTC/USDT", {}).get("5m")
                del btc_data, btc_collector
                if btc_df_5m is not None and len(btc_df_5m) > 15000:
                    btc_df_5m = btc_df_5m.iloc[-15000:]
                logger.info("  BTC 5m: %d bars loaded", len(btc_df_5m) if btc_df_5m is not None else 0)
            except Exception as e:
                logger.warning("Phase 5.0a: failed to load BTC context: %s", e)
                btc_df_5m = None

            candidate_results = {}
            for symbol in symbols:
                # Phase 4.1a: load 5m + 15m + 1h + 4h so ML can learn HTF context
                logger.info("Loading 5m/15m/1h/4h data for candidate training: %s", symbol)
                collector = CandleCollector(self._exchange, [symbol], ["5m", "15m", "1h", "4h"])
                sym_data = await collector.collect_all()
                df = sym_data.get(symbol, {}).get("5m")
                htf_df = sym_data.get(symbol, {}).get("15m")
                htf_1h_df = sym_data.get(symbol, {}).get("1h")
                htf_4h_df = sym_data.get(symbol, {}).get("4h")
                del sym_data, collector

                if df is None or len(df) < 500:
                    logger.warning("Skipping candidate training for %s: insufficient data", symbol)
                    gc.collect()
                    continue

                # Cap at 15k rows to prevent OOM on 1GB VM
                if len(df) > 15000:
                    df = df.iloc[-15000:]
                    logger.info("Capped to 15k rows for %s", symbol)

                # Log HTF coverage for visibility
                logger.info(
                    "  HTF data: 15m=%d, 1h=%d, 4h=%d bars",
                    len(htf_df) if htf_df is not None else 0,
                    len(htf_1h_df) if htf_1h_df is not None else 0,
                    len(htf_4h_df) if htf_4h_df is not None else 0,
                )

                logger.info("Running candidate trainer for %s (all scanners, MFE binary labels)...", symbol)
                await asyncio.sleep(0)
                ct = CandidateTrainer()
                # Phase 4.4: MFE binary labels
                #   threshold_r=0.3 — price must move at least 0.3R in our direction
                #   max_bars=20    — within 100 minutes on 5m (scalp + fast intraday window)
                #                    Phase 4.3 purge gap is auto-derived from max_bars.
                result = ct.run_all_scanners(
                    df, symbol, SCANNERS,
                    n_splits=5, n_estimators=50, max_depth=6,
                    # 2026-09-10: label = "did the live exit engine book a
                    # profit" (LiveTrackerRunner replay), not "did price
                    # touch 0.3R within 100 min". The MFE label predicted
                    # something the bot never let happen (median hold 1.2 min).
                    label_mode="won",
                    mfe_threshold_r=0.3,
                    mfe_max_bars=20,
                    htf_df=htf_df,
                    htf_1h_df=htf_1h_df,
                    htf_4h_df=htf_4h_df,
                    btc_df=btc_df_5m,  # Phase 5.0a
                    save_models=False,  # family models are the served ones
                )
                candidate_results[symbol] = result

                # Free memory
                del df, htf_df, htf_1h_df, htf_4h_df, ct
                gc.collect()

            self._status["results"]["candidate_training"] = candidate_results
            self._save_status()

            # Phase 6: Pair-family training (shared model per family, not per symbol)
            # Groups: liquid_majors (BTC/ETH/SOL), secondary (AVAX/LINK), high_beta (DOGE+)
            logger.info("=== PHASE 6: Pair-family training (shared models per family) ===")
            self._status["phase"] = "pair_family_training"
            self._save_status()

            try:
                # Collect all symbol data for family training
                # Phase 4.1a: also load 1h + 4h for multi-TF feature fusion
                symbol_data_all = {}
                htf_data_all = {}       # 15m (legacy)
                htf_1h_data_all = {}    # Phase 4.1a
                htf_4h_data_all = {}    # Phase 4.1a
                for symbol in symbols:
                    logger.info("Loading 5m/15m/1h/4h data for family training: %s", symbol)
                    collector = CandleCollector(self._exchange, [symbol], ["5m", "15m", "1h", "4h"])
                    sym_data = await collector.collect_all()
                    df_sym = sym_data.get(symbol, {}).get("5m")
                    htf_sym = sym_data.get(symbol, {}).get("15m")
                    h1_sym = sym_data.get(symbol, {}).get("1h")
                    h4_sym = sym_data.get(symbol, {}).get("4h")
                    del sym_data, collector

                    if df_sym is not None and len(df_sym) >= 500:
                        if len(df_sym) > 15000:
                            df_sym = df_sym.iloc[-15000:]
                        symbol_data_all[symbol] = df_sym
                        if htf_sym is not None:
                            htf_data_all[symbol] = htf_sym
                        if h1_sym is not None:
                            htf_1h_data_all[symbol] = h1_sym
                        if h4_sym is not None:
                            htf_4h_data_all[symbol] = h4_sym
                    await asyncio.sleep(0)

                if len(symbol_data_all) >= 2:
                    logger.info(
                        "Phase 4.1a HTF coverage: 15m=%d, 1h=%d, 4h=%d symbols",
                        len(htf_data_all), len(htf_1h_data_all), len(htf_4h_data_all),
                    )
                    ct_family = CandidateTrainer()
                    # Phase 4.4: family models also on MFE binary labels (same thresholds)
                    # Phase 5.0a: shared BTC context for all family symbols
                    family_results = ct_family.run_pair_family(
                        symbol_data_all, SCANNERS,
                        n_splits=5, n_estimators=50, max_depth=6,
                        label_mode="won",  # live-exit replay label (see per-symbol note)
                        mfe_threshold_r=0.3,
                        mfe_max_bars=20,
                        htf_data=htf_data_all,
                        htf_1h_data=htf_1h_data_all,
                        htf_4h_data=htf_4h_data_all,
                        btc_df=btc_df_5m,  # Phase 5.0a
                    )
                    self._status["results"]["pair_family_training"] = family_results
                    del ct_family
                else:
                    logger.warning("Skipping pair-family training: only %d symbols with data",
                                   len(symbol_data_all))
                del symbol_data_all, htf_data_all, htf_1h_data_all, htf_4h_data_all
                gc.collect()
            except Exception as e:
                logger.error("Pair-family training failed: %s", e, exc_info=True)
                self._status["results"]["pair_family_training"] = {"error": str(e)}

            self._status["phase"] = "complete"
            self._status["completed_at"] = datetime.now(timezone.utc).isoformat()
            self._save_status()

            # Record run in tracker for comparison
            # Phase 4.6 (2026-04-16): fix stale run_config. Was recording
            # label_mode="realized_r" and mfe_threshold_r=0.8 / mfe_max_bars=15
            # while the actual training above uses MFE labels with
            # threshold_r=0.3 and max_bars=20. Audit trail was misleading.
            self._tracker.record_run(
                run_config={"symbols": symbols, "timeframes": timeframes,
                            "label_mode": "won", "mfe_threshold_r": 0.3,
                            "mfe_max_bars": 20, "candidate_tf": "5m"},
                scanner_results=all_results,
                ml_results=candidate_results,
                label=f"{'_'.join(symbols)}_5m_mfe_candidates",
            )

            logger.info("=== TRAINING PIPELINE COMPLETE ===")
            return self._status

        except Exception as e:
            logger.exception("Training pipeline error: %s", e)
            self._status["phase"] = "error"
            self._status["error"] = str(e)
            self._save_status()
            raise

    def _generate_comparison(self, results: Dict) -> Dict:
        """Generate a comparison report across timeframes and scanners."""
        comparison = {
            "by_scanner": {},
            "by_timeframe": {},
            "by_symbol": {},
            "best_setups": [],
            "worst_setups": [],
        }

        for key, data in results.items():
            scanner = data["scanner"]
            symbol = data["symbol"]
            tf = data["timeframe"]
            metrics = data["metrics"]
            wf = data.get("walk_forward", {})

            # By scanner
            comparison["by_scanner"].setdefault(scanner, []).append({
                "symbol": symbol, "tf": tf,
                **metrics,
                "wf_verdict": wf.get("verdict", "?"),
                "wf_edge_pct": wf.get("edge_holds_pct", 0),
            })

            # By timeframe
            comparison["by_timeframe"].setdefault(tf, []).append({
                "scanner": scanner, "symbol": symbol,
                **metrics,
                "wf_verdict": wf.get("verdict", "?"),
            })

            # By symbol
            comparison["by_symbol"].setdefault(symbol, []).append({
                "scanner": scanner, "tf": tf,
                **metrics,
                "wf_verdict": wf.get("verdict", "?"),
            })

        # Find best and worst setups
        all_setups = []
        for key, data in results.items():
            m = data["metrics"]
            wf = data.get("walk_forward", {})
            all_setups.append({
                "key": key,
                "scanner": data["scanner"],
                "symbol": data["symbol"],
                "tf": data["timeframe"],
                "expectancy_r": m.get("expectancy_r", -999),
                "win_rate": m.get("win_rate", 0),
                "trades": m.get("trades", 0),
                "wf_verdict": wf.get("verdict", "?"),
                "wf_edge_pct": wf.get("edge_holds_pct", 0),
            })

        all_setups.sort(key=lambda x: x["expectancy_r"], reverse=True)
        comparison["best_setups"] = all_setups[:10]
        comparison["worst_setups"] = all_setups[-10:]

        return comparison

    def _save_status(self):
        """Save training status to disk."""
        try:
            STATUS_FILE.write_text(json.dumps(self._status, default=str, indent=2))
        except Exception as e:
            logger.warning("Failed to save status: %s", e)
