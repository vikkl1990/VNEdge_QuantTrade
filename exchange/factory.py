"""
Factory for creating exchange client instances.

Reads the active exchange name from the bot configuration and returns
the appropriate ExchangeBase subclass, fully configured and ready
to ``connect()``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from config import get_config
from config.constants import ExchangeName
from exchange.base import ExchangeBase
from exchange.ccxt_client import CcxtExchangeClient

logger = logging.getLogger(__name__)

# Registry of exchange name -> client class.
# All currently supported exchanges use the CCXT adapter, but the mapping
# makes it trivial to swap in exchange-specific implementations later.
_CLIENT_REGISTRY: Dict[str, type] = {
    ExchangeName.BINANCE.value: CcxtExchangeClient,
    ExchangeName.BYBIT.value:   CcxtExchangeClient,
    ExchangeName.OKX.value:     CcxtExchangeClient,
    ExchangeName.DELTA.value:   CcxtExchangeClient,
}


def create_exchange_client(
    config: Optional[Dict[str, Any]] = None,
) -> ExchangeBase:
    """
    Create and return an exchange client based on the bot configuration.

    Parameters
    ----------
    config : dict, optional
        Full bot config dict.  When *None*, loaded automatically via
        ``get_config()``.

    Returns
    -------
    ExchangeBase
        An uninitialised client -- call ``await client.connect()``
        (or use it as an async context manager) before issuing requests.

    Raises
    ------
    ValueError
        If the configured exchange name is not supported.
    """
    cfg = config or get_config()
    exchange_name: str = cfg["exchange"]["name"].lower()

    client_cls = _CLIENT_REGISTRY.get(exchange_name)
    if client_cls is None:
        raise ValueError(
            f"Unsupported exchange '{exchange_name}'. "
            f"Supported: {sorted(_CLIENT_REGISTRY.keys())}"
        )

    logger.info(
        "Creating exchange client: exchange=%s  market_type=%s  testnet=%s",
        exchange_name,
        cfg["exchange"].get("market_type", "futures"),
        cfg["exchange"].get("testnet", True),
    )

    return client_cls(config=cfg)
