"""
Trade data model for the crypto trading bot.

Represents a single trade lifecycle from entry signal through exit,
tracking all relevant metrics including PnL, slippage, and excursion.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class TradeStatus(Enum):
    """Lifecycle states of a trade."""
    PENDING = "pending"        # Order submitted, not yet filled
    OPEN = "open"              # Entry filled, position active
    PARTIAL = "partial"        # Partially exited
    CLOSED = "closed"          # Fully exited
    CANCELLED = "cancelled"    # Order rejected or cancelled before fill
    FAILED = "failed"          # Execution error


class TradeSide(Enum):
    """Direction of the trade."""
    LONG = "long"
    SHORT = "short"


@dataclass
class TakeProfit:
    """A single take-profit level."""
    price: float
    close_pct: float          # Percentage of position to close at this TP
    hit: bool = False
    hit_time: Optional[datetime] = None
    order_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "price": self.price,
            "close_pct": self.close_pct,
            "hit": self.hit,
            "hit_time": self.hit_time.isoformat() if self.hit_time else None,
            "order_id": self.order_id,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TakeProfit:
        return cls(
            price=data["price"],
            close_pct=data["close_pct"],
            hit=data.get("hit", False),
            hit_time=datetime.fromisoformat(data["hit_time"]) if data.get("hit_time") else None,
            order_id=data.get("order_id"),
        )


@dataclass
class Trade:
    """
    Complete representation of a single trade.

    Tracks the full lifecycle from signal through entry, management,
    and exit, including all fills, fees, and performance metrics.
    """

    # --- Identity ---
    trade_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    symbol: str = ""
    side: TradeSide = TradeSide.LONG
    status: TradeStatus = TradeStatus.PENDING

    # --- Entry ---
    entry_price: float = 0.0
    entry_time: Optional[datetime] = None
    entry_order_id: Optional[str] = None

    # --- Exit ---
    exit_price: float = 0.0
    exit_time: Optional[datetime] = None

    # --- Risk levels ---
    stop_loss: float = 0.0
    take_profits: List[TakeProfit] = field(default_factory=list)
    current_sl: float = 0.0         # Current trailing stop level
    trailing_activated: bool = False

    # --- Sizing ---
    position_size: float = 0.0      # Size in base currency (e.g. BTC)
    position_size_usd: float = 0.0  # Notional value at entry
    leverage: int = 1
    filled_size: float = 0.0        # How much has actually been filled
    remaining_size: float = 0.0     # How much is still open

    # --- Performance ---
    pnl: float = 0.0               # Realized PnL in quote currency
    pnl_pct: float = 0.0           # Realized PnL as percentage of entry
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0
    fees: float = 0.0              # Total fees paid
    slippage: float = 0.0          # Entry slippage in quote currency

    # --- Excursion tracking ---
    max_favorable_excursion: float = 0.0   # Best unrealized PnL seen
    max_adverse_excursion: float = 0.0     # Worst unrealized PnL seen
    highest_price_seen: float = 0.0
    lowest_price_seen: float = 0.0

    # --- Signal metadata ---
    entry_reason: str = ""
    exit_reason: str = ""
    grade: str = ""                 # A+, A, B, C, etc.
    confidence: float = 0.0
    timeframe: str = ""
    indicators: Dict[str, Any] = field(default_factory=dict)

    # --- Order tracking ---
    order_ids: List[str] = field(default_factory=list)

    # --- Timestamps ---
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def __post_init__(self):
        if self.current_sl == 0.0 and self.stop_loss > 0.0:
            self.current_sl = self.stop_loss
        if self.remaining_size == 0.0 and self.position_size > 0.0:
            self.remaining_size = self.position_size

    # ------------------------------------------------------------------
    # Price tracking
    # ------------------------------------------------------------------

    def update_price(self, current_price: float) -> None:
        """Update excursion tracking with a new price tick."""
        if self.status not in (TradeStatus.OPEN, TradeStatus.PARTIAL):
            return

        if current_price > self.highest_price_seen or self.highest_price_seen == 0.0:
            self.highest_price_seen = current_price
        if current_price < self.lowest_price_seen or self.lowest_price_seen == 0.0:
            self.lowest_price_seen = current_price

        unrealized = self._calc_unrealized(current_price)
        self.unrealized_pnl = unrealized
        if self.position_size_usd > 0:
            self.unrealized_pnl_pct = (unrealized / self.position_size_usd) * 100.0

        if unrealized > self.max_favorable_excursion:
            self.max_favorable_excursion = unrealized
        if unrealized < self.max_adverse_excursion:
            self.max_adverse_excursion = unrealized

        self.updated_at = datetime.utcnow()

    def _calc_unrealized(self, current_price: float) -> float:
        """Calculate unrealized PnL for the remaining open position."""
        if self.remaining_size <= 0 or self.entry_price <= 0:
            return 0.0

        if self.side == TradeSide.LONG:
            return (current_price - self.entry_price) * self.remaining_size
        else:
            return (self.entry_price - current_price) * self.remaining_size

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    def mark_filled(self, fill_price: float, fill_size: float, fee: float = 0.0) -> None:
        """Record an entry fill."""
        self.entry_price = fill_price
        self.entry_time = datetime.utcnow()
        self.filled_size = fill_size
        self.remaining_size = fill_size
        self.position_size = fill_size
        self.position_size_usd = fill_price * fill_size
        self.fees += fee
        self.status = TradeStatus.OPEN
        self.highest_price_seen = fill_price
        self.lowest_price_seen = fill_price
        self.updated_at = datetime.utcnow()

    def mark_partial_exit(
        self, exit_price: float, exit_size: float, fee: float = 0.0, reason: str = ""
    ) -> float:
        """
        Record a partial exit. Returns the realized PnL for this chunk.
        """
        if self.side == TradeSide.LONG:
            chunk_pnl = (exit_price - self.entry_price) * exit_size
        else:
            chunk_pnl = (self.entry_price - exit_price) * exit_size

        chunk_pnl -= fee
        self.pnl += chunk_pnl
        self.fees += fee
        self.remaining_size -= exit_size
        self.remaining_size = max(self.remaining_size, 0.0)

        if self.position_size_usd > 0:
            self.pnl_pct = (self.pnl / self.position_size_usd) * 100.0

        if self.remaining_size <= 0:
            self.status = TradeStatus.CLOSED
            self.exit_price = exit_price
            self.exit_time = datetime.utcnow()
            self.exit_reason = reason or self.exit_reason
        else:
            self.status = TradeStatus.PARTIAL
            if reason:
                self.exit_reason = reason

        self.updated_at = datetime.utcnow()
        return chunk_pnl

    def mark_closed(self, exit_price: float, fee: float = 0.0, reason: str = "") -> float:
        """Close the entire remaining position."""
        return self.mark_partial_exit(exit_price, self.remaining_size, fee, reason)

    def mark_cancelled(self, reason: str = "") -> None:
        """Cancel a pending trade that was never filled."""
        self.status = TradeStatus.CANCELLED
        self.exit_reason = reason or "cancelled"
        self.updated_at = datetime.utcnow()

    def mark_failed(self, reason: str = "") -> None:
        """Mark trade as failed due to execution error."""
        self.status = TradeStatus.FAILED
        self.exit_reason = reason or "execution_failed"
        self.updated_at = datetime.utcnow()

    # ------------------------------------------------------------------
    # TP / SL helpers
    # ------------------------------------------------------------------

    def is_stop_hit(self, current_price: float) -> bool:
        """Check if current price has hit the stop loss."""
        if self.current_sl <= 0:
            return False
        if self.side == TradeSide.LONG:
            return current_price <= self.current_sl
        else:
            return current_price >= self.current_sl

    def next_tp(self) -> Optional[TakeProfit]:
        """Return the next unhit take-profit level, or None."""
        for tp in self.take_profits:
            if not tp.hit:
                return tp
        return None

    def is_tp_hit(self, current_price: float) -> Optional[TakeProfit]:
        """Check if current price has hit any unhit take-profit level."""
        for tp in self.take_profits:
            if tp.hit:
                continue
            if self.side == TradeSide.LONG and current_price >= tp.price:
                return tp
            elif self.side == TradeSide.SHORT and current_price <= tp.price:
                return tp
        return None

    # ------------------------------------------------------------------
    # Duration
    # ------------------------------------------------------------------

    @property
    def duration_seconds(self) -> Optional[float]:
        """How long the trade has been / was open, in seconds."""
        if self.entry_time is None:
            return None
        end = self.exit_time if self.exit_time else datetime.utcnow()
        return (end - self.entry_time).total_seconds()

    @property
    def is_winner(self) -> bool:
        return self.pnl > 0

    @property
    def is_open(self) -> bool:
        return self.status in (TradeStatus.OPEN, TradeStatus.PARTIAL)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Serialize trade to a plain dictionary."""
        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "status": self.status.value,
            "entry_price": self.entry_price,
            "entry_time": self.entry_time.isoformat() if self.entry_time else None,
            "entry_order_id": self.entry_order_id,
            "exit_price": self.exit_price,
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "stop_loss": self.stop_loss,
            "take_profits": [tp.to_dict() for tp in self.take_profits],
            "current_sl": self.current_sl,
            "trailing_activated": self.trailing_activated,
            "position_size": self.position_size,
            "position_size_usd": self.position_size_usd,
            "leverage": self.leverage,
            "filled_size": self.filled_size,
            "remaining_size": self.remaining_size,
            "pnl": self.pnl,
            "pnl_pct": self.pnl_pct,
            "unrealized_pnl": self.unrealized_pnl,
            "unrealized_pnl_pct": self.unrealized_pnl_pct,
            "fees": self.fees,
            "slippage": self.slippage,
            "max_favorable_excursion": self.max_favorable_excursion,
            "max_adverse_excursion": self.max_adverse_excursion,
            "highest_price_seen": self.highest_price_seen,
            "lowest_price_seen": self.lowest_price_seen,
            "entry_reason": self.entry_reason,
            "exit_reason": self.exit_reason,
            "grade": self.grade,
            "confidence": self.confidence,
            "timeframe": self.timeframe,
            "indicators": self.indicators,
            "order_ids": self.order_ids,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Trade:
        """Deserialize a trade from a plain dictionary."""
        trade = cls(
            trade_id=data.get("trade_id", str(uuid.uuid4())[:12]),
            symbol=data.get("symbol", ""),
            side=TradeSide(data.get("side", "long")),
            status=TradeStatus(data.get("status", "pending")),
            entry_price=data.get("entry_price", 0.0),
            exit_price=data.get("exit_price", 0.0),
            stop_loss=data.get("stop_loss", 0.0),
            current_sl=data.get("current_sl", 0.0),
            trailing_activated=data.get("trailing_activated", False),
            position_size=data.get("position_size", 0.0),
            position_size_usd=data.get("position_size_usd", 0.0),
            leverage=data.get("leverage", 1),
            filled_size=data.get("filled_size", 0.0),
            remaining_size=data.get("remaining_size", 0.0),
            pnl=data.get("pnl", 0.0),
            pnl_pct=data.get("pnl_pct", 0.0),
            unrealized_pnl=data.get("unrealized_pnl", 0.0),
            unrealized_pnl_pct=data.get("unrealized_pnl_pct", 0.0),
            fees=data.get("fees", 0.0),
            slippage=data.get("slippage", 0.0),
            max_favorable_excursion=data.get("max_favorable_excursion", 0.0),
            max_adverse_excursion=data.get("max_adverse_excursion", 0.0),
            highest_price_seen=data.get("highest_price_seen", 0.0),
            lowest_price_seen=data.get("lowest_price_seen", 0.0),
            entry_reason=data.get("entry_reason", ""),
            exit_reason=data.get("exit_reason", ""),
            grade=data.get("grade", ""),
            confidence=data.get("confidence", 0.0),
            timeframe=data.get("timeframe", ""),
            indicators=data.get("indicators", {}),
            order_ids=data.get("order_ids", []),
        )

        # Parse datetimes
        if data.get("entry_time"):
            trade.entry_time = datetime.fromisoformat(data["entry_time"])
        if data.get("exit_time"):
            trade.exit_time = datetime.fromisoformat(data["exit_time"])
        if data.get("created_at"):
            trade.created_at = datetime.fromisoformat(data["created_at"])
        if data.get("updated_at"):
            trade.updated_at = datetime.fromisoformat(data["updated_at"])

        trade.entry_order_id = data.get("entry_order_id")

        # Parse take-profit list
        trade.take_profits = [
            TakeProfit.from_dict(tp) for tp in data.get("take_profits", [])
        ]

        return trade

    def __repr__(self) -> str:
        return (
            f"Trade(id={self.trade_id}, {self.symbol} {self.side.value} "
            f"{self.status.value}, entry={self.entry_price:.2f}, "
            f"size={self.position_size:.6f}, pnl={self.pnl:.2f})"
        )
