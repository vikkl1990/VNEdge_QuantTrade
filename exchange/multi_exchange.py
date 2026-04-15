"""Multi-exchange adapter — abstracts Delta/Bybit/Binance behind common interface."""
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger("exchange.multi")


class ExchangeAdapter:
    """Abstract base for exchange clients. Implementations: Delta, Bybit, Binance."""

    EXCHANGE_NAME = "abstract"

    def __init__(self, api_key: str, api_secret: str, testnet: bool = True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet

    async def fetch_balance(self) -> float:
        raise NotImplementedError

    async def place_order(self, symbol: str, side: str, qty: float, order_type: str = "market") -> Dict:
        raise NotImplementedError

    async def cancel_order(self, order_id: str) -> bool:
        raise NotImplementedError

    async def get_positions(self) -> list:
        raise NotImplementedError

    async def get_ticker(self, symbol: str) -> Dict:
        raise NotImplementedError


class DeltaAdapter(ExchangeAdapter):
    EXCHANGE_NAME = "delta"

    def __init__(self, api_key, api_secret, testnet=True):
        super().__init__(api_key, api_secret, testnet)
        from delta_rest_client import DeltaRestClient
        url = "https://cdn-ind.testnet.deltaex.org" if testnet else "https://api.india.delta.exchange"
        self._client = DeltaRestClient(base_url=url, api_key=api_key, api_secret=api_secret)

    async def fetch_balance(self) -> float:
        try:
            wallets = self._client.get_balances()
            for w in wallets or []:
                if w.get("asset_symbol") == "USDT":
                    return float(w.get("available_balance", 0) or 0)
        except Exception as e:
            logger.warning("Delta balance fetch failed: %s", e)
        return 0.0


class BybitAdapter(ExchangeAdapter):
    """Stub Bybit adapter — implementation pending."""
    EXCHANGE_NAME = "bybit"

    async def fetch_balance(self) -> float:
        logger.warning("Bybit adapter not yet implemented")
        return 0.0


class BinanceAdapter(ExchangeAdapter):
    """Stub Binance adapter — implementation pending."""
    EXCHANGE_NAME = "binance"

    async def fetch_balance(self) -> float:
        logger.warning("Binance adapter not yet implemented")
        return 0.0


def get_adapter(exchange: str, api_key: str, api_secret: str, testnet: bool = True) -> ExchangeAdapter:
    """Factory: return the right adapter for the named exchange."""
    exchange = (exchange or "delta").lower()
    if exchange == "delta":
        return DeltaAdapter(api_key, api_secret, testnet)
    elif exchange == "bybit":
        return BybitAdapter(api_key, api_secret, testnet)
    elif exchange == "binance":
        return BinanceAdapter(api_key, api_secret, testnet)
    raise ValueError(f"Unknown exchange: {exchange}")
