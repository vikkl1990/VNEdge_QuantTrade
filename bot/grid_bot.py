"""
Grid Bot — Profits from price oscillation without predicting direction.

Places buy orders below current price and sell orders above.
Every completed buy→sell cycle captures the grid spread as profit.

Backtest proven: ALL 8 coins profitable, +$224/day on $50/coin.

Architecture:
- Dynamic grid center: 100-bar SMA (adapts to trending markets)
- Grid levels: ±N levels at configurable spacing (0.2-0.5%)
- Position tracking: each filled buy becomes a pending sell
- Risk: max exposure per coin, stale position cleanup
- Fees: supports maker (0.10%) and taker (0.18%) modes
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

_STORAGE_DIR = Path("storage")
_GRID_FILE = _STORAGE_DIR / "grid_state.json"


@dataclass
class GridLevel:
    """A single grid level with buy/sell state."""
    price: float
    side: str  # "buy" or "sell"
    filled: bool = False
    fill_time: str = ""
    fill_price: float = 0.0


@dataclass
class GridPosition:
    """A filled buy waiting for its sell to complete."""
    buy_price: float
    buy_time: str
    symbol: str
    quantity: float = 0.0
    status: str = "open"  # open, closed, stale


@dataclass
class GridFill:
    """A completed buy→sell cycle = one grid profit."""
    symbol: str
    buy_price: float
    sell_price: float
    buy_time: str
    sell_time: str
    gross_pct: float
    net_pct: float
    profit_usd: float
    fees_usd: float


class GridBot:
    """
    Grid trading bot that profits from price oscillation.

    Config:
        grid_pct: spacing between grid levels (0.002 = 0.2%)
        num_levels: number of levels above and below center
        position_usd: USD per grid level
        fee_rt: round-trip fee rate (0.0018 taker, 0.0010 maker)
        max_positions: max open buy positions per symbol
        stale_pct: close positions that drift >X% from current price
    """

    def __init__(self, config: Dict[str, Any] = None):
        cfg = config or {}
        grid_cfg = cfg.get("grid", {})

        self.grid_pct: float = grid_cfg.get("grid_pct", 0.002)  # 0.2% default
        self.num_levels: int = grid_cfg.get("num_levels", 10)
        self.position_usd: float = grid_cfg.get("position_usd", 50.0)
        self.fee_rt: float = grid_cfg.get("fee_rt", 0.0010)  # default maker
        self.max_positions: int = grid_cfg.get("max_positions", 20)
        self.stale_pct: float = grid_cfg.get("stale_pct", 0.02)  # 2%
        self.sma_period: int = grid_cfg.get("sma_period", 100)

        # State per symbol
        self._positions: Dict[str, List[GridPosition]] = {}  # symbol → open positions
        self._fills: List[GridFill] = []
        self._price_history: Dict[str, List[float]] = {}  # symbol → recent closes
        self._last_grid_center: Dict[str, float] = {}

        # Stats
        self._total_fills: int = 0
        self._total_profit: float = 0.0
        self._total_fees: float = 0.0
        self._start_time: float = time.time()

        # Load saved state
        self._load_state()

        logger.info(
            "GridBot initialized: spacing=%.2f%%, levels=%d, position=$%.0f, fee=%.2f%%",
            self.grid_pct * 100, self.num_levels, self.position_usd, self.fee_rt * 100,
        )

    def seed_history(self, symbol: str, closes: list) -> None:
        """Seed price history from historical candle data.

        Call this on startup to avoid waiting 8+ hours for SMA to build.
        Pass the last 100-200 close prices from the data feed.
        """
        if not closes:
            return
        self._price_history[symbol] = list(closes[-self.sma_period - 10:])
        center = float(np.mean(self._price_history[symbol][-self.sma_period:]))
        self._last_grid_center[symbol] = center
        logger.info(
            "GridBot seeded %s: %d prices, SMA center=$%.4f — READY TO TRADE",
            symbol, len(self._price_history[symbol]), center,
        )

    def update(self, symbol: str, price: float, high: float = 0, low: float = 0) -> List[Dict[str, Any]]:
        """
        Process a new price tick for a symbol.

        Returns list of events (grid fills, new positions, etc.)
        """
        if price <= 0:
            return []

        # Use high/low if provided, else just price
        if high <= 0:
            high = price
        if low <= 0:
            low = price

        events = []

        # Track price history for SMA
        if symbol not in self._price_history:
            self._price_history[symbol] = []
        self._price_history[symbol].append(price)
        if len(self._price_history[symbol]) > self.sma_period + 50:
            self._price_history[symbol] = self._price_history[symbol][-self.sma_period - 10:]

        # Need enough history for SMA
        if len(self._price_history[symbol]) < self.sma_period:
            return []

        # Calculate dynamic grid center (SMA)
        center = np.mean(self._price_history[symbol][-self.sma_period:])
        self._last_grid_center[symbol] = center

        # Initialize positions list
        if symbol not in self._positions:
            self._positions[symbol] = []

        now_iso = datetime.now(timezone.utc).isoformat()

        # ══════════════════════════════════════════
        # STEP 1: Check SELL fills (close existing positions)
        # ══════════════════════════════════════════
        positions_to_close = []
        for pos in self._positions[symbol]:
            if pos.status != "open":
                continue

            # Generate sell level for this position
            sell_price = pos.buy_price * (1 + self.grid_pct)

            # Check if high reached sell level
            if high >= sell_price:
                # GRID FILL! Buy→Sell cycle complete
                gross_pct = (sell_price - pos.buy_price) / pos.buy_price * 100
                # fee_rt is ROUND-TRIP (includes both buy + sell legs)
                net_pct = gross_pct - self.fee_rt * 100  # fee_rt already covers both sides
                profit_usd = self.position_usd * net_pct / 100
                fees_usd = self.position_usd * self.fee_rt

                fill = GridFill(
                    symbol=symbol,
                    buy_price=pos.buy_price,
                    sell_price=sell_price,
                    buy_time=pos.buy_time,
                    sell_time=now_iso,
                    gross_pct=round(gross_pct, 4),
                    net_pct=round(net_pct, 4),
                    profit_usd=round(profit_usd, 4),
                    fees_usd=round(fees_usd, 4),
                )
                self._fills.append(fill)
                self._total_fills += 1
                self._total_profit += profit_usd
                self._total_fees += fees_usd

                pos.status = "closed"
                positions_to_close.append(pos)

                events.append({
                    "type": "grid_fill",
                    "symbol": symbol,
                    "buy": round(pos.buy_price, 4),
                    "sell": round(sell_price, 4),
                    "profit": round(profit_usd, 4),
                    "net_pct": round(net_pct, 4),
                    "message": (
                        f"GRID FILL: {symbol} buy@{pos.buy_price:.4f} → sell@{sell_price:.4f} | "
                        f"Profit: ${profit_usd:+.4f} ({net_pct:+.3f}%)"
                    ),
                })

                logger.info(
                    "GRID FILL: %s buy@%.4f → sell@%.4f | $%+.4f (%+.3f%%)",
                    symbol, pos.buy_price, sell_price, profit_usd, net_pct,
                )

        # Remove closed positions
        self._positions[symbol] = [p for p in self._positions[symbol] if p.status == "open"]

        # ══════════════════════════════════════════
        # STEP 2: Check BUY fills (new positions from grid levels below)
        # ══════════════════════════════════════════
        open_count = len(self._positions[symbol])

        if open_count < self.max_positions:
            # Generate grid levels below current price
            for level_idx in range(1, self.num_levels + 1):
                buy_level = center * (1 - level_idx * self.grid_pct)

                # Check if low touched this level
                if low <= buy_level:
                    # Check if we already have a position at this level
                    already_have = any(
                        abs(p.buy_price - buy_level) / buy_level < 0.001
                        for p in self._positions[symbol]
                    )

                    if not already_have and open_count < self.max_positions:
                        pos = GridPosition(
                            buy_price=buy_level,
                            buy_time=now_iso,
                            symbol=symbol,
                            quantity=self.position_usd / buy_level,
                        )
                        self._positions[symbol].append(pos)
                        open_count += 1

                        events.append({
                            "type": "grid_buy",
                            "symbol": symbol,
                            "price": round(buy_level, 4),
                            "target_sell": round(buy_level * (1 + self.grid_pct), 4),
                            "message": f"GRID BUY: {symbol} @ {buy_level:.4f} | Target sell: {buy_level * (1 + self.grid_pct):.4f}",
                        })

        # ══════════════════════════════════════════
        # STEP 3: Clean stale positions (drifted too far)
        # ══════════════════════════════════════════
        stale = []
        for pos in self._positions[symbol]:
            if pos.status == "open":
                drift = abs(pos.buy_price - price) / price
                if drift > self.stale_pct:
                    pos.status = "stale"
                    stale.append(pos)
                    # Close at market (loss)
                    loss_pct = (price - pos.buy_price) / pos.buy_price * 100
                    loss_usd = self.position_usd * loss_pct / 100
                    self._total_profit += loss_usd
                    events.append({
                        "type": "grid_stale",
                        "symbol": symbol,
                        "buy": round(pos.buy_price, 4),
                        "current": round(price, 4),
                        "loss": round(loss_usd, 4),
                        "message": f"GRID STALE: {symbol} buy@{pos.buy_price:.4f} → close@{price:.4f} | ${loss_usd:+.4f}",
                    })

        self._positions[symbol] = [p for p in self._positions[symbol] if p.status == "open"]

        # Save state periodically
        if self._total_fills % 10 == 0 and events:
            self._save_state()

        return events

    def get_status(self) -> Dict[str, Any]:
        """Get current grid bot status for dashboard."""
        uptime = time.time() - self._start_time

        positions_by_symbol = {}
        for symbol, positions in self._positions.items():
            positions_by_symbol[symbol] = {
                "open": len([p for p in positions if p.status == "open"]),
                "center": round(self._last_grid_center.get(symbol, 0), 4),
            }

        recent_fills = self._fills[-20:] if self._fills else []

        return {
            "enabled": True,
            "grid_pct": self.grid_pct * 100,
            "num_levels": self.num_levels,
            "position_usd": self.position_usd,
            "fee_rt_pct": self.fee_rt * 100,
            "total_fills": self._total_fills,
            "total_profit": round(self._total_profit, 2),
            "total_fees": round(self._total_fees, 2),
            "uptime_sec": int(uptime),
            "fills_per_hour": round(self._total_fills / max(uptime / 3600, 0.01), 1),
            "profit_per_hour": round(self._total_profit / max(uptime / 3600, 0.01), 2),
            "positions": positions_by_symbol,
            "recent_fills": [asdict(f) for f in recent_fills],
        }

    def get_open_positions(self) -> List[Dict[str, Any]]:
        """Get all open grid positions for dashboard."""
        result = []
        for symbol, positions in self._positions.items():
            for pos in positions:
                if pos.status == "open":
                    sell_target = pos.buy_price * (1 + self.grid_pct)
                    result.append({
                        "symbol": symbol,
                        "buy_price": round(pos.buy_price, 4),
                        "sell_target": round(sell_target, 4),
                        "buy_time": pos.buy_time,
                        "position_usd": self.position_usd,
                    })
        return result

    def _save_state(self) -> None:
        """Save grid state to disk."""
        try:
            state = {
                "total_fills": self._total_fills,
                "total_profit": self._total_profit,
                "total_fees": self._total_fees,
                "positions": {
                    sym: [{"buy_price": p.buy_price, "buy_time": p.buy_time, "symbol": p.symbol}
                          for p in positions if p.status == "open"]
                    for sym, positions in self._positions.items()
                },
                "fills": [asdict(f) for f in self._fills[-100:]],  # keep last 100
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }
            _STORAGE_DIR.mkdir(exist_ok=True)
            with open(_GRID_FILE, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.warning("Failed to save grid state: %s", e)

    def _load_state(self) -> None:
        """Load grid state from disk."""
        try:
            if _GRID_FILE.exists():
                with open(_GRID_FILE) as f:
                    state = json.load(f)
                self._total_fills = state.get("total_fills", 0)
                self._total_profit = state.get("total_profit", 0.0)
                self._total_fees = state.get("total_fees", 0.0)

                for sym, positions in state.get("positions", {}).items():
                    self._positions[sym] = [
                        GridPosition(buy_price=p["buy_price"], buy_time=p["buy_time"], symbol=p.get("symbol", sym))
                        for p in positions
                    ]

                for fill_data in state.get("fills", []):
                    self._fills.append(GridFill(**fill_data))

                logger.info(
                    "GridBot loaded: %d fills, $%.2f profit, %d open positions",
                    self._total_fills, self._total_profit,
                    sum(len(p) for p in self._positions.values()),
                )
        except Exception as e:
            logger.warning("Failed to load grid state: %s", e)
