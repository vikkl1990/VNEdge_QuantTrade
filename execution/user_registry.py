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
            # BUGFIX 2026-04-21: asyncpg returns `id` as a UUID object, not a
            # string. The previous f"user_trade_{user_id[:8]}" sliced that UUID
            # and raised TypeError _before_ create_task wrapped the coroutine,
            # leaking an un-awaited coro and killing the whole broadcast via
            # the outer except. Normalise to str on every iteration.
            tasks = []
            for user_info in self._active_users_cache:
                user_id = str(user_info["id"])
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

        # Phase 5.20-FIX2 (2026-04-25) — shadow_live uses LIVE keys + PROD endpoint.
        # Audit + user requirement: shadow_live's purpose is to simulate against
        # the REAL prod market. Even though shadow path makes ZERO Delta REST
        # calls (verified by safety audit), the wrapper still needs to be
        # constructed with the correct key/endpoint for any read-only ops
        # (balance fetch for sizing, ticker fallback). Routing to testnet
        # would simulate against testnet prices — the bug we found earlier.
        # Mapping:
        #   bot_mode='demo'        → testnet keys + testnet endpoint
        #   bot_mode='live'        → live keys + prod endpoint
        #   bot_mode='shadow_live' → live keys + prod endpoint (READ-ONLY)
        if bot_mode in ("live", "shadow_live"):
            key_label = "live"
        else:
            key_label = "demo"
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
                        # PRODUCT_MAP entries are dicts — pick demo_id/prod_id
                        # by this client's mode (same fix as UserRealManager).
                        info = PRODUCT_MAP.get(symbol) or {}
                        pid = info.get("demo_id" if self.mode == "demo" else "prod_id")
                        if pid:
                            return self._client.get_ticker(pid)
                    except: pass
                    return {}

            delta = UserDeltaClient(api_key, api_secret, base_url, is_testnet)
        except Exception as e:
            logger.error("Registry: DeltaClient creation failed for user %s: %s", user_id[:8], e)
            return None

        # Build user config from DB fields.
        # BUGFIX 2026-04-21: users.trading_pairs is a jsonb column; asyncpg
        # hands it back as a RAW JSON STRING, not a Python list. The previous
        # `user_info.get("trading_pairs") or []` left the str intact, which
        # made UserRealManager.__init__'s `list(...)` explode it into single
        # characters and then every symbol check (`"BTC/USDT" in ['[','"',...]`)
        # returned False — the user_symbol_filter rejected every real signal.
        raw_pairs = user_info.get("trading_pairs")
        if isinstance(raw_pairs, str):
            try:
                import json as _json
                parsed = _json.loads(raw_pairs)
                pairs_list = parsed if isinstance(parsed, list) else []
            except Exception:
                pairs_list = []
        elif isinstance(raw_pairs, list):
            pairs_list = raw_pairs
        else:
            pairs_list = []

        user_config = {
            "max_leverage": user_info.get("max_leverage", 20),
            "max_daily_loss_usd": float(user_info.get("max_daily_loss_pct", 3.0)) * 100,  # pct → USD estimate
            "max_position_notional": 500,
            "trading_pairs": pairs_list,
            # Phase 5.3.2 (2026-04-23) — T2.2 replay found confidence gate
            # was the #1 admission leak: 279 paper winners ($1,205) rejected
            # in last 7 days, with 79% WR (IDENTICAL to current qualifying
            # WR). Lower threshold captures volume without degrading edge.
            # 55 → 45 is a midpoint; if results are clean after a 48h cohort
            # we can drop further to 40.
            "min_confidence": 45,
            "ml_threshold": 0.55,  # Phase 4.5: lowered from 0.60 to catch B/C winners
            "size_multiplier": 1.0,
            "max_daily_trades": 15,
            "bot_mode": bot_mode,
            # Phase 5.20-FIX (2026-04-25) — pass through new user fields
            "maker_patience_mode": user_info.get("maker_patience_mode") or "standard",
            # Phase 5.20.8 — per-user exit guard policy (Wave 4 A/B)
            "exit_policy": user_info.get("exit_policy") or "current",
            # Phase 6.C — Wave 6.C Lever 2: cohort filter on/off
            "cohort_filter_enabled": bool(user_info.get("cohort_filter_enabled") or False),
            # Phase 6.C — Wave 6.C Lever 1: shadow simulated balance override
            # (re-added 2026-04-26 after audit found this dropped during a rollback;
            # symptom was niranjan getting `ssb=None → using default100` in
            # SIZE_BAL trace because user_config.get('shadow_simulated_balance')
            # returned None even though DB value existed)
            "shadow_simulated_balance": user_info.get("shadow_simulated_balance"),
            # Phase 6.C — Lever 3 Fix #3: mark alignment (paper watermark)
            "mark_alignment_enabled": bool(user_info.get("mark_alignment_enabled") or False),
            # cohort pause is read directly from DB inside _refresh_cohort_blacklist,
            # so no need to pass through here — but log it for visibility:
        }
        _pause_until = user_info.get("cohort_blacklist_paused_until")
        if _pause_until:
            logger.info(
                "Registry: user %s loaded with maker_patience_mode='%s' cohort_pause_until=%s",
                user_id[:8], user_config["maker_patience_mode"], _pause_until,
            )

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

        # Phase 4.2 — reconcile DB-open trades on first manager build
        # (bot restart lost in-memory monitor state). Fire-and-forget.
        # Phase 5.0.3 — ALSO reconcile from exchange-side (orphan positions
        # that were live on Delta but never persisted to DB). Today's
        # incident: 2 BTC longs orphaned during rapid restart cycle,
        # closed manually at net -$0.87 after 17 min of unmonitored risk.
        async def _recon_both():
            try:
                logger.warning("RECON_START: %s (%s) — calling reconcile_open_trades",
                               user_id[:8], user_info.get("email", ""))
                await mgr.reconcile_open_trades()      # DB side
                logger.warning("RECON_DONE: %s (%s) — reconcile_open_trades returned",
                               user_id[:8], user_info.get("email", ""))
            except Exception as exc:
                logger.warning("Registry: DB recon FAIL %s: %s", user_id[:8], exc)
            try:
                await mgr._reconcile_from_exchange()   # Exchange side
            except Exception as exc:
                logger.debug("Registry: exchange recon fail %s: %s", user_id[:8], exc)
        try:
            asyncio.create_task(_recon_both())
        except Exception as exc:
            logger.warning("Registry: reconcile task spawn FAIL for %s: %s", user_id[:8], exc)

        logger.warning("Registry: created manager for user %s (%s)", user_id[:8], user_info.get("email", ""))
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
                # Phase 5.20-FIX (2026-04-25) — pull maker_patience_mode and
                # cohort_blacklist_paused_until so they reach UserRealManager.
                # Bug found: multimode A/B/C silently NOT running because
                # user_config dict didn't include these fields → manager
                # defaulted to 'standard' regardless of DB setting.
                rows = await conn.fetch("""
                    SELECT id, email, role, bot_mode, max_leverage, max_daily_loss_pct,
                           max_open_positions, trading_pairs, preferred_leverage,
                           is_active,
                           maker_patience_mode,
                           cohort_blacklist_paused_until,
                           -- Bug 3c (2026-04-27): the 4 columns below were
                           -- referenced in user_config dict pass-through but
                           -- NOT in this SELECT, so user_info.get(...) always
                           -- returned None → shadow_simulated_balance silently
                           -- ignored → admin sized off $100 default → floored
                           -- at $10 → niranjan looked 5-7x larger by accident.
                           -- A/B's 7.5x niranjan win on delta_shadow was
                           -- inflated by this; real treatment effect TBD after
                           -- this fix lands.
                           exit_policy,
                           cohort_filter_enabled,
                           shadow_simulated_balance,
                           mark_alignment_enabled
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
