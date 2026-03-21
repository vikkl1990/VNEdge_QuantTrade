"""
Latency Arbitrage Strategy -- Delta India vs Binance
=====================================================
Binance is the price leader for BTC/ETH. Delta India follows with a lag.
When Binance moves significantly and Delta hasn't caught up, we trade on Delta
in the direction of the move, then close when Delta converges.

Edge: Binance price leads Delta by 200ms-2s on volatile moves.
Cost: ~0.14% round-trip (0.05% taker + 0.00% close + 0.06% settlement + ~0.03% slippage)
Minimum dislocation to trade: 0.20% (provides 0.06% net edge minimum)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Coroutine, Dict, List, Optional, Any

import aiohttp
import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Symbol mapping (matches exchange/delta_ws.py conventions)
# ---------------------------------------------------------------------------

# Delta India WS uses these symbol names on the v2/ticker channel
DELTA_SYMBOL_MAP = {
    "BTC/USDT": "BTCUSD",
    "ETH/USDT": "ETHUSD",
    "SOL/USDT": "SOLUSD",
    "AVAX/USDT": "AVAXUSD",
    "DOGE/USDT": "DOGEUSD",
}

DELTA_REVERSE_MAP = {v: k for k, v in DELTA_SYMBOL_MAP.items()}

# Binance futures bookTicker uses these lowercase symbol names
BINANCE_SYMBOL_MAP = {
    "BTC/USDT": "btcusdt",
    "ETH/USDT": "ethusdt",
    "SOL/USDT": "solusdt",
    "AVAX/USDT": "avaxusdt",
    "DOGE/USDT": "dogeusdt",
}

BINANCE_REVERSE_MAP = {v.upper(): k for k, v in BINANCE_SYMBOL_MAP.items()}


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class PriceSnapshot:
    """Point-in-time price from an exchange."""
    exchange: str          # "binance" or "delta"
    symbol: str            # canonical e.g. "BTC/USDT"
    bid: float
    ask: float
    mid: float             # (bid + ask) / 2
    timestamp: float       # exchange-reported unix seconds (best effort)
    local_recv_ts: float   # time.time() when we received it locally


@dataclass
class Dislocation:
    """Measured price dislocation between two exchanges."""
    symbol: str
    binance_mid: float
    delta_mid: float
    dislocation_pct: float   # (binance_mid - delta_mid) / delta_mid * 100
    dislocation_abs: float   # absolute USD difference
    binance_ts: float
    delta_ts: float
    latency_ms: float        # |local_recv_ts difference| in ms
    direction: str           # "long" if binance > delta, "short" otherwise
    timestamp: float


@dataclass
class LatencyArbSignal:
    """Signal to trade on Delta based on Binance price leadership."""
    symbol: str
    side: str                    # "long" or "short"
    delta_entry_price: float     # current Delta mid (our entry)
    binance_target: float        # where Binance is (Delta should converge here)
    dislocation_pct: float
    expected_profit_pct: float   # dislocation - estimated costs
    confidence: int              # 0-100
    timestamp: float


# ---------------------------------------------------------------------------
# Analysis layer dataclasses
# ---------------------------------------------------------------------------

@dataclass
class DislocationSpike:
    """Tracks a dislocation event from start to resolution."""
    symbol: str
    direction: str
    peak_disl_pct: float
    start_time: float
    start_binance_mid: float
    start_delta_mid: float
    # Updated as spike progresses
    end_time: Optional[float] = None
    duration_s: float = 0.0
    # Survival: was dislocation still > threshold at 1s, 2s, 5s, 10s?
    survived_1s: bool = False
    survived_2s: bool = False
    survived_5s: bool = False
    survived_10s: bool = False
    # Resolution: who moved?
    end_binance_mid: float = 0.0
    end_delta_mid: float = 0.0
    delta_converged: bool = False   # True = Delta caught up to Binance
    binance_reverted: bool = False  # True = Binance reverted back
    convergence_pct: float = 0.0    # how much of the gap closed
    # Net edge classification at peak
    peak_classification: str = "UNKNOWN"


@dataclass
class SimulatedTrade:
    """Simulated trade result from a resolved spike."""
    symbol: str
    side: str
    entry_price: float   # delta ask+slippage (long) or bid-slippage (short)
    exit_price: float    # delta mid at resolution
    gross_pnl_pct: float
    fees_pct: float
    net_pnl_pct: float
    hold_time_s: float
    was_profitable: bool
    timestamp: float


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class LatencyArbEngine:
    """
    Measures price dislocation between Binance and Delta India.
    Generates signals when dislocation exceeds cost threshold.

    Binance feed: bookTicker (fastest public feed, ~10ms top-of-book updates)
    Delta feed : v2/ticker  (matches existing delta_ws.py channel)
    """

    # WebSocket endpoints
    BINANCE_WS_URL = "wss://fstream.binance.com/ws"
    DELTA_WS_URL = "wss://socket.india.delta.exchange"

    # Thresholds
    MIN_DISLOCATION_PCT = 0.20    # minimum % to consider a trade
    STRONG_DISLOCATION_PCT = 0.35 # high-confidence level
    MAX_DISLOCATION_PCT = 1.5     # too large = probably bad data
    MIN_CONFIDENCE = 60
    CONVERGENCE_TIMEOUT_S = 30

    # ------------------------------------------------------------------
    # Layer 1: Per-pair execution thresholds
    # Majors need > 0.15% net edge, alts need > 0.20%
    # ------------------------------------------------------------------
    PAIR_THRESHOLDS = {
        "BTC/USDT":  {"min_disl": 0.15, "min_net_edge": 0.15, "max_spread": 0.03, "max_lag_ms": 5000, "taker_fee": 0.0005, "settlement_fee": 0.0006, "slippage_est": 0.0003},
        "ETH/USDT":  {"min_disl": 0.15, "min_net_edge": 0.15, "max_spread": 0.03, "max_lag_ms": 5000, "taker_fee": 0.0005, "settlement_fee": 0.0006, "slippage_est": 0.0003},
        "SOL/USDT":  {"min_disl": 0.20, "min_net_edge": 0.20, "max_spread": 0.05, "max_lag_ms": 5000, "taker_fee": 0.0005, "settlement_fee": 0.0006, "slippage_est": 0.0005},
        "AVAX/USDT": {"min_disl": 0.20, "min_net_edge": 0.20, "max_spread": 0.08, "max_lag_ms": 5000, "taker_fee": 0.0005, "settlement_fee": 0.0006, "slippage_est": 0.0005},
        "DOGE/USDT": {"min_disl": 0.25, "min_net_edge": 0.20, "max_spread": 0.05, "max_lag_ms": 5000, "taker_fee": 0.0005, "settlement_fee": 0.0006, "slippage_est": 0.0008},
    }
    DEFAULT_THRESHOLD = {"min_disl": 0.20, "min_net_edge": 0.20, "max_spread": 0.05, "max_lag_ms": 5000, "taker_fee": 0.0005, "settlement_fee": 0.0006, "slippage_est": 0.0005}

    # Freshness thresholds per exchange
    BINANCE_FRESHNESS_S = 2.0
    DELTA_FRESHNESS_S = 10.0   # Delta has ~4.5s lag, use generous window

    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        on_signal: Optional[Callable[..., Coroutine[Any, Any, None]]] = None,
    ):
        self.symbols = symbols or ["BTC/USDT", "ETH/USDT"]
        self.on_signal = on_signal

        # Latest prices from each exchange
        self._binance_prices: Dict[str, PriceSnapshot] = {}
        self._delta_prices: Dict[str, PriceSnapshot] = {}

        # Dislocation history for analysis (per symbol)
        self._dislocation_history: Dict[str, deque] = {
            s: deque(maxlen=5000) for s in self.symbols
        }

        # Statistics
        self._stats: Dict[str, Any] = {
            "binance_msgs": 0,
            "delta_msgs": 0,
            "dislocations_detected": 0,
            "signals_generated": 0,
            "avg_dislocation_pct": {},
            "max_dislocation_pct": {},
            "avg_latency_ms": {},
            "started_at": None,
        }

        # Measurement mode (no trading, just measuring)
        self._measure_only = True
        self._running = False

        # Cooldown per symbol (prevent rapid-fire signals)
        self._last_signal_time: Dict[str, float] = {}
        self._signal_cooldown_s = 10  # min seconds between signals per symbol

        # aiohttp session (created in start)
        self._session: Optional[aiohttp.ClientSession] = None

        # ----------------------------------------------------------
        # Layer 2: Decay tracking
        # When a dislocation spike starts, track how long it survives
        # ----------------------------------------------------------
        self._active_spikes: Dict[str, Optional[DislocationSpike]] = {s: None for s in self.symbols}
        self._spike_history: Dict[str, deque] = {s: deque(maxlen=500) for s in self.symbols}

        # ----------------------------------------------------------
        # Layer 4: Edge simulation -- simulated P&L replay
        # ----------------------------------------------------------
        self._simulated_trades: Dict[str, deque] = {s: deque(maxlen=200) for s in self.symbols}

        # ----------------------------------------------------------
        # Layer 5: Session/event segmentation
        # ----------------------------------------------------------
        # Dislocations bucketed by UTC hour
        self._hourly_stats: Dict[str, Dict[int, list]] = {
            s: {h: [] for h in range(24)} for s in self.symbols
        }
        # Volatility regime: rolling Binance mid prices (1 per second approx)
        self._vol_window: Dict[str, deque] = {s: deque(maxlen=60) for s in self.symbols}
        self._last_vol_sample_ts: Dict[str, float] = {s: 0.0 for s in self.symbols}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, measure_only: bool = True):
        """Start both WebSocket connections and begin measuring."""
        self._measure_only = measure_only
        self._running = True
        self._stats["started_at"] = time.time()
        self._session = aiohttp.ClientSession()

        logger.info(
            "LatencyArb starting -- symbols=%s, measure_only=%s",
            self.symbols, measure_only,
        )

        # Run both WS connections + dislocation checker concurrently
        await asyncio.gather(
            self._binance_ws_loop(),
            self._delta_ws_loop(),
            self._dislocation_checker(),
        )

    async def stop(self):
        """Signal all loops to stop and close the session."""
        self._running = False
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Binance WebSocket (bookTicker -- fastest public feed)
    # ------------------------------------------------------------------

    async def _binance_ws_loop(self):
        """Subscribe to Binance futures bookTicker for all symbols."""
        streams = "/".join(
            f"{BINANCE_SYMBOL_MAP[s]}@bookTicker"
            for s in self.symbols
            if s in BINANCE_SYMBOL_MAP
        )
        url = f"{self.BINANCE_WS_URL}/{streams}"

        while self._running:
            try:
                async with self._session.ws_connect(
                    url, heartbeat=20, timeout=30,
                ) as ws:
                    logger.info("Binance WS connected: %s", url)
                    async for msg in ws:
                        if not self._running:
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            recv_ts = time.time()
                            data = json.loads(msg.data)
                            self._process_binance_tick(data, recv_ts)
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                logger.warning("Binance WS error: %s -- reconnecting in 2s", e)
                await asyncio.sleep(2)

    def _process_binance_tick(self, data: dict, recv_ts: float):
        """
        Process Binance bookTicker message.

        Format: {"s": "BTCUSDT", "b": "65000.10", "B": "1.2",
                 "a": "65000.20", "A": "0.8", "T": 1710000000000, ...}
        """
        raw_sym = data.get("s", "")
        bid = float(data.get("b", 0))
        ask = float(data.get("a", 0))

        if bid <= 0 or ask <= 0:
            return

        symbol = BINANCE_REVERSE_MAP.get(raw_sym.upper())
        if not symbol:
            return

        # Binance "T" field is transaction time in milliseconds
        exchange_ts = data.get("T", recv_ts * 1000) / 1000.0

        self._binance_prices[symbol] = PriceSnapshot(
            exchange="binance",
            symbol=symbol,
            bid=bid,
            ask=ask,
            mid=(bid + ask) / 2.0,
            timestamp=exchange_ts,
            local_recv_ts=recv_ts,
        )
        self._stats["binance_msgs"] += 1

    # ------------------------------------------------------------------
    # Delta India WebSocket (v2/ticker -- matches delta_ws.py)
    # ------------------------------------------------------------------

    async def _delta_ws_loop(self):
        """
        Subscribe to Delta India v2/ticker for all symbols.

        Message format (from delta_ws.py):
            {
                "type": "v2/ticker",
                "symbol": "BTCUSD",
                "close": "65000",
                "mark_price": "65001",
                "quotes": {"best_bid": "64999", "best_ask": "65001"},
                "timestamp": <milliseconds>,
                ...
            }
        """
        while self._running:
            try:
                async with self._session.ws_connect(
                    self.DELTA_WS_URL, heartbeat=25, timeout=30,
                ) as ws:
                    logger.info("Delta WS connected: %s", self.DELTA_WS_URL)

                    # Subscribe to v2/ticker for each symbol
                    delta_symbols = [
                        DELTA_SYMBOL_MAP[s]
                        for s in self.symbols
                        if s in DELTA_SYMBOL_MAP
                    ]
                    sub_msg = {
                        "type": "subscribe",
                        "payload": {
                            "channels": [
                                {"name": "v2/ticker", "symbols": delta_symbols}
                            ]
                        },
                    }
                    await ws.send_json(sub_msg)
                    logger.info("Delta WS subscribed to v2/ticker: %s", delta_symbols)

                    async for msg in ws:
                        if not self._running:
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            recv_ts = time.time()
                            data = json.loads(msg.data)
                            self._process_delta_tick(data, recv_ts)
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                logger.warning("Delta WS error: %s -- reconnecting in 2s", e)
                await asyncio.sleep(2)

    def _process_delta_tick(self, data: dict, recv_ts: float):
        """
        Process Delta v2/ticker message.

        Uses the exact same message schema as exchange/delta_ws.py:
        - type == "v2/ticker"
        - symbol in top-level
        - close, mark_price at top level
        - best_bid / best_ask nested under "quotes"
        - timestamp in milliseconds
        """
        if data.get("type") != "v2/ticker":
            return

        delta_sym = data.get("symbol", "")
        symbol = DELTA_REVERSE_MAP.get(delta_sym)
        if not symbol:
            return

        try:
            last = float(data.get("close", 0))
            quotes = data.get("quotes", {})
            bid = float(quotes.get("best_bid", last))
            ask = float(quotes.get("best_ask", last))
        except (ValueError, TypeError):
            return

        if bid <= 0 or ask <= 0:
            return

        # Delta timestamp is in milliseconds
        ts_raw = data.get("timestamp")
        if ts_raw:
            try:
                exchange_ts = float(ts_raw) / 1000.0
            except (ValueError, TypeError):
                exchange_ts = recv_ts
        else:
            exchange_ts = recv_ts

        self._delta_prices[symbol] = PriceSnapshot(
            exchange="delta",
            symbol=symbol,
            bid=bid,
            ask=ask,
            mid=(bid + ask) / 2.0,
            timestamp=exchange_ts,
            local_recv_ts=recv_ts,
        )
        self._stats["delta_msgs"] += 1

    # ------------------------------------------------------------------
    # Dislocation detection
    # ------------------------------------------------------------------

    async def _dislocation_checker(self):
        """Periodically compare prices and detect dislocations."""
        # Wait for initial prices to arrive
        await asyncio.sleep(3)

        while self._running:
            for symbol in self.symbols:
                bp = self._binance_prices.get(symbol)
                dp = self._delta_prices.get(symbol)

                if not bp or not dp:
                    continue

                # Both prices must be fresh
                # Binance: 2s, Delta: 10s (Delta has ~4.5s lag)
                now = time.time()
                if now - bp.local_recv_ts > self.BINANCE_FRESHNESS_S:
                    continue
                if now - dp.local_recv_ts > self.DELTA_FRESHNESS_S:
                    continue

                if dp.mid <= 0:
                    continue

                # Calculate dislocation
                disl_pct = (bp.mid - dp.mid) / dp.mid * 100.0
                disl_abs = bp.mid - dp.mid
                latency_ms = abs(bp.local_recv_ts - dp.local_recv_ts) * 1000.0
                direction = "long" if disl_pct > 0 else "short"

                disl = Dislocation(
                    symbol=symbol,
                    binance_mid=bp.mid,
                    delta_mid=dp.mid,
                    dislocation_pct=disl_pct,
                    dislocation_abs=disl_abs,
                    binance_ts=bp.timestamp,
                    delta_ts=dp.timestamp,
                    latency_ms=latency_ms,
                    direction=direction,
                    timestamp=now,
                )

                self._dislocation_history[symbol].append(disl)

                abs_disl = abs(disl_pct)

                # --- Layer 5: session segmentation ---
                utc_hour = datetime.fromtimestamp(now, tz=timezone.utc).hour
                self._hourly_stats[symbol][utc_hour].append({
                    "disl_pct": abs_disl,
                    "timestamp": now,
                    "direction": direction,
                })
                # Trim hourly buckets to last 2000 entries each
                bucket = self._hourly_stats[symbol][utc_hour]
                if len(bucket) > 2000:
                    self._hourly_stats[symbol][utc_hour] = bucket[-2000:]

                # --- Layer 5: volatility sampling (1 sample/s from Binance) ---
                if now - self._last_vol_sample_ts.get(symbol, 0) >= 1.0:
                    self._vol_window[symbol].append(bp.mid)
                    self._last_vol_sample_ts[symbol] = now

                # --- Layer 2: spike tracking ---
                self._update_spike_tracking(symbol, abs_disl, direction, bp, dp, now)

                # Check if dislocation is tradeable
                if abs_disl >= self.MIN_DISLOCATION_PCT:
                    self._stats["dislocations_detected"] += 1

                    if abs_disl > self.MAX_DISLOCATION_PCT:
                        logger.warning(
                            "EXTREME dislocation %s: %.3f%% -- skipping (bad data?)",
                            symbol, disl_pct,
                        )
                        continue

                    # Cooldown check
                    last_sig = self._last_signal_time.get(symbol, 0)
                    if now - last_sig < self._signal_cooldown_s:
                        continue

                    # Build confidence
                    confidence = self._calc_confidence(symbol, disl)

                    if confidence >= self.MIN_CONFIDENCE:
                        estimated_cost = 0.14  # % round-trip
                        expected_profit = abs_disl - estimated_cost

                        signal = LatencyArbSignal(
                            symbol=symbol,
                            side=direction,
                            delta_entry_price=dp.mid,
                            binance_target=bp.mid,
                            dislocation_pct=abs_disl,
                            expected_profit_pct=expected_profit,
                            confidence=confidence,
                            timestamp=now,
                        )

                        self._stats["signals_generated"] += 1
                        self._last_signal_time[symbol] = now

                        logger.info(
                            "LATENCY ARB SIGNAL: %s %s | Delta=%.2f Binance=%.2f | "
                            "Disl=%.3f%% | ExpProfit=%.3f%% | Conf=%d",
                            direction.upper(), symbol, dp.mid, bp.mid,
                            abs_disl, expected_profit, confidence,
                        )

                        if not self._measure_only and self.on_signal:
                            await self.on_signal(signal)

            await asyncio.sleep(0.05)  # 50ms check interval

    # ------------------------------------------------------------------
    # Confidence scoring
    # ------------------------------------------------------------------

    def _calc_confidence(self, symbol: str, disl: Dislocation) -> int:
        """Calculate confidence score (0-100) for a dislocation signal."""
        conf = 50  # base

        abs_disl = abs(disl.dislocation_pct)

        # Dislocation size: bigger = more confident
        if abs_disl >= self.STRONG_DISLOCATION_PCT:
            conf += 20
        elif abs_disl >= 0.25:
            conf += 10

        # Consistency: check if recent dislocations are in the same direction
        history = list(self._dislocation_history.get(symbol, []))[-20:]
        if len(history) >= 5:
            same_dir = sum(
                1 for d in history[-5:] if d.direction == disl.direction
            )
            if same_dir >= 4:
                conf += 15
            elif same_dir >= 3:
                conf += 5

        # Freshness: both prices very recent
        now = time.time()
        bp = self._binance_prices.get(symbol)
        dp = self._delta_prices.get(symbol)
        if bp and dp:
            max_age = max(now - bp.local_recv_ts, now - dp.local_recv_ts)
            if max_age < 0.5:
                conf += 10
            elif max_age < 1.0:
                conf += 5

        # Delta spread: tight spread = better fill
        if dp and dp.ask > 0 and dp.bid > 0:
            spread_pct = (dp.ask - dp.bid) / dp.mid * 100.0
            if spread_pct < 0.05:
                conf += 5
            elif spread_pct > 0.15:
                conf -= 10  # wide spread eats into profit

        return min(100, max(0, conf))

    # ==================================================================
    # LAYER 1: Net Edge Calculator
    # ==================================================================

    def compute_net_edge(self, symbol: str, gross_disl_pct: float) -> Dict:
        """Compute net edge after all costs. Returns classification."""
        t = self.PAIR_THRESHOLDS.get(symbol, self.DEFAULT_THRESHOLD)
        dp = self._delta_prices.get(symbol)

        spread_pct = 0.0
        if dp and dp.mid > 0:
            spread_pct = (dp.ask - dp.bid) / dp.mid * 100.0

        # Cost = spread + entry_fee + settlement + slippage
        total_cost_pct = spread_pct + (t["taker_fee"] * 100) + (t["settlement_fee"] * 100) + (t["slippage_est"] * 100)
        net_edge_pct = abs(gross_disl_pct) - total_cost_pct

        # Classification
        safety_buffer = t["min_net_edge"]
        if net_edge_pct <= 0:
            classification = "NO_TRADE"
        elif net_edge_pct < safety_buffer:
            classification = "WATCH"
        else:
            classification = "EXECUTABLE"

        # Additional checks for EXECUTABLE
        if classification == "EXECUTABLE":
            if spread_pct > t["max_spread"]:
                classification = "WATCH"  # spread too wide
            delta_age_ms = (time.time() - dp.local_recv_ts) * 1000 if dp else 99999
            if delta_age_ms > t["max_lag_ms"]:
                classification = "WATCH"  # data too stale

        return {
            "gross_disl_pct": round(abs(gross_disl_pct), 4),
            "spread_pct": round(spread_pct, 4),
            "total_cost_pct": round(total_cost_pct, 4),
            "net_edge_pct": round(net_edge_pct, 4),
            "classification": classification,
            "safety_buffer": safety_buffer,
        }

    # ==================================================================
    # LAYER 2: Decay Analysis -- Spike Tracking
    # ==================================================================

    def _update_spike_tracking(
        self,
        symbol: str,
        abs_disl: float,
        direction: str,
        bp: PriceSnapshot,
        dp: PriceSnapshot,
        now: float,
    ):
        """Track dislocation spikes: start, update survival, resolve."""
        spike = self._active_spikes.get(symbol)

        if spike is None:
            # No active spike -- check if we should start one
            if abs_disl >= self.MIN_DISLOCATION_PCT:
                new_spike = DislocationSpike(
                    symbol=symbol,
                    direction=direction,
                    peak_disl_pct=abs_disl,
                    start_time=now,
                    start_binance_mid=bp.mid,
                    start_delta_mid=dp.mid,
                )
                # Classify at inception
                edge_info = self.compute_net_edge(symbol, abs_disl)
                new_spike.peak_classification = edge_info["classification"]
                self._active_spikes[symbol] = new_spike
        else:
            # Active spike -- update
            elapsed = now - spike.start_time
            spike.duration_s = elapsed

            # Update peak
            if abs_disl > spike.peak_disl_pct:
                spike.peak_disl_pct = abs_disl
                # Re-classify at new peak
                edge_info = self.compute_net_edge(symbol, abs_disl)
                spike.peak_classification = edge_info["classification"]

            # Update survival flags
            if elapsed >= 1.0 and abs_disl >= self.MIN_DISLOCATION_PCT * 0.5:
                spike.survived_1s = True
            if elapsed >= 2.0 and abs_disl >= self.MIN_DISLOCATION_PCT * 0.5:
                spike.survived_2s = True
            if elapsed >= 5.0 and abs_disl >= self.MIN_DISLOCATION_PCT * 0.5:
                spike.survived_5s = True
            if elapsed >= 10.0 and abs_disl >= self.MIN_DISLOCATION_PCT * 0.5:
                spike.survived_10s = True

            # Check if spike should resolve:
            # abs_disl dropped below 50% of peak OR below 0.05%
            should_resolve = (
                abs_disl < spike.peak_disl_pct * 0.5
                or abs_disl < 0.05
                or elapsed > 60.0  # hard timeout
            )

            if should_resolve:
                spike.end_time = now
                spike.end_binance_mid = bp.mid
                spike.end_delta_mid = dp.mid

                # Determine convergence: who moved?
                initial_gap = spike.start_binance_mid - spike.start_delta_mid
                if abs(initial_gap) > 0:
                    # How much did Delta move toward where Binance was?
                    delta_move = spike.end_delta_mid - spike.start_delta_mid
                    binance_move = spike.end_binance_mid - spike.start_binance_mid

                    # Convergence %: how much of the initial gap closed
                    end_gap = spike.end_binance_mid - spike.end_delta_mid
                    spike.convergence_pct = (1.0 - abs(end_gap) / abs(initial_gap)) * 100.0
                    spike.convergence_pct = max(0.0, min(100.0, spike.convergence_pct))

                    # Who caused the convergence?
                    # If Delta moved more toward Binance's initial position -> delta_converged
                    # If Binance moved back toward Delta's initial position -> binance_reverted
                    if abs(initial_gap) > 0:
                        delta_contribution = abs(delta_move) / abs(initial_gap) if initial_gap != 0 else 0
                        binance_contribution = abs(binance_move) / abs(initial_gap) if initial_gap != 0 else 0

                        # Check direction of moves
                        delta_moved_toward = (delta_move > 0) == (initial_gap > 0)
                        binance_moved_back = (binance_move > 0) != (initial_gap > 0)

                        if delta_moved_toward and delta_contribution > binance_contribution:
                            spike.delta_converged = True
                        elif binance_moved_back and binance_contribution > delta_contribution:
                            spike.binance_reverted = True
                        else:
                            # Mixed: both moved
                            spike.delta_converged = delta_moved_toward
                            spike.binance_reverted = binance_moved_back

                # --- Layer 4: simulate trade if spike was EXECUTABLE at peak ---
                self._maybe_simulate_trade(spike, dp)

                self._spike_history[symbol].append(spike)
                self._active_spikes[symbol] = None

    # ==================================================================
    # LAYER 3: Convergence Analysis
    # ==================================================================

    def get_convergence_stats(self, symbol: str = None) -> Dict:
        """For resolved spikes: what % of the time did Delta catch up vs Binance revert?"""
        symbols = [symbol] if symbol else self.symbols
        result: Dict[str, Any] = {}

        for sym in symbols:
            spikes = list(self._spike_history.get(sym, []))
            if not spikes:
                result[sym] = {
                    "total_resolved": 0,
                    "delta_converged_pct": 0.0,
                    "binance_reverted_pct": 0.0,
                    "mixed_pct": 0.0,
                    "avg_convergence_pct": 0.0,
                }
                continue

            n = len(spikes)
            delta_conv = sum(1 for s in spikes if s.delta_converged and not s.binance_reverted)
            binance_rev = sum(1 for s in spikes if s.binance_reverted and not s.delta_converged)
            mixed = sum(1 for s in spikes if s.delta_converged and s.binance_reverted)
            neither = n - delta_conv - binance_rev - mixed

            avg_conv = float(np.mean([s.convergence_pct for s in spikes])) if spikes else 0.0

            result[sym] = {
                "total_resolved": n,
                "delta_converged_pct": round(delta_conv / n * 100, 1),
                "binance_reverted_pct": round(binance_rev / n * 100, 1),
                "mixed_pct": round(mixed / n * 100, 1),
                "neither_pct": round(neither / n * 100, 1),
                "avg_convergence_pct": round(avg_conv, 1),
            }

        return result

    # ==================================================================
    # LAYER 4: Edge Simulation -- Simulated P&L Replay
    # ==================================================================

    def _maybe_simulate_trade(self, spike: DislocationSpike, dp: PriceSnapshot):
        """If the spike was EXECUTABLE at peak, simulate what would have happened."""
        if spike.peak_classification != "EXECUTABLE":
            return

        t = self.PAIR_THRESHOLDS.get(spike.symbol, self.DEFAULT_THRESHOLD)

        if spike.direction == "long":
            # Buy on Delta at ask + slippage
            entry_price = spike.start_delta_mid * (1.0 + t["slippage_est"])
        else:
            # Sell on Delta at bid - slippage
            entry_price = spike.start_delta_mid * (1.0 - t["slippage_est"])

        # Exit at Delta mid at resolution
        exit_price = spike.end_delta_mid if spike.end_delta_mid > 0 else dp.mid

        if spike.direction == "long":
            gross_pnl_pct = (exit_price - entry_price) / entry_price * 100.0
        else:
            gross_pnl_pct = (entry_price - exit_price) / entry_price * 100.0

        fees_pct = (t["taker_fee"] + t["settlement_fee"]) * 100.0

        net_pnl_pct = gross_pnl_pct - fees_pct

        trade = SimulatedTrade(
            symbol=spike.symbol,
            side=spike.direction,
            entry_price=round(entry_price, 6),
            exit_price=round(exit_price, 6),
            gross_pnl_pct=round(gross_pnl_pct, 4),
            fees_pct=round(fees_pct, 4),
            net_pnl_pct=round(net_pnl_pct, 4),
            hold_time_s=round(spike.duration_s, 2),
            was_profitable=net_pnl_pct > 0,
            timestamp=spike.start_time,
        )
        self._simulated_trades[spike.symbol].append(trade)

    def get_simulation_results(self, symbol: str = None) -> Dict:
        """Return simulated trade results."""
        symbols = [symbol] if symbol else self.symbols
        result: Dict[str, Any] = {"by_symbol": {}}
        all_trades: List[SimulatedTrade] = []

        for sym in symbols:
            trades = list(self._simulated_trades.get(sym, []))
            all_trades.extend(trades)

            if not trades:
                result["by_symbol"][sym] = {
                    "total": 0, "win_rate": 0.0,
                    "avg_net_pnl": 0.0, "total_net_pnl": 0.0,
                    "avg_hold_time_s": 0.0,
                }
                continue

            wins = sum(1 for t in trades if t.was_profitable)
            result["by_symbol"][sym] = {
                "total": len(trades),
                "win_rate": round(wins / len(trades) * 100, 1),
                "avg_net_pnl": round(float(np.mean([t.net_pnl_pct for t in trades])), 4),
                "total_net_pnl": round(sum(t.net_pnl_pct for t in trades), 4),
                "avg_hold_time_s": round(float(np.mean([t.hold_time_s for t in trades])), 2),
            }

        # Aggregate
        if all_trades:
            wins_total = sum(1 for t in all_trades if t.was_profitable)
            result["total_simulated"] = len(all_trades)
            result["win_rate"] = round(wins_total / len(all_trades) * 100, 1)
            result["avg_net_pnl"] = round(float(np.mean([t.net_pnl_pct for t in all_trades])), 4)
            result["total_net_pnl"] = round(sum(t.net_pnl_pct for t in all_trades), 4)
            result["avg_hold_time_s"] = round(float(np.mean([t.hold_time_s for t in all_trades])), 2)
        else:
            result["total_simulated"] = 0
            result["win_rate"] = 0.0
            result["avg_net_pnl"] = 0.0
            result["total_net_pnl"] = 0.0
            result["avg_hold_time_s"] = 0.0

        return result

    # ==================================================================
    # LAYER 2 (cont'd): Decay Analysis Stats
    # ==================================================================

    def get_decay_analysis(self, symbol: str = None) -> Dict:
        """Return decay/survival statistics for dislocation spikes."""
        symbols = [symbol] if symbol else self.symbols
        result: Dict[str, Any] = {}

        for sym in symbols:
            spikes = list(self._spike_history.get(sym, []))
            if not spikes:
                result[sym] = {
                    "total_spikes": 0,
                    "avg_duration_s": 0.0,
                    "avg_peak_disl_pct": 0.0,
                    "survival_1s_pct": 0.0,
                    "survival_2s_pct": 0.0,
                    "survival_5s_pct": 0.0,
                    "survival_10s_pct": 0.0,
                }
                continue

            n = len(spikes)
            result[sym] = {
                "total_spikes": n,
                "avg_duration_s": round(float(np.mean([s.duration_s for s in spikes])), 2),
                "avg_peak_disl_pct": round(float(np.mean([s.peak_disl_pct for s in spikes])), 4),
                "survival_1s_pct": round(sum(1 for s in spikes if s.survived_1s) / n * 100, 1),
                "survival_2s_pct": round(sum(1 for s in spikes if s.survived_2s) / n * 100, 1),
                "survival_5s_pct": round(sum(1 for s in spikes if s.survived_5s) / n * 100, 1),
                "survival_10s_pct": round(sum(1 for s in spikes if s.survived_10s) / n * 100, 1),
                "executable_at_peak_pct": round(
                    sum(1 for s in spikes if s.peak_classification == "EXECUTABLE") / n * 100, 1
                ),
            }

        return result

    # ==================================================================
    # LAYER 5: Session / Event Segmentation
    # ==================================================================

    def _compute_rolling_volatility(self, symbol: str) -> float:
        """Compute rolling volatility as annualized std of 1s returns from Binance."""
        prices = list(self._vol_window.get(symbol, []))
        if len(prices) < 5:
            return 0.0
        arr = np.array(prices)
        returns = np.diff(arr) / arr[:-1]
        if len(returns) == 0:
            return 0.0
        return float(np.std(returns) * 100.0)  # as percentage

    def get_session_stats(self, symbol: str = None) -> Dict:
        """Return dislocation statistics segmented by UTC hour and volatility."""
        symbols = [symbol] if symbol else self.symbols
        result: Dict[str, Any] = {}

        for sym in symbols:
            hourly = {}
            quiet_hours: List[int] = []
            active_hours: List[int] = []

            for h in range(24):
                bucket = self._hourly_stats[sym][h]
                if not bucket:
                    hourly[h] = {
                        "count": 0, "avg_disl": 0.0, "max_disl": 0.0,
                        "tradeable_count": 0, "pct_tradeable": 0.0,
                    }
                    quiet_hours.append(h)
                    continue

                disls = [e["disl_pct"] for e in bucket]
                tradeable = sum(1 for d in disls if d >= self.MIN_DISLOCATION_PCT)
                avg_d = float(np.mean(disls))
                max_d = float(max(disls))

                hourly[h] = {
                    "count": len(bucket),
                    "avg_disl": round(avg_d, 4),
                    "max_disl": round(max_d, 4),
                    "tradeable_count": tradeable,
                    "pct_tradeable": round(tradeable / len(bucket) * 100, 1) if bucket else 0.0,
                }

                # Classify hours
                if avg_d >= self.MIN_DISLOCATION_PCT * 0.5 and tradeable >= 3:
                    active_hours.append(h)
                elif avg_d < self.MIN_DISLOCATION_PCT * 0.25:
                    quiet_hours.append(h)

            result[sym] = {
                "hourly": hourly,
                "active_hours_utc": sorted(active_hours),
                "quiet_hours_utc": sorted(quiet_hours),
                "current_volatility_pct": round(self._compute_rolling_volatility(sym), 4),
            }

        return result

    # ------------------------------------------------------------------
    # Public accessors (updated with all 5 analysis layers)
    # ------------------------------------------------------------------

    def get_stats(self) -> Dict[str, Any]:
        """Return measurement statistics including all analysis layers."""
        result: Dict[str, Any] = dict(self._stats)
        result["avg_dislocation_pct"] = {}
        result["max_dislocation_pct"] = {}
        result["avg_latency_ms"] = {}

        for symbol in self.symbols:
            history = list(self._dislocation_history.get(symbol, []))
            if not history:
                continue
            disls = [abs(d.dislocation_pct) for d in history]
            lats = [d.latency_ms for d in history]
            result["avg_dislocation_pct"][symbol] = round(np.mean(disls), 4)
            result["max_dislocation_pct"][symbol] = round(max(disls), 4)
            result[f"p95_dislocation_pct_{symbol}"] = round(
                float(np.percentile(disls, 95)), 4
            )
            result["avg_latency_ms"][symbol] = round(float(np.mean(lats)), 1)

            # How often is dislocation >= threshold?
            tradeable = sum(1 for d in disls if d >= self.MIN_DISLOCATION_PCT)
            result[f"tradeable_pct_{symbol}"] = round(
                tradeable / len(disls) * 100.0, 1
            )

        if self._stats["started_at"]:
            result["uptime_s"] = round(time.time() - self._stats["started_at"], 0)

        # --- All 5 analysis layers ---
        result["net_edge"] = {
            sym: self.compute_net_edge(sym, abs(self._dislocation_history[sym][-1].dislocation_pct))
            if self._dislocation_history.get(sym)
            else {"classification": "NO_DATA"}
            for sym in self.symbols
        }
        result["decay"] = self.get_decay_analysis()
        result["convergence"] = self.get_convergence_stats()
        result["simulation"] = self.get_simulation_results()
        result["session"] = self.get_session_stats()

        return result

    def get_dislocation_history(self, symbol: str) -> List[Dislocation]:
        """Return the full dislocation deque as a list (for analysis scripts)."""
        return list(self._dislocation_history.get(symbol, []))

    def get_recent_dislocations(self, symbol: str, n: int = 20) -> List[Dict]:
        """Return recent dislocations formatted for display (with net edge)."""
        history = list(self._dislocation_history.get(symbol, []))[-n:]
        results = []
        for d in history:
            edge_info = self.compute_net_edge(symbol, d.dislocation_pct)
            results.append({
                "binance": round(d.binance_mid, 2),
                "delta": round(d.delta_mid, 2),
                "disl_pct": round(d.dislocation_pct, 4),
                "direction": d.direction,
                "latency_ms": round(d.latency_ms, 1),
                "net_edge": round(edge_info["net_edge_pct"], 4),
                "classification": edge_info["classification"],
                "time": datetime.fromtimestamp(
                    d.timestamp, tz=timezone.utc
                ).strftime("%H:%M:%S.%f")[:-3],
            })
        return results
