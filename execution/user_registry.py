"""
UserRealRegistry — Manages per-user RealManager instances for VN Edge.

Lazy-loads user managers when signals are broadcast. Each active user
with configured API keys gets their own independent real trading manager.
Paper signals are shared; real execution is per-user.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("execution.user_registry")


class UserRealRegistry:
    """Manages per-user RealManager instances. Lazy-loaded on first signal.

    Architecture:
    1. Orchestrator calls broadcast_signal(signal) for every qualified paper signal
    2. Registry loads active users from DB (cached, refreshed every 60s)
    3. For each active user with API keys: get_or_create UserRealManager
    4. Fire-and-forget: user_manager.execute_signal(signal)
    5. Each user's trade runs independently
    """

    def __init__(self, db_pool: Any):
        self._db_pool = db_pool
        self._managers: Dict[str, Any] = {}  # user_id → UserRealManager
        self._active_users_cache: List[Dict] = []
        self._last_user_refresh: float = 0
        self._user_refresh_interval: float = 60.0  # refresh active users every 60s
        self._price_feed = None  # set by orchestrator for WS price access
        self._initialized = False

        logger.info("UserRealRegistry created")

    async def initialize(self):
        """Load active users on startup."""
        if self._initialized:
            return
        await self._refresh_active_users()
        self._initialized = True
        logger.info("UserRealRegistry initialized: %d active users", len(self._active_users_cache))

    def set_price_feed(self, orchestrator):
        """Set orchestrator reference for WS price access."""
        self._price_feed = orchestrator

    # ══════════════════════════════════════════════════════════════
    # BROADCAST SIGNAL TO ALL ACTIVE USERS
    # ══════════════════════════════════════════════════════════════

    async def broadcast_signal(self, signal: dict):
        """Send a qualified paper signal to ALL active users' real managers.

        Called by orchestrator._process_signal() as fire-and-forget.
        Each user's execution runs independently — one user's failure
        doesn't affect others.
        """
        try:
            # Refresh user list periodically
            if time.time() - self._last_user_refresh > self._user_refresh_interval:
                await self._refresh_active_users()

            if not self._active_users_cache:
                return

            # Fire to each active user in parallel
            tasks = []
            for user_info in self._active_users_cache:
                user_id = user_info["id"]
                try:
                    mgr = await self.get_or_create_manager(user_info)
                    if mgr and mgr.enabled:
                        tasks.append(
                            asyncio.create_task(
                                self._safe_execute(mgr, signal),
                                name=f"user_trade_{user_id[:8]}",
                            )
                        )
                except Exception as e:
                    logger.error("Registry: failed to get manager for %s: %s", user_id[:8], e)

            if tasks:
                logger.debug("Registry: broadcasting signal to %d users", len(tasks))

        except Exception as e:
            logger.error("Registry broadcast_signal error: %s", e)

    async def _safe_execute(self, mgr, signal: dict):
        """Execute signal for a user with error isolation."""
        try:
            await mgr.execute_signal(signal)
        except Exception as e:
            logger.error("Registry: user %s execution failed: %s", mgr.user_id[:8], e)

    # ══════════════════════════════════════════════════════════════
    # USER MANAGER LIFECYCLE
    # ══════════════════════════════════════════════════════════════

    async def get_or_create_manager(self, user_info: dict):
        """Get existing or create new UserRealManager for a user.

        Lazy-loads: creates DeltaClient from user's encrypted API keys,
        configures risk limits from user's DB settings.

        BUGFIX 2026-04-21: asyncpg returns `id` as a UUID object, not str.
        All `user_id[:8]` slices on it raise TypeError, which propagated
        up and made callers treat the create as a failure → UserRealManager
        was orphaned in self._managers AND never returned. Normalise to
        str at the top of the function so every log + dict key uses the
        same representation.
        """
        user_id = str(user_info["id"])

        # Return cached manager if exists
        if user_id in self._managers:
            return self._managers[user_id]

        # Load user's API keys from DB
        api_keys = await self._load_user_api_keys(user_id)
        if not api_keys:
            logger.debug("Registry: no API keys for user %s", user_id[:8])
            return None

        # Determine which key to use based on bot_mode.
        # SEC FIX (2026-04-19): STRICT LABEL MATCH — no "any active key" fallback.
        # Previously, if a user had bot_mode=live but only a demo key, the code
        # silently fell through to the demo key and executed "live" trades on
        # testnet (confusing), OR vice versa (catastrophic — demo user trading
        # real money). Now: require exact label match. If missing, return None
        # and the /api/user/real/toggle handler refuses the mode flip upfront.
        bot_mode = user_info.get("bot_mode", "paper")
        if bot_mode == "paper":
            return None  # Paper-only user, no real manager needed

        key_label = "live" if bot_mode == "live" else "demo"
        key_data = None
        for k in api_keys:
            if k["label"] == key_label and k["is_active"]:
                key_data = k
                break

        if not key_data:
            logger.warning(
                "Registry: user %s bot_mode=%s but no active '%s' key — "
                "refusing to use mismatched key. Ask user to upload a %s-labeled key.",
                user_id[:8], bot_mode, key_label, key_label,
            )
            return None

        # Decrypt API keys — per-user cipher first, falls back to legacy master key.
        try:
            from auth.crypto import decrypt_api_key
            api_key = decrypt_api_key(key_data["api_key_enc"], user_id=str(user_id))
            api_secret = decrypt_api_key(key_data["api_secret_enc"], user_id=str(user_id))
            base_url = key_data.get("base_url", "")
        except Exception as e:
            logger.error("Registry: key decryption failed for user %s: %s", user_id[:8], e)
            return None

        # Create DeltaClient for this user
        try:
            from delta_rest_client import DeltaRestClient
            is_testnet = "testnet" in (base_url or "") or key_label == "demo"
            if not base_url:
                base_url = "https://cdn-ind.testnet.deltaex.org" if is_testnet else "https://api.india.delta.exchange"

            # Create a lightweight wrapper with the user's own keys
            class UserDeltaClient:
                def __init__(self, ak, sk, url, testnet):
                    self._client = DeltaRestClient(base_url=url, api_key=ak, api_secret=sk)
                    self.mode = "demo" if testnet else "live"
                    self._connected = True
                def connect(self): return True
                def fetch_balance(self):
                    # Delta India uses asset_symbol='USD' (not USDT).
                    # Fetch all wallets and pick the first USD-denominated.
                    try:
                        from exchange.delta_balance import fetch_usd_balance
                        return fetch_usd_balance(api_key, api_secret, base_url)
                    except Exception:
                        return 0
                def get_ticker(self, symbol):
                    try:
                        from exchange.delta_client import PRODUCT_MAP
                        pid = PRODUCT_MAP.get(symbol)
                        if pid:
                            return self._client.get_ticker(pid)
                    except: pass
                    return {}

            delta = UserDeltaClient(api_key, api_secret, base_url, is_testnet)
        except Exception as e:
            logger.error("Registry: DeltaClient creation failed for user %s: %s", user_id[:8], e)
            return None

        # Build user config from DB fields
        user_config = {
            "max_leverage": user_info.get("max_leverage", 20),
            "max_daily_loss_usd": float(user_info.get("max_daily_loss_pct", 3.0)) * 100,  # pct → USD estimate
            "max_position_notional": 500,
            "trading_pairs": user_info.get("trading_pairs") or [],
            "min_confidence": 55,
            "ml_threshold": 0.60,
            "size_multiplier": 1.0,
            "max_daily_trades": 15,
            "bot_mode": bot_mode,
        }

        # Create manager
        from execution.user_real_manager import UserRealManager
        mgr = UserRealManager(
            user_id=str(user_id),
            user_email=user_info.get("email", ""),
            user_config=user_config,
            delta_client=delta,
            db_pool=self._db_pool,
        )
        mgr._price_feed = self._price_feed

        self._managers[user_id] = mgr
        logger.info("Registry: created manager for user %s (%s)", user_id[:8], user_info.get("email", ""))
        return mgr

    async def get_manager_for_user(self, user_id: str):
        """Get a specific user's manager (for API endpoints)."""
        return self._managers.get(user_id)

    # ══════════════════════════════════════════════════════════════
    # DB QUERIES
    # ══════════════════════════════════════════════════════════════

    async def _refresh_active_users(self):
        """Load active users who have bot_mode != 'paper' from DB."""
        try:
            async with self._db_pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT id, email, role, bot_mode, max_leverage, max_daily_loss_pct,
                           max_open_positions, trading_pairs, preferred_leverage,
                           is_active
                    FROM users
                    WHERE is_active = TRUE AND bot_mode != 'paper'
                    ORDER BY created_at
                """)
                self._active_users_cache = [dict(r) for r in rows]
                self._last_user_refresh = time.time()

                # Remove managers for users no longer active
                active_ids = {str(r["id"]) for r in rows}
                stale = [uid for uid in self._managers if uid not in active_ids]
                for uid in stale:
                    logger.info("Registry: removing stale manager for %s", uid[:8])
                    del self._managers[uid]

        except Exception as e:
            logger.error("Registry: failed to refresh active users: %s", e)

    async def _load_user_api_keys(self, user_id: str) -> List[Dict]:
        """Load a user's API keys from DB (encrypted)."""
        try:
            async with self._db_pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT id, exchange, label, api_key_enc, api_secret_enc,
                           base_url, is_active
                    FROM user_api_keys
                    WHERE user_id = $1 AND exchange = 'delta'
                    ORDER BY label
                """, user_id)
                return [dict(r) for r in rows]
        except Exception as e:
            logger.error("Registry: failed to load API keys for %s: %s", str(user_id)[:8], e)
            return []

    # ══════════════════════════════════════════════════════════════
    # ADMIN QUERIES
    # ══════════════════════════════════════════════════════════════

    def get_all_status(self) -> List[Dict]:
        """Return status of all active user managers (for admin dashboard)."""
        return [mgr.get_status() for mgr in self._managers.values()]

    async def shutdown(self):
        """Gracefully close all user managers."""
        logger.info("Registry: shutting down %d user managers", len(self._managers))
        self._managers.clear()
