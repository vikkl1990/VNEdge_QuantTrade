"""Trade replay + performance attribution endpoints.

Multi-tenancy (SEC FIX 2026-04-19): both endpoints now scope results to the
authenticated user. Admins (role == 'admin') retain system-wide visibility.
Non-admin users see only their own trades / attribution; enumeration attacks
on trade_id now return 404 (indistinguishable from a non-existent trade).
"""
import json
import logging
from aiohttp import web

logger = logging.getLogger("dashboard.replay")


def _get_user_ctx(request: web.Request) -> tuple:
    """Extract (user_id, role) from the auth middleware's session.
    Returns (None, None) when not authenticated — callers must 401 in that case.
    """
    user = request.get("user") or {}
    uid = user.get("user_id") or ""
    role = user.get("role") or ""
    return (uid if uid else None), role


def register_replay_routes(app: web.Application, db_pool):
    """Register trade replay + attribution endpoints."""

    async def handle_trade_replay(request: web.Request) -> web.Response:
        """GET /api/replay/trade/{trade_id} — full timeline of a closed trade.

        Scoped: non-admins can only replay their OWN trades. If the trade_id
        exists but belongs to another user, returns 404 (not 403) to avoid
        leaking the existence of other users' trade IDs to an enumerator.
        """
        user_id, role = _get_user_ctx(request)
        if not user_id:
            return web.json_response({"error": "unauthorized"}, status=401)

        trade_id = request.match_info.get("trade_id", "")
        if not trade_id:
            return web.json_response({"error": "trade_id required"}, status=400)

        async with db_pool.acquire() as conn:
            if role == "admin":
                row = await conn.fetchrow("""
                    SELECT id, user_id, symbol, side, entry_price, exit_price,
                           pnl_usd, fees_usd, opened_at, closed_at, signal_data, metadata
                    FROM user_trades WHERE id = $1
                """, trade_id)
            else:
                # Non-admin: must own the trade. Any mismatch → 404 (indistinguishable
                # from a non-existent trade, so an enumerator can't discover that
                # trade_id X exists under some other user).
                row = await conn.fetchrow("""
                    SELECT id, user_id, symbol, side, entry_price, exit_price,
                           pnl_usd, fees_usd, opened_at, closed_at, signal_data, metadata
                    FROM user_trades WHERE id = $1 AND user_id = $2
                """, trade_id, user_id)

            if not row:
                return web.json_response({"error": "trade not found"}, status=404)
        t = dict(row)
        t["id"] = str(t["id"])
        t["user_id"] = str(t["user_id"])
        for f in ("opened_at", "closed_at"):
            if t.get(f): t[f] = t[f].isoformat()
        # Fake tick-by-tick reconstruction (would query candles in real impl)
        return web.json_response({
            "trade": t,
            "ticks": [],  # placeholder for tick replay
            "events": [{"type": "entry", "ts": t.get("opened_at"), "price": t.get("entry_price")},
                       {"type": "exit", "ts": t.get("closed_at"), "price": t.get("exit_price")}],
        })

    async def handle_perf_attribution(request: web.Request) -> web.Response:
        """GET /api/attribution — which features drive returns (PnL by scanner/regime/grade).

        Scoped: non-admins see only their own attribution. Admins see system-wide
        aggregated performance. Previously this returned system-wide stats to
        ANY authenticated caller, leaking aggregate portfolio performance.
        """
        user_id, role = _get_user_ctx(request)
        if not user_id:
            return web.json_response({"error": "unauthorized"}, status=401)

        async with db_pool.acquire() as conn:
            if role == "admin":
                rows = await conn.fetch("""
                    SELECT
                        metadata->>'scanner' AS scanner,
                        metadata->>'regime' AS regime,
                        metadata->>'grade' AS grade,
                        COUNT(*) AS trades,
                        COUNT(*) FILTER (WHERE pnl_usd > 0) AS wins,
                        COALESCE(SUM(pnl_usd), 0) AS total_pnl,
                        COALESCE(AVG(pnl_usd), 0) AS avg_pnl
                    FROM user_trades
                    WHERE status = 'closed' AND pnl_usd IS NOT NULL
                    GROUP BY scanner, regime, grade
                    ORDER BY total_pnl DESC
                    LIMIT 100
                """)
            else:
                rows = await conn.fetch("""
                    SELECT
                        metadata->>'scanner' AS scanner,
                        metadata->>'regime' AS regime,
                        metadata->>'grade' AS grade,
                        COUNT(*) AS trades,
                        COUNT(*) FILTER (WHERE pnl_usd > 0) AS wins,
                        COALESCE(SUM(pnl_usd), 0) AS total_pnl,
                        COALESCE(AVG(pnl_usd), 0) AS avg_pnl
                    FROM user_trades
                    WHERE user_id = $1 AND status = 'closed' AND pnl_usd IS NOT NULL
                    GROUP BY scanner, regime, grade
                    ORDER BY total_pnl DESC
                    LIMIT 100
                """, user_id)
        attribution = []
        for r in rows:
            d = dict(r)
            d["wr_pct"] = round((d["wins"] / d["trades"] * 100) if d["trades"] > 0 else 0, 1)
            d["total_pnl"] = float(d["total_pnl"] or 0)
            d["avg_pnl"] = round(float(d["avg_pnl"] or 0), 2)
            attribution.append(d)
        return web.json_response({
            "attribution": attribution,
            "scope": "system" if role == "admin" else "user",
        })

    app.router.add_get("/api/replay/trade/{trade_id}", handle_trade_replay)
    app.router.add_get("/api/attribution", handle_perf_attribution)
    logger.info("Replay + attribution routes registered (2 endpoints, user-scoped)")
