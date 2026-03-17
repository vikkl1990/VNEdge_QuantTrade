"""
Abstract base class defining the exchange interface.

Every exchange adapter (CCXT, mock, replay) must implement this contract
so the rest of the bot can treat all exchanges identically.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, List, Optional

from config.constants import (
    ExchangeStatus,
    MarketType,
    OrderSide,
    OrderType,
    TimeInForce,
)


# ---------------------------------------------------------------------------
# Normalised data containers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Ticker:
    """Normalised ticker snapshot."""
    symbol: str
    bid: float
    ask: float
    last: float
    volume_24h: float
    timestamp: int  # Unix ms


@dataclass(frozen=True)
class OHLCV:
    """Single candlestick bar."""
    timestamp: int   # Unix ms (open time)
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class OrderBookLevel:
    price: float
    amount: float


@dataclass(frozen=True)
class OrderBook:
    """Normalised order book snapshot."""
    symbol: str
    bids: List[OrderBookLevel]
    asks: List[OrderBookLevel]
    timestamp: int  # Unix ms


@dataclass(frozen=True)
class FundingRate:
    """Perpetual-swap funding rate info."""
    symbol: str
    rate: float
    next_funding_time: int  # Unix ms
    timestamp: int


@dataclass(frozen=True)
class OpenInterest:
    """Aggregate open interest for a symbol."""
    symbol: str
    open_interest: float       # in contracts or base currency
    open_interest_value: float  # in quote (USD)
    timestamp: int


@dataclass
class Balance:
    """Account balance snapshot."""
    total: Dict[str, float] = field(default_factory=dict)
    free: Dict[str, float] = field(default_factory=dict)
    used: Dict[str, float] = field(default_factory=dict)


@dataclass
class Order:
    """Normalised order record."""
    id: str
    client_order_id: Optional[str]
    symbol: str
    side: str          # "long" / "short" (matches OrderSide values)
    order_type: str    # "market" / "limit" / ...
    amount: float
    price: Optional[float]
    filled: float
    remaining: float
    status: str        # "open", "closed", "canceled", "expired", "rejected"
    timestamp: int
    fee: Optional[float] = None
    fee_currency: Optional[str] = None
    average: Optional[float] = None
    raw: Optional[Dict[str, Any]] = None  # original exchange response


@dataclass
class Position:
    """Normalised futures position."""
    symbol: str
    side: str              # "long" / "short"
    size: float            # signed; positive = long
    entry_price: float
    mark_price: float
    liquidation_price: Optional[float]
    unrealised_pnl: float
    leverage: int
    margin_type: str       # "cross" / "isolated"
    timestamp: int
    raw: Optional[Dict[str, Any]] = None


# Callback type alias for websocket subscriptions
SubscriptionCallback = Callable[..., Coroutine[Any, Any, None]]


# ---------------------------------------------------------------------------
# Abstract exchange interface
# ---------------------------------------------------------------------------

class ExchangeBase(ABC):
    """
    Contract that every exchange adapter must fulfil.

    Subclasses are expected to be used as async context managers::

        async with CcxtExchangeClient(config) as client:
            ticker = await client.fetch_ticker("BTC/USDT")
    """

    def __init__(self) -> None:
        self._status: ExchangeStatus = ExchangeStatus.DISCONNECTED
        self._last_request_ts: float = 0.0
        self._request_count: int = 0
        self._error_count: int = 0
        self._connected_since: Optional[float] = None

    # -- async context manager ------------------------------------------------

    async def __aenter__(self) -> ExchangeBase:
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.disconnect()

    # -- connection lifecycle -------------------------------------------------

    @abstractmethod
    async def connect(self) -> None:
        """Authenticate and open connections (REST + WS)."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Cleanly close all connections and cancel running tasks."""

    # -- market data (REST) ---------------------------------------------------

    @abstractmethod
    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "5m",
        limit: int = 100,
    ) -> List[OHLCV]:
        """Fetch historical candlestick data."""

    @abstractmethod
    async def fetch_ticker(self, symbol: str) -> Ticker:
        """Fetch the latest ticker for *symbol*."""

    @abstractmethod
    async def fetch_order_book(
        self,
        symbol: str,
        limit: int = 25,
    ) -> OrderBook:
        """Fetch the current order book snapshot."""

    @abstractmethod
    async def fetch_funding_rate(self, symbol: str) -> FundingRate:
        """Fetch the current / next funding rate (futures only)."""

    @abstractmethod
    async def fetch_open_interest(self, symbol: str) -> OpenInterest:
        """Fetch aggregate open interest (futures only)."""

    # -- account / balance ----------------------------------------------------

    @abstractmethod
    async def fetch_balance(self) -> Balance:
        """Fetch the full account balance."""

    # -- order management -----------------------------------------------------

    @abstractmethod
    async def create_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        amount: float,
        price: Optional[float] = None,
        *,
        time_in_force: TimeInForce = TimeInForce.GTC,
        reduce_only: bool = False,
        params: Optional[Dict[str, Any]] = None,
    ) -> Order:
        """Place an order. Returns the normalised Order once acknowledged."""

    @abstractmethod
    async def cancel_order(
        self,
        order_id: str,
        symbol: str,
    ) -> Order:
        """Cancel an open order."""

    @abstractmethod
    async def fetch_open_orders(
        self,
        symbol: Optional[str] = None,
    ) -> List[Order]:
        """Fetch open orders, optionally filtered by symbol."""

    # -- position management (futures) ----------------------------------------

    @abstractmethod
    async def fetch_position(
        self,
        symbol: Optional[str] = None,
    ) -> List[Position]:
        """Fetch open position(s). Returns a list (may be empty)."""

    @abstractmethod
    async def set_leverage(
        self,
        symbol: str,
        leverage: int,
    ) -> None:
        """Set leverage for a symbol (futures only)."""

    # -- websocket subscriptions ----------------------------------------------

    @abstractmethod
    def subscribe_ticker(
        self,
        symbol: str,
        callback: SubscriptionCallback,
    ) -> None:
        """Register a callback for real-time ticker updates."""

    @abstractmethod
    def subscribe_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        callback: SubscriptionCallback,
    ) -> None:
        """Register a callback for real-time candlestick updates."""

    # -- health / status properties -------------------------------------------

    @property
    def status(self) -> ExchangeStatus:
        """Current connection status."""
        return self._status

    @property
    def is_connected(self) -> bool:
        return self._status == ExchangeStatus.CONNECTED

    @property
    def uptime_seconds(self) -> float:
        """Seconds since the client connected, or 0 if disconnected."""
        if self._connected_since is None:
            return 0.0
        return time.time() - self._connected_since

    @property
    def request_count(self) -> int:
        return self._request_count

    @property
    def error_count(self) -> int:
        return self._error_count

    @property
    def last_request_ts(self) -> float:
        """Unix timestamp of the most recent REST request."""
        return self._last_request_ts

    def health_summary(self) -> Dict[str, Any]:
        """Return a dict summarising the connection health."""
        return {
            "status": self._status.value,
            "uptime_s": round(self.uptime_seconds, 1),
            "requests": self._request_count,
            "errors": self._error_count,
            "last_request": self._last_request_ts,
        }
