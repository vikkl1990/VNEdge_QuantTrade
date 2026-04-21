"""Shared helper for fetching a user's USD-denominated Delta balance.

Delta India uses `asset_symbol='USD'` (with different asset_ids per
environment: 14 on prod, 3 on testnet) — NOT 'USDT' like some other
exchanges. The 6 original callers hardcoded `asset_id=5` which doesn't
exist on Delta India → balance queries silently returned None →
dashboard showed $0 for users with actual funded accounts.

This helper:
  1. Fetches ALL wallets via /v2/wallet/balances
  2. Picks the first non-zero entry whose asset_symbol is USD or USDT
  3. Returns available_balance as float (0.0 if none found)

Usage:
    from exchange.delta_balance import fetch_usd_balance
    bal = fetch_usd_balance(api_key, api_secret, base_url)  # blocking call
"""
from __future__ import annotations

import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

# Order matters: USD first (Delta India native), then USDT (for other exchanges)
_PREFERRED_SYMBOLS = ("USD", "USDT")


def fetch_usd_balance(
    api_key: str, api_secret: str, base_url: str
) -> float:
    """Probe Delta for the user's USD/USDT balance.

    Returns the first non-zero available_balance whose asset_symbol is
    in _PREFERRED_SYMBOLS, or 0.0 if none found. Exceptions propagate
    to the caller so they can categorise (key_rejected vs unreachable).

    NOTE: intentionally blocking — call from asyncio.to_thread in async
    contexts to avoid blocking the event loop.
    """
    from delta_rest_client import DeltaRestClient
    client = DeltaRestClient(base_url=base_url, api_key=api_key, api_secret=api_secret)

    # Delta library's get_balances(asset_id) takes a REQUIRED asset_id + filters
    # internally to a single asset. We instead fetch all wallets via the raw
    # request and scan for any USD-denominated entry.
    raw = client.request("GET", "/v2/wallet/balances", auth=True)

    wallets: Optional[List] = None
    try:
        body = raw.json() if hasattr(raw, "json") else raw
    except Exception:
        body = raw

    # Library sometimes returns the list directly, sometimes wrapped in
    # {"success": true, "result": [...]}. Handle both shapes.
    if isinstance(body, dict):
        if "result" in body and isinstance(body["result"], list):
            wallets = body["result"]
        else:
            # Single-wallet dict (e.g. when Delta returns a flat record)
            wallets = [body]
    elif isinstance(body, list):
        wallets = body

    if not wallets:
        return 0.0

    best_bal = 0.0
    for sym in _PREFERRED_SYMBOLS:
        for w in wallets:
            if not isinstance(w, dict):
                continue
            if w.get("asset_symbol") == sym:
                try:
                    bal = float(w.get("available_balance") or 0)
                except (ValueError, TypeError):
                    continue
                if bal > 0:
                    return bal
                if bal > best_bal:
                    best_bal = bal
    return best_bal


def fetch_all_wallets(
    api_key: str, api_secret: str, base_url: str
) -> list:
    """Return the full list of wallet entries (for debugging / rich display).
    Each entry is a dict with asset_symbol, asset_id, available_balance, etc.
    """
    from delta_rest_client import DeltaRestClient
    client = DeltaRestClient(base_url=base_url, api_key=api_key, api_secret=api_secret)
    raw = client.request("GET", "/v2/wallet/balances", auth=True)
    try:
        body = raw.json() if hasattr(raw, "json") else raw
    except Exception:
        body = raw
    if isinstance(body, dict):
        return body.get("result", [body])
    return body if isinstance(body, list) else []
