"""
Exchange abstraction layer.

Provides a unified interface for interacting with cryptocurrency exchanges
(Binance, Bybit, OKX, Delta Exchange) through the ``ExchangeBase`` contract
and the ``CcxtExchangeClient`` implementation.

Quick start::

    from exchange import create_exchange_client

    client = create_exchange_client()
    async with client:
        ticker = await client.fetch_ticker("BTC/USDT")
"""

from exchange.base import (
    Balance,
    ExchangeBase,
    FundingRate,
    OHLCV,
    OpenInterest,
    Order,
    OrderBook,
    OrderBookLevel,
    Position,
    Ticker,
)
from exchange.ccxt_client import (
    CcxtExchangeClient,
    ExchangeConnectionError,
    ExchangeDowntimeError,
    InsufficientMarginError,
    OrderRejectedError,
    StalePriceError,
)
from exchange.factory import create_exchange_client

__all__ = [
    # Client classes
    "ExchangeBase",
    "CcxtExchangeClient",
    # Factory
    "create_exchange_client",
    # Data containers
    "Balance",
    "FundingRate",
    "OHLCV",
    "OpenInterest",
    "Order",
    "OrderBook",
    "OrderBookLevel",
    "Position",
    "Ticker",
    # Exceptions
    "ExchangeConnectionError",
    "ExchangeDowntimeError",
    "InsufficientMarginError",
    "OrderRejectedError",
    "StalePriceError",
]
