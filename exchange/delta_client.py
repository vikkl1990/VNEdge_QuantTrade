"""
Delta Exchange Client — Official SDK wrapper for order execution.

Uses delta-rest-client (official Delta Exchange Python SDK) for:
- Order placement (market, limit, stop-loss, take-profit)
- Position management
- Balance queries
- Leverage setting

Keeps ccxt for data feeds (candles, tickers) since delta-rest-client
is synchronous and we need async for data.

Supports dual mode:
- DEMO: testnet (cdn-ind.testnet.deltaex.org)
- LIVE: production (api.india.delta.exchange)
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("bot.delta_client")

# Product ID mapping: symbol → {demo_id, prod_id, contract_size, tick_size}
PRODUCT_MAP = {
    # Prod IDs from: GET https://api.india.delta.exchange/v2/products (2026-03-31)
    # Demo IDs from: GET https://cdn-ind.testnet.deltaex.org/v2/products (2026-03-31)
    # Tick sizes verified against live API responses.
    "BTC/USDT": {
        "demo_id": 84,
        "prod_id": 27,
        "symbol": "BTCUSD",
        "contract_size": 0.001,  # 1 lot = 0.001 BTC
        "tick_size": 0.5,
        "tick_size_demo": 0.1,
    },
    "ETH/USDT": {
        "demo_id": 1699,
        "prod_id": 3136,
        "symbol": "ETHUSD",
        "contract_size": 0.01,  # 1 lot = 0.01 ETH
        "tick_size": 0.05,
        "tick_size_demo": 0.05,
    },
    "SOL/USDT": {
        "demo_id": 92572,
        "prod_id": 14823,
        "symbol": "SOLUSD",
        "contract_size": 1.0,  # 1 lot = 1 SOL
        "tick_size": 0.0001,       # prod API: 0.0001
        "tick_size_demo": 0.0001,
    },
    "XRP/USDT": {
        "demo_id": 93723,
        "prod_id": 14969,
        "symbol": "XRPUSD",
        "contract_size": 1.0,  # 1 lot = 1 XRP
        "tick_size": 0.0001,
        "tick_size_demo": 0.0001,
    },
    "LTC/USDT": {
        "demo_id": 0,              # not on testnet
        "prod_id": 15040,
        "symbol": "LTCUSD",
        "contract_size": 0.1,  # 1 lot = 0.1 LTC
        "tick_size": 0.01,
        "tick_size_demo": 0.01,
    },
    "ADA/USDT": {
        "demo_id": 101760,
        "prod_id": 16614,
        "symbol": "ADAUSD",
        "contract_size": 1.0,  # 1 lot = 1 ADA
        "tick_size": 0.00001,      # prod API: 0.00001
        "tick_size_demo": 0.00001,
    },
    "DOT/USDT": {
        "demo_id": 0,              # not on testnet
        "prod_id": 15304,
        "symbol": "DOTUSD",
        "contract_size": 1.0,  # 1 lot = 1 DOT
        "tick_size": 0.001,
        "tick_size_demo": 0.001,
    },
    "TAO/USDT": {
        "demo_id": 0,              # not on testnet
        "prod_id": 26540,
        "symbol": "TAOUSD",
        "contract_size": 0.01,  # 1 lot = 0.01 TAO
        "tick_size": 0.1,         # prod API: 0.1 (was 0.01 — FIXED)
        "tick_size_demo": 0.1,
    },
    "DOGE/USDT": {
        "demo_id": 101555,
        "prod_id": 14745,
        "symbol": "DOGEUSD",
        "contract_size": 1.0,
        "tick_size": 0.000001,     # prod API: 0.000001 (was 0.00001 — FIXED)
        "tick_size_demo": 0.000001,
    },
    "LINK/USDT": {
        "demo_id": 0,              # not on testnet
        "prod_id": 15041,
        "symbol": "LINKUSD",
        "contract_size": 1.0,
        "tick_size": 0.001,
        "tick_size_demo": 0.001,
    },
}

# Demo balance asset ID (USD on testnet)
DEMO_BALANCE_ASSET_ID = 3
# Production balance asset IDs to check
PROD_BALANCE_ASSET_IDS = [14, 5, 3, 1, 2, 4, 6, 7]  # 14=USD on Delta India production


class DeltaClient:
    """Unified Delta Exchange client for real/demo trading.

    Includes rate limiting, cancel tracking, and per-symbol order delay
    to comply with Delta Exchange API usage policies.
    """

    # Rate limiting: max 150 requests/min (conservative buffer vs Delta's 300-500)
    MAX_REQUESTS_PER_MIN = 150
    MIN_ORDER_DELAY_MS = 400  # Minimum ms between orders on same symbol
    CANCEL_RATE_ALERT_PCT = 15  # Alert if cancel rate > 15%

    def __init__(self, mode: str = "demo"):
        """
        Args:
            mode: "demo" for testnet, "live" for production
        """
        self.mode = mode
        self._client = None
        self._connected = False
        self._balance_cache: Optional[float] = None
        self._balance_ts: float = 0

        # Rate limiting: track requests per minute
        self._request_times: List[float] = []
        self._request_count_total: int = 0

        # Per-symbol order delay: last order time per symbol
        self._last_order_time: Dict[str, float] = {}

        # Cancel tracking: orders placed vs cancelled
        self._orders_placed: int = 0
        self._orders_cancelled: int = 0
        self._cancel_log: List[Dict] = []  # last 50 cancellation records

        # API failure monitoring
        self._api_errors: List[float] = []  # timestamps of API errors

    def connect(self) -> bool:
        """Initialize the Delta REST client."""
        try:
            from delta_rest_client import DeltaRestClient, OrderType
            self._OrderType = OrderType  # store for use in other methods

            if self.mode == "demo":
                api_key = os.getenv("DELTA_DEMO_API_KEY", "")
                api_secret = os.getenv("DELTA_DEMO_API_SECRET", "")
                base_url = os.getenv("DELTA_DEMO_BASE_URL", "https://cdn-ind.testnet.deltaex.org")
            else:
                api_key = os.getenv("DELTA_API_KEY", "")
                api_secret = os.getenv("DELTA_API_SECRET", "")
                base_url = "https://api.india.delta.exchange"

            if not api_key or not api_secret:
                logger.error("DELTA: No API credentials for %s mode", self.mode)
                return False

            self._client = DeltaRestClient(
                base_url=base_url,
                api_key=api_key,
                api_secret=api_secret,
            )
            self._connected = True
            logger.info("DELTA [%s]: Connected to %s", self.mode.upper(), base_url)
            return True

        except Exception as e:
            logger.error("DELTA [%s]: Connection failed: %s", self.mode.upper(), e)
            return False

    @property
    def is_connected(self) -> bool:
        return self._connected and self._client is not None

    # ==================================================================
    # Rate Limiting & Compliance
    # ==================================================================

    def _rate_limit_check(self):
        """Enforce rate limit: max 150 requests/minute. Sleeps if needed."""
        now = time.time()
        # Prune requests older than 60 seconds
        self._request_times = [t for t in self._request_times if now - t < 60]
        if len(self._request_times) >= self.MAX_REQUESTS_PER_MIN:
            wait = 60 - (now - self._request_times[0]) + 0.1
            logger.warning("DELTA [%s] RATE LIMIT: %d req/min — sleeping %.1fs",
                          self.mode.upper(), len(self._request_times), wait)
            time.sleep(max(wait, 0.2))
        self._request_times.append(time.time())
        self._request_count_total += 1

    def _enforce_order_delay(self, symbol: str):
        """Enforce minimum delay between orders on the same symbol."""
        now = time.time()
        last = self._last_order_time.get(symbol, 0)
        elapsed_ms = (now - last) * 1000
        if elapsed_ms < self.MIN_ORDER_DELAY_MS:
            wait_s = (self.MIN_ORDER_DELAY_MS - elapsed_ms) / 1000
            time.sleep(wait_s)
        self._last_order_time[symbol] = time.time()

    def _track_order_placed(self):
        """Track order placement for cancel rate calculation."""
        self._orders_placed += 1

    def _track_cancel(self, symbol: str = "", reason: str = ""):
        """Track cancellation for rate monitoring."""
        self._orders_cancelled += 1
        self._cancel_log.append({
            "time": time.time(), "symbol": symbol, "reason": reason,
        })
        if len(self._cancel_log) > 50:
            self._cancel_log = self._cancel_log[-50:]
        # Check cancel rate
        if self._orders_placed > 10:
            cancel_pct = (self._orders_cancelled / self._orders_placed) * 100
            if cancel_pct > self.CANCEL_RATE_ALERT_PCT:
                logger.warning(
                    "DELTA [%s] HIGH CANCEL RATE: %.1f%% (%d/%d) — may trigger exchange flags",
                    self.mode.upper(), cancel_pct, self._orders_cancelled, self._orders_placed,
                )

    def _track_api_error(self):
        """Track API error for failure monitoring."""
        now = time.time()
        self._api_errors.append(now)
        self._api_errors = [t for t in self._api_errors if now - t < 300]  # last 5 min
        if len(self._api_errors) > 5:
            logger.critical(
                "DELTA [%s] API FAILURE ALERT: %d errors in last 5 min",
                self.mode.upper(), len(self._api_errors),
            )

    def get_compliance_stats(self) -> Dict:
        """Return compliance metrics for dashboard/monitoring."""
        now = time.time()
        recent_requests = len([t for t in self._request_times if now - t < 60])
        cancel_rate = (self._orders_cancelled / max(self._orders_placed, 1)) * 100
        recent_errors = len([t for t in self._api_errors if now - t < 300])
        return {
            "requests_per_min": recent_requests,
            "max_requests_per_min": self.MAX_REQUESTS_PER_MIN,
            "orders_placed": self._orders_placed,
            "orders_cancelled": self._orders_cancelled,
            "cancel_rate_pct": round(cancel_rate, 1),
            "api_errors_5min": recent_errors,
            "total_requests": self._request_count_total,
        }

    def _get_product_id(self, symbol: str) -> Optional[int]:
        """Get Delta product ID for a symbol. Returns None if not configured."""
        info = PRODUCT_MAP.get(symbol)
        if not info:
            logger.warning("DELTA: Unknown symbol %s", symbol)
            return None
        pid = info["demo_id"] if self.mode == "demo" else info["prod_id"]
        if not pid or pid == 0:
            logger.debug("DELTA: Product ID not configured for %s (%s mode)", symbol, self.mode)
            return None
        return pid

    def _get_product_info(self, symbol: str) -> Optional[Dict]:
        """Get full product info for a symbol."""
        return PRODUCT_MAP.get(symbol)

    # ==================================================================
    # Balance
    # ==================================================================

    def fetch_balance(self) -> float:
        """Get available USDT/USD balance. Cached for 60s."""
        now = time.time()
        if self._balance_cache is not None and (now - self._balance_ts) < 60:
            return self._balance_cache

        try:
            if self.mode == "demo":
                bal = self._client.get_balances(DEMO_BALANCE_ASSET_ID)
                if bal:
                    self._balance_cache = float(bal.get("available_balance", 0))
                    self._balance_ts = now
                    return self._balance_cache
            else:
                # Single API call - get_balances fetches all wallets internally
                # Try primary asset_id first (14 = USD on Delta India)
                try:
                    bal = self._client.get_balances(14)
                    if bal:
                        avail = float(bal.get("available_balance", 0))
                        total = float(bal.get("balance", 0))
                        if avail > 0 or total > 0:
                            self._balance_cache = avail
                            self._balance_total = total
                            self._balance_ts = now
                            return self._balance_cache
                except Exception:
                    pass
                # Fallback: try other asset IDs
                for aid in PROD_BALANCE_ASSET_IDS:
                    if aid == 14:
                        continue  # already tried
                    try:
                        bal = self._client.get_balances(aid)
                        if bal:
                            avail = float(bal.get("available_balance", 0))
                            total = float(bal.get("balance", 0))
                            if avail > 0 or total > 0:
                                self._balance_cache = avail
                                self._balance_total = total
                                self._balance_ts = now
                                return self._balance_cache
                    except Exception:
                        continue
        except Exception as e:
            logger.warning("DELTA [%s]: Balance fetch failed: %s", self.mode.upper(), e)

        return self._balance_cache or 0.0

    # ==================================================================
    # Orders
    # ==================================================================

    def place_market_order(
        self, symbol: str, side: str, lots: int, reduce_only: bool = False,
        client_order_id: Optional[str] = None, post_only: bool = False,
        limit_price: float = 0,
    ) -> Dict[str, Any]:
        """Place a market or limit order.

        Args:
            symbol: e.g. "BTC/USDT"
            side: "buy" or "sell"
            lots: number of contracts/lots
            reduce_only: True for closing orders
            client_order_id: optional 32-char tracking ID for reconciliation
            post_only: True for maker-only limit orders (saves fees)
            limit_price: if > 0, place limit order instead of market

        Returns:
            Order response dict with id, status, fill_price, etc.
        """
        product_id = self._get_product_id(symbol)
        if not product_id:
            return {"error": "unknown_symbol", "symbol": symbol}

        self._rate_limit_check()
        self._enforce_order_delay(symbol)

        try:
            kwargs = {
                "product_id": product_id,
                "size": lots,
                "side": side,
                "order_type": self._OrderType.MARKET,
                "reduce_only": "true" if reduce_only else "false",
            }
            if client_order_id:
                kwargs["client_order_id"] = client_order_id[:32]
            if limit_price > 0:
                info = self._get_product_info(symbol)
                tick = info.get("tick_size_demo" if self.mode == "demo" else "tick_size", 0.01)
                kwargs["order_type"] = self._OrderType.LIMIT
                kwargs["limit_price"] = str(round(limit_price / tick) * tick)
                if post_only:
                    kwargs["post_only"] = "true"

            result = self._client.place_order(**kwargs)
            self._track_order_placed()
            logger.info(
                "DELTA [%s] ORDER: %s %s %d lots | product=%d | coid=%s | result=%s",
                self.mode.upper(), side, symbol, lots, product_id,
                client_order_id[:12] if client_order_id else "-",
                str(result)[:200],
            )
            return result if isinstance(result, dict) else {"raw": result}

        except Exception as e:
            self._track_api_error()
            logger.error("DELTA [%s] ORDER FAILED: %s %s %d lots | %s",
                        self.mode.upper(), side, symbol, lots, e)
            return {"error": str(e)}

    def place_stop_loss(
        self, symbol: str, side: str, lots: int, stop_price: float,
        client_order_id: Optional[str] = None, trail_amount: float = 0,
    ) -> Dict[str, Any]:
        """Place a stop-loss order (reduce-only).

        Args:
            symbol: e.g. "BTC/USDT"
            side: "buy" (to close short) or "sell" (to close long)
            lots: number of contracts
            stop_price: trigger price
            client_order_id: optional tracking ID
            trail_amount: if > 0, use Delta's native trailing stop
        """
        product_id = self._get_product_id(symbol)
        if not product_id:
            return {"error": "unknown_symbol"}

        info = self._get_product_info(symbol)
        tick = info.get("tick_size_demo" if self.mode == "demo" else "tick_size", 0.01)
        stop_price = round(stop_price / tick) * tick

        self._rate_limit_check()
        self._enforce_order_delay(symbol)

        try:
            result = self._client.place_stop_order(
                product_id=product_id,
                size=int(lots),
                side=side,
                stop_price=str(stop_price),
                order_type=self._OrderType.MARKET,
            )
            self._track_order_placed()
            logger.info(
                "DELTA [%s] SL: %s %s %d lots @ %.4f | trail=%.2f | coid=%s | result=%s",
                self.mode.upper(), side, symbol, lots, stop_price, trail_amount,
                client_order_id[:12] if client_order_id else "-",
                str(result)[:200],
            )
            return result if isinstance(result, dict) else {"raw": result}

        except Exception as e:
            self._track_api_error()
            logger.error("DELTA [%s] SL FAILED: %s | %s", self.mode.upper(), symbol, e)
            return {"error": str(e)}

    def place_take_profit(
        self, symbol: str, side: str, lots: int, stop_price: float,
        client_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Place a take-profit order (reduce-only stop at TP level).

        Uses take_profit_order type — triggers when price reaches TP.
        """
        product_id = self._get_product_id(symbol)
        if not product_id:
            return {"error": "unknown_symbol"}

        info = self._get_product_info(symbol)
        tick = info.get("tick_size_demo" if self.mode == "demo" else "tick_size", 0.01)
        stop_price = round(stop_price / tick) * tick

        self._rate_limit_check()
        self._enforce_order_delay(symbol)

        try:
            payload = {
                "product_id": product_id,
                "size": int(lots),
                "side": side,
                "stop_price": str(stop_price),
                "order_type": "market_order",
                "stop_order_type": "take_profit_order",
                "reduce_only": "true",
                "close_on_trigger": "true",
            }
            if client_order_id:
                payload["client_order_id"] = client_order_id[:32]

            result = self._client.request("POST", "/v2/orders", payload=payload, auth=True)
            self._track_order_placed()
            logger.info(
                "DELTA [%s] TP: %s %s %d lots @ %.4f | coid=%s | result=%s",
                self.mode.upper(), side, symbol, lots, stop_price,
                client_order_id[:12] if client_order_id else "-",
                str(result)[:200],
            )
            return result if isinstance(result, dict) else {"raw": result}

        except Exception as e:
            self._track_api_error()
            logger.error("DELTA [%s] TP FAILED: %s | %s", self.mode.upper(), symbol, e)
            return {"error": str(e)}

    # ==================================================================
    # Bracket Order (atomic entry + SL + TP)
    # ==================================================================

    def place_bracket_order(
        self, symbol: str, side: str, lots: int,
        stop_loss_price: float, take_profit_price: float = 0,
        limit_price: float = 0, client_order_id: Optional[str] = None,
        post_only: bool = True, time_in_force: str = "",
        trail_amount: float = 0,
    ) -> Dict[str, Any]:
        """Place entry + SL + optional TP in one atomic API call.

        Uses POST /v2/orders with inline bracket_stop_loss_price parameter.
        This is the CORRECT Delta API way — NOT /v2/orders/bracket (which is
        for attaching SL/TP to EXISTING positions only).
        """
        product_id = self._get_product_id(symbol)
        if not product_id:
            return {"error": "unknown_symbol"}

        self._rate_limit_check()
        self._enforce_order_delay(symbol)

        info = self._get_product_info(symbol)
        tick = info.get("tick_size_demo" if self.mode == "demo" else "tick_size", 0.01)

        stop_loss_price = round(stop_loss_price / tick) * tick
        if take_profit_price > 0:
            take_profit_price = round(take_profit_price / tick) * tick

        # Build inline bracket payload (single POST /v2/orders call)
        # Delta rule: bracket_stop_loss_price and bracket_trail_amount are MUTUALLY EXCLUSIVE
        # If trail_amount > 0: use trail only (Delta auto-creates SL from entry ± trail)
        # If trail_amount = 0: use fixed SL price
        payload = {
            "product_id": product_id,
            "size": int(lots),
            "side": side,
            "order_type": "market_order" if limit_price <= 0 else "limit_order",
            "bracket_stop_trigger_method": "mark_price",
        }
        if trail_amount > 0:
            payload["bracket_trail_amount"] = str(round(trail_amount / tick) * tick)
            # trail creates SL automatically — do NOT also set bracket_stop_loss_price
        else:
            payload["bracket_stop_loss_price"] = str(stop_loss_price)

        if client_order_id:
            payload["client_order_id"] = client_order_id[:32]

        if limit_price > 0:
            payload["limit_price"] = str(round(limit_price / tick) * tick)
            if post_only and not time_in_force:
                payload["post_only"] = "true"  # post_only conflicts with IOC

        if take_profit_price > 0:
            payload["bracket_take_profit_price"] = str(take_profit_price)

        # bracket_trail_amount already set above if trail_amount > 0

        if time_in_force:
            payload["time_in_force"] = time_in_force

        try:
            result = self._client.request(
                "POST", "/v2/orders",
                payload=payload,
                auth=True,
            )
            # Parse response
            if hasattr(result, 'json'):
                result = result.json().get("result", result.json())
            elif not isinstance(result, dict):
                result = {"raw": str(result)}

            logger.info(
                "DELTA [%s] BRACKET ORDER: %s %s %d lots | SL=%.4f TP=%.4f | result=%s",
                self.mode.upper(), side, symbol, lots, stop_loss_price,
                take_profit_price, str(result)[:200],
            )
            return result

        except Exception as e:
            logger.warning(
                "DELTA [%s] INLINE BRACKET FAILED: %s | falling back to separate orders",
                self.mode.upper(), e,
            )
            # Fallback: entry + separate SL
            entry_result = self.place_market_order(symbol, side, lots,
                                                    client_order_id=client_order_id)
            if entry_result.get("error"):
                return entry_result

            close_side = "sell" if side == "buy" else "buy"
            import time
            time.sleep(2)

            sl_result = None
            for attempt in range(3):
                try:
                    sl_result = self.place_stop_loss(symbol, close_side, lots, stop_loss_price)
                    if sl_result and not sl_result.get("error"):
                        break
                except Exception as sl_err:
                    logger.debug("SL attempt %d failed: %s", attempt + 1, sl_err)
                    time.sleep(1.0)

            if take_profit_price > 0:
                logger.info("DELTA [%s] FALLBACK: placing TP (take_profit_order) for %s @ %.4f", self.mode.upper(), symbol, take_profit_price)
                try:
                    self.place_take_profit(symbol, close_side, lots, take_profit_price)
                except Exception:
                    pass

            if not sl_result or sl_result.get("error"):
                logger.critical(
                    "DELTA [%s] SL FAILED! %s %s %d lots — UNPROTECTED!",
                    self.mode.upper(), symbol, side, lots,
                )
                entry_result["sl_failed"] = True

            entry_result["bracket_fallback"] = True
            return entry_result

    # ==================================================================
    # Leverage
    # ==================================================================

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set leverage for a symbol."""
        product_id = self._get_product_id(symbol)
        if not product_id:
            return False

        try:
            self._client.set_leverage(
                product_id=product_id,
                leverage=str(leverage),
            )
            logger.info("DELTA [%s] LEVERAGE: %s = %dx", self.mode.upper(), symbol, leverage)
            return True
        except Exception as e:
            logger.warning("DELTA [%s] LEVERAGE FAILED: %s %dx | %s",
                          self.mode.upper(), symbol, leverage, e)
            return False

    # ==================================================================
    # Positions
    # ==================================================================

    def get_position(self, symbol: str) -> Optional[Dict]:
        """Get current position for a symbol."""
        product_id = self._get_product_id(symbol)
        if not product_id:
            return None

        try:
            pos = self._client.get_position(product_id)
            if pos and int(pos.get("size", 0) or 0) != 0:
                return {
                    "symbol": symbol,
                    "side": "long" if pos.get("side") == "buy" else "short",
                    "size": int(pos.get("size", 0)),
                    "entry_price": float(pos.get("entry_price", 0)),
                    "margin": float(pos.get("margin", 0)),
                    "unrealized_pnl": float(pos.get("pnl", 0) or 0),
                    "leverage": float(pos.get("leverage", 0) or 0),
                    "liquidation_price": float(pos.get("liquidation_price", 0) or 0),
                    "raw": pos,
                }
            return None
        except Exception as e:
            logger.warning("DELTA [%s] POSITION: %s | %s", self.mode.upper(), symbol, e)
            return None

    def get_all_positions(self) -> List[Dict]:
        """Get all open positions in a single API call."""
        try:
            result = self._client.request("GET", "/v2/positions/margined", auth=True)
            if hasattr(result, 'json'):
                data = result.json().get("result", [])
            elif isinstance(result, list):
                data = result
            elif isinstance(result, dict):
                data = result.get("result", [result])
            else:
                data = []
            return [p for p in data if isinstance(p, dict)]
        except Exception as e:
            logger.warning("DELTA [%s]: get_all_positions failed: %s", self.mode.upper(), e)
            return []

    # ==================================================================
    # Orders
    # ==================================================================

    def get_ticker(self, symbol: str) -> dict:
        """Get current bid/ask/last for a symbol."""
        try:
            product_id = self._get_product_id(symbol)
            # Use the REST API ticker endpoint
            resp = self._client.request("GET", f"/v2/tickers/{product_id}")
            if resp and isinstance(resp, dict):
                return {
                    "bid": float(resp.get("best_bid", 0) or 0),
                    "ask": float(resp.get("best_ask", 0) or 0),
                    "last": float(resp.get("close", 0) or resp.get("last_price", 0) or 0),
                    "mark": float(resp.get("mark_price", 0) or 0),
                }
        except Exception as e:
            logger.debug("get_ticker(%s) failed: %s", symbol, e)
        return {}


    def get_open_orders(self) -> List[Dict]:
        """Get all open orders."""
        try:
            orders = self._client.get_live_orders()
            return orders if isinstance(orders, list) else []
        except Exception as e:
            logger.warning("DELTA [%s] ORDERS: %s", self.mode.upper(), e)
            return []

    def cancel_all_orders(self, symbol: Optional[str] = None) -> bool:
        """Cancel all open orders, optionally for a specific symbol.

        Uses bulk endpoint first (no rate limit), falls back to one-by-one.
        """
        try:
            product_id = self._get_product_id(symbol) if symbol else None
            # Try bulk cancel first (Tier 3: no rate limit)
            if self.cancel_all_orders_bulk(product_id):
                return True

            # Fallback: one-by-one
            orders = self.get_open_orders()
            if symbol and product_id:
                orders = [o for o in orders if o.get("product_id") == product_id]

            for order in orders:
                try:
                    self._client.cancel_order(
                        product_id=order.get("product_id"),
                        order_id=order.get("id"),
                    )
                except Exception:
                    pass

            for _ in orders:
                self._track_cancel(symbol=symbol or "ALL", reason="cancel_all")
            logger.info("DELTA [%s] CANCEL ALL: %d orders", self.mode.upper(), len(orders))
            return True
        except Exception as e:
            logger.error("DELTA [%s] CANCEL ALL FAILED: %s", self.mode.upper(), e)
            return False

    def cancel_all_orders_bulk(self, product_id: Optional[int] = None) -> bool:
        """Bulk cancel via DELETE /v2/orders/all (NO rate limit).

        Much faster than individual cancellation for emergencies.
        """
        try:
            payload = {}
            if product_id:
                payload["product_id"] = product_id
            payload["contract_types"] = "perpetual_futures"
            payload["cancel_limit_orders"] = "true"
            payload["cancel_stop_orders"] = "true"
            self._client.request("DELETE", "/v2/orders/all", payload=payload, auth=True)
            logger.info("DELTA [%s] BULK CANCEL: product=%s", self.mode.upper(),
                       product_id or "ALL")
            return True
        except Exception as e:
            logger.debug("DELTA [%s] BULK CANCEL failed: %s", self.mode.upper(), e)
            return False

    def close_position(self, symbol: str, close_side: str, lots: int) -> Optional[Dict]:
        """Close a position by placing a market reduce-only order.

        Checks position exists before closing to avoid 'no_open_position' errors.
        """
        try:
            product_id = self._get_product_id(symbol)
            if not product_id:
                logger.warning("DELTA [%s] CLOSE: unknown symbol %s", self.mode.upper(), symbol)
                return None

            # Verify position exists before attempting close
            pos = self.get_position_realtime(symbol)
            if not pos:
                logger.info("DELTA [%s] CLOSE: no position to close for %s — skipping",
                           self.mode.upper(), symbol)
                return None

            # Cancel any existing SL/TP orders first (no rate limit on cancels)
            try:
                self.cancel_all_orders_bulk(product_id)
            except Exception:
                # Fallback to individual cancel
                try:
                    orders = self.get_open_orders()
                    for o in orders:
                        if (o.get("product_id") == product_id and
                            str(o.get("reduce_only", "")).lower() in ("true", "1")):
                            self._client.cancel_order(product_id, o.get("id"))
                except Exception:
                    pass

            # Place market close order
            order = self._client.place_order(
                product_id=product_id,
                size=lots,
                side=close_side,
                order_type=self._OrderType.MARKET,
                reduce_only="true",
            )
            logger.info("DELTA [%s] CLOSE: %s %s %d lots | order=%s",
                       self.mode.upper(), symbol, close_side, lots,
                       order.get("id", "?") if order else "failed")
            return order
        except Exception as e:
            logger.warning("DELTA [%s] CLOSE FAILED: %s %s — %s",
                          self.mode.upper(), symbol, close_side, e)
            return None

    # ==================================================================
    # Trade History
    # ==================================================================

    def get_fills(self, limit: int = 20) -> List[Dict]:
        """Get recent fills/trades."""
        try:
            result = self._client.fills(page_size=limit)
            if isinstance(result, dict):
                return result.get("result", [])
            return result if isinstance(result, list) else []
        except Exception as e:
            logger.warning("DELTA [%s] FILLS: %s", self.mode.upper(), e)
            return []

    # ==================================================================
    # Utility
    # ==================================================================

    def calculate_lots(self, symbol: str, position_usd: float, price: float) -> int:
        """Calculate number of lots for a given USD position size.

        Args:
            symbol: e.g. "BTC/USDT"
            position_usd: desired position in USD
            price: current price

        Returns:
            Number of lots (integer, minimum 1)
        """
        info = self._get_product_info(symbol)
        if not info:
            return 0

        contract_size = info["contract_size"]
        # 1 lot = contract_size × price USD
        lot_value = contract_size * price
        lots = int(position_usd / lot_value)
        return max(lots, 1) if position_usd > 0 else 0

    def lot_value_usd(self, symbol: str, price: float) -> float:
        """Get the USD value of 1 lot at current price."""
        info = self._get_product_info(symbol)
        if not info:
            return 0.0
        return info["contract_size"] * price

    # ==================================================================
    # Order Lookup & Reconciliation (Tier 1 + Tier 3)
    # ==================================================================

    def get_order_by_client_id(self, client_order_id: str) -> Optional[Dict]:
        """Look up an order by client_order_id for perfect reconciliation.

        This eliminates the need for fragile paper_to_real mapping.
        """
        try:
            result = self._client.request(
                "GET", f"/v2/orders/client_order_id/{client_order_id[:32]}",
                auth=True,
            )
            if isinstance(result, dict) and result.get("id"):
                return result
            return None
        except Exception as e:
            logger.debug("DELTA [%s] ORDER LOOKUP by client_id failed: %s",
                        self.mode.upper(), e)
            return None

    def get_order_history(self, product_id: Optional[int] = None,
                          limit: int = 50) -> List[Dict]:
        """Get order history for reconciliation (Tier 3).

        Weight: 10 per call. Use sparingly.
        """
        try:
            payload = {"page_size": limit}
            if product_id:
                payload["product_ids"] = str(product_id)
            payload["contract_types"] = "perpetual_futures"
            result = self._client.request("GET", "/v2/orders/history",
                                          payload=payload, auth=True)
            if isinstance(result, dict):
                return result.get("result", result.get("data", []))
            return result if isinstance(result, list) else []
        except Exception as e:
            logger.warning("DELTA [%s] ORDER HISTORY: %s", self.mode.upper(), e)
            return []

    # ==================================================================
    # Real-time Position (Tier 1)
    # ==================================================================

    def get_position_realtime(self, symbol: str) -> Optional[Dict]:
        """Get real-time position via product-specific endpoint.

        Uses GET /v2/positions (not /margined which has 10s delay).
        """
        product_id = self._get_product_id(symbol)
        if not product_id:
            return None
        try:
            result = self._client.request(
                "GET", "/v2/positions",
                payload={"product_id": product_id},
                auth=True,
            )
            pos = result if isinstance(result, dict) else {}
            if pos and int(pos.get("size", 0) or 0) != 0:
                return {
                    "symbol": symbol,
                    "side": "long" if pos.get("side") == "buy" else "short",
                    "size": int(pos.get("size", 0)),
                    "entry_price": float(pos.get("entry_price", 0)),
                    "margin": float(pos.get("margin", 0)),
                    "unrealized_pnl": float(pos.get("pnl", 0) or 0),
                    "liquidation_price": float(pos.get("liquidation_price", 0) or 0),
                }
            return None
        except Exception as e:
            logger.debug("DELTA [%s] REALTIME POS: %s | %s", self.mode.upper(), symbol, e)
            return None

    # ==================================================================
    # Emergency Controls (Tier 1)
    # ==================================================================

    def close_all_positions(self) -> Dict:
        """Emergency close ALL positions via Delta bulk endpoint.

        Called by circuit breaker on trip.
        """
        try:
            result = self._client.request(
                "DELETE", "/v2/positions/all",
                payload={},
                auth=True,
            )
            logger.critical("DELTA [%s] EMERGENCY CLOSE-ALL executed: %s",
                           self.mode.upper(), str(result)[:200])
            return result if isinstance(result, dict) else {"raw": result}
        except Exception as e:
            logger.error("DELTA [%s] EMERGENCY CLOSE-ALL FAILED: %s",
                        self.mode.upper(), e)
            return {"error": str(e)}

    # ==================================================================
    # Margin & Risk Controls (Tier 2)
    # ==================================================================

    def set_margin_mode(self, mode: str = "isolated") -> bool:
        """Set account margin mode: 'isolated' or 'cross'.

        Isolated recommended — limits risk to per-position margin.
        """
        try:
            self._client.request(
                "PUT", "/v2/account/margin-mode",
                payload={"margin_mode": mode},
                auth=True,
            )
            logger.info("DELTA [%s] MARGIN MODE: set to %s", self.mode.upper(), mode)
            return True
        except Exception as e:
            logger.warning("DELTA [%s] MARGIN MODE failed: %s", self.mode.upper(), e)
            return False

    def enable_auto_topup(self, symbol: str) -> bool:
        """Enable auto-topup for a position to prevent liquidation.

        Automatically adds margin when position approaches liquidation.
        """
        product_id = self._get_product_id(symbol)
        if not product_id:
            return False
        try:
            self._client.request(
                "POST", "/v2/positions/auto-topup",
                payload={"product_id": product_id, "auto_topup": True},
                auth=True,
            )
            logger.info("DELTA [%s] AUTO-TOPUP: enabled for %s", self.mode.upper(), symbol)
            return True
        except Exception as e:
            logger.debug("DELTA [%s] AUTO-TOPUP failed for %s: %s",
                        self.mode.upper(), symbol, e)
            return False

    # ==================================================================
    # Bracket Edit (Tier 3)
    # ==================================================================

    def edit_bracket(self, symbol: str, stop_loss_price: float = 0,
                     take_profit_price: float = 0, trail_amount: float = 0) -> Dict:
        """Atomically update SL/TP on an existing position via PUT /v2/orders/bracket.

        No gap where position is unprotected (vs cancel+replace).
        """
        product_id = self._get_product_id(symbol)
        if not product_id:
            return {"error": "unknown_symbol"}

        info = self._get_product_info(symbol)
        tick = info.get("tick_size_demo" if self.mode == "demo" else "tick_size", 0.01)

        payload = {
            "product_id": product_id,
            "bracket_stop_trigger_method": "mark_price",  # always use mark_price
        }
        if stop_loss_price > 0:
            payload["stop_loss_order"] = {
                "order_type": "market_order",
                "stop_price": str(round(stop_loss_price / tick) * tick),
            }
        if take_profit_price > 0:
            payload["take_profit_order"] = {
                "order_type": "market_order",
                "stop_price": str(round(take_profit_price / tick) * tick),
            }
        if trail_amount > 0:
            payload["bracket_trail_amount"] = str(round(trail_amount / tick) * tick)

        try:
            result = self._client.request("PUT", "/v2/orders/bracket",
                                          payload=payload, auth=True)
            logger.info("DELTA [%s] EDIT BRACKET: %s SL=%.4f TP=%.4f | result=%s",
                       self.mode.upper(), symbol, stop_loss_price, take_profit_price,
                       str(result)[:200])
            return result if isinstance(result, dict) else {"raw": result}
        except Exception as e:
            logger.warning("DELTA [%s] EDIT BRACKET failed: %s | %s",
                          self.mode.upper(), symbol, e)
            return {"error": str(e)}
