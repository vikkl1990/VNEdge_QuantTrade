"""
CCXT-based exchange client supporting Binance, Bybit, OKX, and Delta Exchange.

Uses ccxt.pro for websocket streaming with automatic fallback to REST polling
when websockets are unavailable or fail.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import ccxt.pro as ccxtpro            # websocket-capable exchanges
import ccxt.async_support as ccxt_async  # async REST fallback
import ccxt as ccxt_sync               # used only for class resolution fallback

from config import get_config
from config.constants import (
    ExchangeName,
    ExchangeStatus,
    MarketType,
    OrderSide,
    OrderType,
    TimeInForce,
)

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
    SubscriptionCallback,
    Ticker,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exchange-specific configuration
# ---------------------------------------------------------------------------

# Map our canonical exchange name to the ccxt class name
_CCXT_CLASS_MAP: Dict[str, str] = {
    "binance": "binance",
    "bybit": "bybit",
    "okx": "okx",
    "delta": "delta",       # ccxt id for Delta Exchange
}

# Some exchanges use different symbol separators or naming for futures
# This mapping converts our canonical "BTC/USDT" format into what the
# exchange expects for perpetual swap markets.
_FUTURES_SYMBOL_SUFFIX: Dict[str, str] = {
    "binance": ":USDT",     # BTC/USDT:USDT
    "bybit": ":USDT",       # BTC/USDT:USDT
    "okx": ":USDT",         # BTC/USDT:USDT (swap)
    "delta": "",             # Delta uses BTC/USDT directly
}

# API key environment variable names per exchange
_ENV_KEY_MAP: Dict[str, Tuple[str, str, Optional[str]]] = {
    "binance": ("BINANCE_API_KEY", "BINANCE_API_SECRET", None),
    "bybit":   ("BYBIT_API_KEY",   "BYBIT_API_SECRET",   None),
    "okx":     ("OKX_API_KEY",     "OKX_API_SECRET",     "OKX_PASSPHRASE"),
    "delta":   ("DELTA_API_KEY",   "DELTA_API_SECRET",    None),
}

# REST polling intervals (seconds) when WS is unavailable
_POLL_INTERVALS = {
    "ticker": 1.0,
    "ohlcv": 5.0,
}


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class ExchangeConnectionError(Exception):
    """Raised when the exchange cannot be reached or authenticated."""


class OrderRejectedError(Exception):
    """Raised when the exchange rejects an order."""


class InsufficientMarginError(Exception):
    """Raised when there is not enough margin/balance for an order."""


class StalePriceError(Exception):
    """Raised when price data is considered too old to act upon."""


class ExchangeDowntimeError(Exception):
    """Raised when the exchange is in maintenance or unreachable."""


# ---------------------------------------------------------------------------
# CCXT Exchange Client
# ---------------------------------------------------------------------------

class CcxtExchangeClient(ExchangeBase):
    """
    Production exchange client built on top of ccxt / ccxt.pro.

    Supports Binance, Bybit, OKX, and Delta Exchange for both spot
    and USDT-margined perpetual futures.

    Parameters
    ----------
    config : dict
        Full bot configuration (as returned by ``get_config()``).
        The ``exchange`` section is used for connection parameters.
    """

    # Maximum age (ms) before a price is considered stale
    STALE_PRICE_THRESHOLD_MS: int = 30_000

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__()

        self._cfg = config or get_config()
        self._exchange_cfg: Dict[str, Any] = self._cfg["exchange"]

        self._exchange_name: str = self._exchange_cfg["name"].lower()
        self._region: str = self._exchange_cfg.get("region", "").lower()
        self._market_type = MarketType(self._exchange_cfg.get("market_type", "futures"))
        self._testnet: bool = self._exchange_cfg.get("testnet", True)
        self._use_ws: bool = self._exchange_cfg.get("websocket", True)
        self._rest_fallback: bool = self._exchange_cfg.get("rest_fallback", True)
        self._reconnect_attempts: int = self._exchange_cfg.get("reconnect_attempts", 10)
        self._reconnect_delay: int = self._exchange_cfg.get("reconnect_delay", 5)
        self._is_delta_india: bool = (self._exchange_name == "delta" and self._region == "india")
        if self._is_delta_india:
            logger.info("Delta India mode enabled (api.india.delta.exchange)")

        # ccxt exchange instance (set on connect)
        self._exchange: Optional[ccxtpro.Exchange] = None

        # Subscription management
        self._ws_tasks: Dict[str, asyncio.Task] = {}
        self._subscriptions: Dict[str, SubscriptionCallback] = {}
        self._poll_tasks: Dict[str, asyncio.Task] = {}
        self._running: bool = False

        # Cached market info for symbol normalisation
        self._markets_loaded: bool = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_ccxt_options(self) -> Dict[str, Any]:
        """Build the options dict passed to the ccxt constructor."""
        key_env, secret_env, passphrase_env = _ENV_KEY_MAP.get(
            self._exchange_name, (None, None, None)
        )

        api_key = os.environ.get(key_env, "") if key_env else ""
        api_secret = os.environ.get(secret_env, "") if secret_env else ""
        passphrase = os.environ.get(passphrase_env, "") if passphrase_env else None

        options: Dict[str, Any] = {
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": self._exchange_cfg.get("rate_limit", True),
            "timeout": 30000,  # 30s max per API call to prevent infinite hangs
            "options": {
                "defaultType": self._market_type.value,
                "adjustForTimeDifference": True,
            },
        }

        if passphrase:
            options["password"] = passphrase

        # Testnet / sandbox URLs
        if self._testnet:
            options["sandbox"] = True

        # Delta India region support — override API URLs
        region = self._exchange_cfg.get("region", "").lower()
        if self._exchange_name == "delta" and region == "india":
            options["urls"] = {
                "api": {
                    "public": "https://api.india.delta.exchange",
                    "private": "https://api.india.delta.exchange",
                },
            }
            logger.info("Using Delta India API endpoint (api.india.delta.exchange)")

        return options

    def _resolve_ccxt_class(self) -> type:
        """Return the ccxt.pro exchange class for the configured exchange."""
        ccxt_id = _CCXT_CLASS_MAP.get(self._exchange_name)
        if ccxt_id is None:
            raise ValueError(
                f"Unsupported exchange '{self._exchange_name}'. "
                f"Supported: {list(_CCXT_CLASS_MAP.keys())}"
            )
        klass = getattr(ccxtpro, ccxt_id, None)
        if klass is None:
            # Fallback: try async ccxt (no WS) if ccxt.pro doesn't list it
            klass = getattr(ccxt_async, ccxt_id, None)
            if klass is None:
                raise ValueError(f"ccxt does not have exchange class '{ccxt_id}'")
            logger.warning(
                "Exchange '%s' not available in ccxt.pro; "
                "websockets will be unavailable. Using async REST fallback.",
                ccxt_id,
            )
            self._use_ws = False
        return klass

    # Delta India uses multiplier-prefixed names for thin-price memes to keep
    # contract size reasonable. Canonical "PEPE/USDT" needs to map to
    # "1000PEPE/USD:USD" on the exchange (each contract = 1000 PEPE).
    # Keep this in sync with exchange/delta_client.py INSTRUMENTS.
    _DELTA_BASE_OVERRIDE: Dict[str, str] = {
        "PEPE": "1000PEPE",
        "SHIB": "1000SHIB",
        "BONK": "1000BONK",
        "FLOKI": "1000FLOKI",
        "BABYDOGE": "1MBABYDOGE",
    }
    # Reverse map for _from_exchange_symbol round-trip
    _DELTA_BASE_REVERSE: Dict[str, str] = {v: k for k, v in {
        "PEPE": "1000PEPE",
        "SHIB": "1000SHIB",
        "BONK": "1000BONK",
        "FLOKI": "1000FLOKI",
        "BABYDOGE": "1MBABYDOGE",
    }.items()}

    def _to_exchange_symbol(self, symbol: str) -> str:
        """
        Convert a canonical symbol (``BTC/USDT``) to the exchange-specific
        format required for the configured market type.

        For futures, most exchanges use ``BTC/USDT:USDT``; for spot the
        canonical format is used as-is.
        """
        # Delta India: BTC/USDT → BTC/USD:USD (futures) or BTC/INR (spot)
        # Meme 1000x contracts: PEPE/USDT → 1000PEPE/USD:USD
        if self._is_delta_india:
            base = symbol.split("/")[0]  # e.g. "BTC" or "PEPE"
            base = self._DELTA_BASE_OVERRIDE.get(base, base)
            if self._market_type == MarketType.SPOT:
                return f"{base}/INR"
            else:
                return f"{base}/USD:USD"

        if self._market_type == MarketType.SPOT:
            return symbol

        suffix = _FUTURES_SYMBOL_SUFFIX.get(self._exchange_name, "")
        if suffix and not symbol.endswith(suffix):
            return f"{symbol}{suffix}"
        return symbol

    def _from_exchange_symbol(self, symbol: str) -> str:
        """Strip futures suffix to return canonical symbol (``BTC/USDT``).

        Reverses 1000x meme mapping: 1000PEPE/USD:USD → PEPE/USDT.
        """
        # Delta India: BTC/USD:USD → BTC/USDT, BTC/INR → BTC/USDT
        if self._is_delta_india:
            base = symbol.split("/")[0]
            base = self._DELTA_BASE_REVERSE.get(base, base)
            return f"{base}/USDT"  # normalize back to canonical

        suffix = _FUTURES_SYMBOL_SUFFIX.get(self._exchange_name, "")
        if suffix and symbol.endswith(suffix):
            return symbol[: -len(suffix)]
        return symbol

    def _map_order_side(self, side: OrderSide) -> str:
        """Map our OrderSide enum to the string ccxt expects."""
        # ccxt expects "buy" / "sell"; our enum uses "long" / "short"
        if side == OrderSide.LONG:
            return "buy"
        return "sell"

    def _map_order_type(self, order_type: OrderType) -> str:
        """Map our OrderType enum to the string ccxt expects."""
        _mapping = {
            OrderType.MARKET: "market",
            OrderType.LIMIT: "limit",
            OrderType.STOP: "stop",
            OrderType.STOP_LIMIT: "stop",
            OrderType.TAKE_PROFIT: "take_profit",
            OrderType.TAKE_PROFIT_LIMIT: "take_profit",
            OrderType.TRAILING_STOP: "trailing_stop",
        }
        return _mapping.get(order_type, order_type.value)

    async def _ensure_markets(self) -> None:
        """Load market info once so symbol resolution works."""
        if not self._markets_loaded and self._exchange is not None:
            # Force Delta India URLs BEFORE loading markets
            if self._is_delta_india:
                self._exchange.urls["api"] = {
                    "public": "https://api.india.delta.exchange",
                    "private": "https://api.india.delta.exchange",
                }
            await self._exchange.load_markets()
            self._markets_loaded = True
            # Re-apply after load (ccxt sometimes resets)
            if self._is_delta_india:
                self._exchange.urls["api"] = {
                    "public": "https://api.india.delta.exchange",
                    "private": "https://api.india.delta.exchange",
                }
                logger.info("Delta India markets loaded: %d symbols available", len(self._exchange.markets))

    def _track_request(self) -> None:
        """Bump internal request counters."""
        self._request_count += 1
        self._last_request_ts = time.time()

    def _track_error(self) -> None:
        self._error_count += 1

    # ------------------------------------------------------------------
    # Retry / error handling decorator logic
    # ------------------------------------------------------------------

    async def _retry(
        self,
        coro_factory: Callable[[], Any],
        *,
        retries: int = 3,
        base_delay: float = 1.0,
        operation: str = "",
    ) -> Any:
        """
        Execute an async callable with exponential-backoff retries.

        Handles transient network errors, rate limits, and exchange
        maintenance windows. Non-retryable errors are raised immediately.
        """
        last_exc: Optional[Exception] = None

        for attempt in range(1, retries + 1):
            try:
                self._track_request()
                result = await coro_factory()
                return result

            except (
                ccxt_sync.NetworkError,
                ccxt_sync.RequestTimeout,
                ccxt_sync.ExchangeNotAvailable,
            ) as exc:
                last_exc = exc
                self._track_error()
                delay = base_delay * (2 ** (attempt - 1))
                logger.warning(
                    "%s failed (attempt %d/%d): %s. Retrying in %.1fs ...",
                    operation or "Request",
                    attempt,
                    retries,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)

            except ccxt_sync.RateLimitExceeded as exc:
                last_exc = exc
                self._track_error()
                # Respect the exchange-suggested wait if available
                delay = base_delay * (3 ** (attempt - 1))
                logger.warning(
                    "Rate limit hit during %s (attempt %d/%d). "
                    "Backing off %.1fs ...",
                    operation or "Request",
                    attempt,
                    retries,
                    delay,
                )
                await asyncio.sleep(delay)

            except ccxt_sync.InsufficientFunds as exc:
                raise InsufficientMarginError(str(exc)) from exc

            except ccxt_sync.InvalidOrder as exc:
                raise OrderRejectedError(str(exc)) from exc

            except ccxt_sync.OnMaintenance as exc:
                raise ExchangeDowntimeError(
                    f"Exchange is in maintenance: {exc}"
                ) from exc

            except ccxt_sync.AuthenticationError as exc:
                raise ExchangeConnectionError(
                    f"Authentication failed: {exc}"
                ) from exc

            except ccxt_sync.ExchangeError as exc:
                # Catch-all for other exchange-level errors
                self._track_error()
                raise

        # All retries exhausted
        raise ExchangeConnectionError(
            f"{operation or 'Request'} failed after {retries} attempts: {last_exc}"
        ) from last_exc

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open exchange connection, load markets, start WS loops."""
        if self._status == ExchangeStatus.CONNECTED:
            logger.debug("Already connected to %s", self._exchange_name)
            return

        self._status = ExchangeStatus.CONNECTING
        logger.info("Connecting to %s (%s) ...", self._exchange_name, self._market_type.value)

        klass = self._resolve_ccxt_class()
        opts = self._build_ccxt_options()
        self._exchange = klass(opts)

        try:
            await self._ensure_markets()
        except Exception as exc:
            self._status = ExchangeStatus.ERROR
            self._track_error()
            raise ExchangeConnectionError(
                f"Failed to load markets from {self._exchange_name}: {exc}"
            ) from exc

        self._status = ExchangeStatus.CONNECTED
        self._connected_since = time.time()
        self._running = True
        logger.info(
            "Connected to %s. Loaded %d markets.",
            self._exchange_name,
            len(self._exchange.markets),
        )

    async def disconnect(self) -> None:
        """Cancel all tasks and close the exchange connection."""
        self._running = False

        # Cancel WS watcher tasks
        for key, task in {**self._ws_tasks, **self._poll_tasks}.items():
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._ws_tasks.clear()
        self._poll_tasks.clear()

        if self._exchange is not None:
            try:
                await self._exchange.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error closing exchange: %s", exc)
            self._exchange = None

        self._status = ExchangeStatus.DISCONNECTED
        self._connected_since = None
        self._markets_loaded = False
        logger.info("Disconnected from %s.", self._exchange_name)

    # ------------------------------------------------------------------
    # Market data -- REST
    # ------------------------------------------------------------------

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "5m",
        limit: int = 100,
    ) -> List[OHLCV]:
        assert self._exchange is not None, "Call connect() first"
        await self._ensure_markets()
        ex_symbol = self._to_exchange_symbol(symbol)

        raw: List[list] = await self._retry(
            lambda: self._exchange.fetch_ohlcv(ex_symbol, timeframe, limit=limit),
            operation=f"fetch_ohlcv({symbol}, {timeframe})",
        )

        return [
            OHLCV(
                timestamp=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
            for row in raw
        ]

    async def fetch_ticker(self, symbol: str) -> Ticker:
        assert self._exchange is not None, "Call connect() first"
        ex_symbol = self._to_exchange_symbol(symbol)

        raw: dict = await self._retry(
            lambda: self._exchange.fetch_ticker(ex_symbol),
            operation=f"fetch_ticker({symbol})",
        )

        ts = raw.get("timestamp") or int(time.time() * 1000)

        # Stale-price guard
        age_ms = int(time.time() * 1000) - ts
        if age_ms > self.STALE_PRICE_THRESHOLD_MS:
            logger.warning(
                "Ticker for %s is %.1fs old -- may be stale.",
                symbol,
                age_ms / 1000,
            )

        return Ticker(
            symbol=self._from_exchange_symbol(raw.get("symbol", ex_symbol)),
            bid=float(raw.get("bid") or 0),
            ask=float(raw.get("ask") or 0),
            last=float(raw.get("last") or 0),
            volume_24h=float(raw.get("quoteVolume") or raw.get("baseVolume") or 0),
            timestamp=ts,
        )

    async def fetch_order_book(
        self,
        symbol: str,
        limit: int = 25,
    ) -> OrderBook:
        assert self._exchange is not None, "Call connect() first"
        ex_symbol = self._to_exchange_symbol(symbol)

        raw: dict = await self._retry(
            lambda: self._exchange.fetch_order_book(ex_symbol, limit),
            operation=f"fetch_order_book({symbol})",
        )

        return OrderBook(
            symbol=self._from_exchange_symbol(raw.get("symbol", ex_symbol)),
            bids=[OrderBookLevel(price=float(b[0]), amount=float(b[1]))
                  for b in raw.get("bids", [])],
            asks=[OrderBookLevel(price=float(a[0]), amount=float(a[1]))
                  for a in raw.get("asks", [])],
            timestamp=raw.get("timestamp") or int(time.time() * 1000),
        )

    async def fetch_funding_rate(self, symbol: str) -> FundingRate:
        assert self._exchange is not None, "Call connect() first"
        ex_symbol = self._to_exchange_symbol(symbol)

        raw: dict = await self._retry(
            lambda: self._exchange.fetch_funding_rate(ex_symbol),
            operation=f"fetch_funding_rate({symbol})",
        )

        return FundingRate(
            symbol=self._from_exchange_symbol(raw.get("symbol", ex_symbol)),
            rate=float(raw.get("fundingRate") or 0),
            next_funding_time=int(raw.get("fundingDatetime") or raw.get("nextFundingDatetime") or 0),
            timestamp=raw.get("timestamp") or int(time.time() * 1000),
        )

    async def fetch_open_interest(self, symbol: str) -> OpenInterest:
        assert self._exchange is not None, "Call connect() first"
        ex_symbol = self._to_exchange_symbol(symbol)

        raw: dict = await self._retry(
            lambda: self._exchange.fetch_open_interest(ex_symbol),
            operation=f"fetch_open_interest({symbol})",
        )

        return OpenInterest(
            symbol=self._from_exchange_symbol(raw.get("symbol", ex_symbol)),
            open_interest=float(raw.get("openInterestAmount") or raw.get("openInterest") or 0),
            open_interest_value=float(raw.get("openInterestValue") or 0),
            timestamp=raw.get("timestamp") or int(time.time() * 1000),
        )

    # ------------------------------------------------------------------
    # Account / balance
    # ------------------------------------------------------------------

    def _fix_delta_india_urls(self) -> None:
        """Force-set Delta India API URLs on the ccxt exchange object.

        ccxt's internal methods (load_markets, describe, etc.) can silently
        reset the custom URLs back to the global ``api.delta.exchange``.
        Call this before any private API call to guarantee India routing.
        """
        india_url = "https://api.india.delta.exchange"
        if self._is_delta_india and self._exchange is not None:
            current = self._exchange.urls.get("api", {})
            needs_fix = False
            if isinstance(current, dict):
                needs_fix = (
                    current.get("public") != india_url
                    or current.get("private") != india_url
                )
            else:
                needs_fix = True

            if needs_fix:
                logger.warning(
                    "Fixed Delta India URLs (were: %s → now: india)", current
                )

            # Always force-set to be safe
            self._exchange.urls["api"] = {
                "public": india_url,
                "private": india_url,
            }

    async def fetch_balance(self) -> Balance:
        assert self._exchange is not None, "Call connect() first"

        # Guarantee India URLs before private API call
        self._fix_delta_india_urls()

        raw: dict = await self._retry(
            lambda: self._exchange.fetch_balance(),
            operation="fetch_balance",
        )

        return Balance(
            total={k: float(v) for k, v in (raw.get("total") or {}).items() if v},
            free={k: float(v) for k, v in (raw.get("free") or {}).items() if v},
            used={k: float(v) for k, v in (raw.get("used") or {}).items() if v},
        )

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

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
        assert self._exchange is not None, "Call connect() first"
        ex_symbol = self._to_exchange_symbol(symbol)
        ccxt_side = self._map_order_side(side)
        ccxt_type = self._map_order_type(order_type)

        extra: Dict[str, Any] = dict(params or {})

        # Attach time-in-force for limit orders
        if order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT, OrderType.TAKE_PROFIT_LIMIT):
            extra.setdefault("timeInForce", time_in_force.value)

        if reduce_only:
            extra["reduceOnly"] = True

        # Stop / TP price handling
        if order_type in (OrderType.STOP, OrderType.STOP_LIMIT) and price is not None:
            extra.setdefault("stopPrice", price)
            # For STOP_LIMIT, price is the limit; stopPrice triggers it
            if order_type == OrderType.STOP:
                price = None  # market stop -- no limit price

        if order_type in (OrderType.TAKE_PROFIT, OrderType.TAKE_PROFIT_LIMIT) and price is not None:
            extra.setdefault("takeProfitPrice", price)
            if order_type == OrderType.TAKE_PROFIT:
                price = None

        logger.info(
            "Creating %s %s order: %s %.6f @ %s  params=%s",
            ccxt_side, ccxt_type, ex_symbol, amount, price, extra,
        )

        raw: dict = await self._retry(
            lambda: self._exchange.create_order(
                ex_symbol, ccxt_type, ccxt_side, amount, price, extra,
            ),
            retries=2,
            operation=f"create_order({symbol}, {ccxt_side}, {ccxt_type})",
        )

        return self._parse_order(raw)

    async def cancel_order(self, order_id: str, symbol: str) -> Order:
        assert self._exchange is not None, "Call connect() first"
        ex_symbol = self._to_exchange_symbol(symbol)

        logger.info("Cancelling order %s on %s", order_id, ex_symbol)

        raw: dict = await self._retry(
            lambda: self._exchange.cancel_order(order_id, ex_symbol),
            operation=f"cancel_order({order_id})",
        )

        return self._parse_order(raw)

    async def fetch_open_orders(
        self,
        symbol: Optional[str] = None,
    ) -> List[Order]:
        assert self._exchange is not None, "Call connect() first"
        ex_symbol = self._to_exchange_symbol(symbol) if symbol else None

        raw_list: list = await self._retry(
            lambda: self._exchange.fetch_open_orders(ex_symbol),
            operation="fetch_open_orders",
        )

        return [self._parse_order(r) for r in raw_list]

    def _parse_order(self, raw: dict) -> Order:
        """Convert a ccxt order dict into our normalised Order."""
        raw_side = (raw.get("side") or "buy").lower()
        # Map back to our enum values
        side = "long" if raw_side == "buy" else "short"

        fee_info = raw.get("fee") or {}

        return Order(
            id=str(raw.get("id", "")),
            client_order_id=raw.get("clientOrderId"),
            symbol=self._from_exchange_symbol(raw.get("symbol", "")),
            side=side,
            order_type=raw.get("type", "unknown"),
            amount=float(raw.get("amount") or 0),
            price=float(raw["price"]) if raw.get("price") is not None else None,
            filled=float(raw.get("filled") or 0),
            remaining=float(raw.get("remaining") or 0),
            status=raw.get("status", "unknown"),
            timestamp=raw.get("timestamp") or int(time.time() * 1000),
            fee=float(fee_info["cost"]) if fee_info.get("cost") is not None else None,
            fee_currency=fee_info.get("currency"),
            average=float(raw["average"]) if raw.get("average") is not None else None,
            raw=raw,
        )

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    async def fetch_position(
        self,
        symbol: Optional[str] = None,
    ) -> List[Position]:
        assert self._exchange is not None, "Call connect() first"

        if self._market_type == MarketType.SPOT:
            return []  # no positions in spot

        symbols = [self._to_exchange_symbol(symbol)] if symbol else None

        raw_list: list = await self._retry(
            lambda: self._exchange.fetch_positions(symbols),
            operation="fetch_positions",
        )

        positions: List[Position] = []
        for raw in raw_list:
            size = float(raw.get("contracts") or raw.get("contractSize") or 0)
            if size == 0:
                continue  # skip empty positions

            raw_side = (raw.get("side") or "long").lower()
            if raw_side not in ("long", "short"):
                raw_side = "long" if size > 0 else "short"

            positions.append(Position(
                symbol=self._from_exchange_symbol(raw.get("symbol", "")),
                side=raw_side,
                size=size if raw_side == "long" else -size,
                entry_price=float(raw.get("entryPrice") or 0),
                mark_price=float(raw.get("markPrice") or 0),
                liquidation_price=(
                    float(raw["liquidationPrice"])
                    if raw.get("liquidationPrice") is not None
                    else None
                ),
                unrealised_pnl=float(raw.get("unrealizedPnl") or 0),
                leverage=int(float(raw.get("leverage") or 1)),
                margin_type=(raw.get("marginMode") or "cross").lower(),
                timestamp=raw.get("timestamp") or int(time.time() * 1000),
                raw=raw,
            ))

        return positions

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        assert self._exchange is not None, "Call connect() first"
        ex_symbol = self._to_exchange_symbol(symbol)

        logger.info("Setting leverage for %s to %dx", ex_symbol, leverage)

        await self._retry(
            lambda: self._exchange.set_leverage(leverage, ex_symbol),
            operation=f"set_leverage({symbol}, {leverage}x)",
        )

    # ------------------------------------------------------------------
    # Websocket subscriptions
    # ------------------------------------------------------------------

    def subscribe_ticker(
        self,
        symbol: str,
        callback: SubscriptionCallback,
    ) -> None:
        key = f"ticker:{symbol}"
        self._subscriptions[key] = callback

        if self._running:
            self._start_subscription(key, symbol, "ticker")

    def subscribe_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        callback: SubscriptionCallback,
    ) -> None:
        key = f"ohlcv:{symbol}:{timeframe}"
        self._subscriptions[key] = callback

        if self._running:
            self._start_subscription(key, symbol, "ohlcv", timeframe=timeframe)

    def _start_subscription(
        self,
        key: str,
        symbol: str,
        data_type: str,
        *,
        timeframe: str = "5m",
    ) -> None:
        """Spin up the WS watcher (or REST poller as fallback)."""
        if key in self._ws_tasks and not self._ws_tasks[key].done():
            return  # already running

        if self._use_ws:
            task = asyncio.create_task(
                self._ws_loop(key, symbol, data_type, timeframe),
                name=f"ws-{key}",
            )
            self._ws_tasks[key] = task
        elif self._rest_fallback:
            task = asyncio.create_task(
                self._poll_loop(key, symbol, data_type, timeframe),
                name=f"poll-{key}",
            )
            self._poll_tasks[key] = task
        else:
            logger.warning(
                "WebSocket unavailable and REST fallback disabled for %s",
                key,
            )

    async def _ws_loop(
        self,
        key: str,
        symbol: str,
        data_type: str,
        timeframe: str,
    ) -> None:
        """
        Continuously watch a ccxt.pro websocket stream.

        On disconnect, attempts reconnection up to ``reconnect_attempts``
        times with exponential back-off. Falls back to REST polling if
        reconnection fails and ``rest_fallback`` is enabled.
        """
        ex_symbol = self._to_exchange_symbol(symbol)
        callback = self._subscriptions.get(key)
        if callback is None:
            return

        consecutive_failures = 0

        while self._running:
            try:
                if data_type == "ticker":
                    raw = await self._exchange.watch_ticker(ex_symbol)
                    ticker = Ticker(
                        symbol=self._from_exchange_symbol(raw.get("symbol", ex_symbol)),
                        bid=float(raw.get("bid") or 0),
                        ask=float(raw.get("ask") or 0),
                        last=float(raw.get("last") or 0),
                        volume_24h=float(raw.get("quoteVolume") or raw.get("baseVolume") or 0),
                        timestamp=raw.get("timestamp") or int(time.time() * 1000),
                    )
                    await callback(ticker)

                elif data_type == "ohlcv":
                    raw_list = await self._exchange.watch_ohlcv(ex_symbol, timeframe)
                    candles = [
                        OHLCV(
                            timestamp=int(row[0]),
                            open=float(row[1]),
                            high=float(row[2]),
                            low=float(row[3]),
                            close=float(row[4]),
                            volume=float(row[5]),
                        )
                        for row in raw_list
                    ]
                    if candles:
                        await callback(candles[-1])  # latest candle

                consecutive_failures = 0

            except asyncio.CancelledError:
                break

            except Exception as exc:  # noqa: BLE001
                consecutive_failures += 1
                self._track_error()

                if consecutive_failures > self._reconnect_attempts:
                    logger.error(
                        "WS stream %s failed %d times. Giving up websocket.",
                        key,
                        consecutive_failures,
                    )
                    if self._rest_fallback:
                        logger.info(
                            "Falling back to REST polling for %s.", key,
                        )
                        task = asyncio.create_task(
                            self._poll_loop(key, symbol, data_type, timeframe),
                            name=f"poll-{key}",
                        )
                        self._poll_tasks[key] = task
                    break

                delay = min(
                    self._reconnect_delay * (2 ** (consecutive_failures - 1)),
                    60,
                )
                self._status = ExchangeStatus.RECONNECTING
                logger.warning(
                    "WS stream %s error (%d/%d): %s. Reconnecting in %.1fs ...",
                    key,
                    consecutive_failures,
                    self._reconnect_attempts,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                self._status = ExchangeStatus.CONNECTED

    async def _poll_loop(
        self,
        key: str,
        symbol: str,
        data_type: str,
        timeframe: str,
    ) -> None:
        """REST polling fallback for when websockets are unavailable."""
        callback = self._subscriptions.get(key)
        if callback is None:
            return

        interval = _POLL_INTERVALS.get(data_type, 5.0)

        while self._running:
            try:
                if data_type == "ticker":
                    ticker = await self.fetch_ticker(symbol)
                    await callback(ticker)

                elif data_type == "ohlcv":
                    candles = await self.fetch_ohlcv(symbol, timeframe, limit=2)
                    if candles:
                        await callback(candles[-1])

                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                self._track_error()
                logger.warning(
                    "REST poll error for %s: %s. Retrying in %.1fs ...",
                    key,
                    exc,
                    interval * 2,
                )
                await asyncio.sleep(interval * 2)

    # ------------------------------------------------------------------
    # Representation
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"<CcxtExchangeClient "
            f"exchange={self._exchange_name} "
            f"market={self._market_type.value} "
            f"status={self._status.value}>"
        )
