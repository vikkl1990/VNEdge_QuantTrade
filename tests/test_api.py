"""
API tests for the VN Edge crypto bot dashboard.

Tests the DashboardServer class directly using aiohttp test client
with mocked dependencies. Also includes a live smoke test suite that
can run against the production endpoint.

Run:
    cd crypto-trading-bot
    python -m pytest tests/test_api.py -v --tb=short
"""

import json
import re
import time
from unittest.mock import patch, MagicMock

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer


# ---------------------------------------------------------------------------
# Fixtures — mock get_config so DashboardServer can be instantiated without
# a real settings.yaml / .env on disk.
# ---------------------------------------------------------------------------

def _fake_config():
    """Return a minimal Config-like object for DashboardServer.__init__."""
    cfg = MagicMock()
    cfg.get.side_effect = lambda key, default=None: {
        "dashboard": {"refresh_interval": 5, "max_alerts_display": 50},
        "bot": {"name": "TestBot", "version": "0.0.1-test", "mode": "paper"},
        "strategy": {"active": "test_strategy"},
        "symbols": ["BTC/USDT", "ETH/USDT"],
        "paper_trading": {
            "taker_fee_rate": 0.0006,
            "maker_fee_rate": 0.0004,
            "settlement_fee_rate": 0.0006,
        },
    }.get(key, default)
    return cfg


@pytest.fixture
def patched_config():
    """Patch get_config globally so DashboardServer can be created."""
    with patch("dashboard.server.get_config", return_value=_fake_config()):
        yield


@pytest_asyncio.fixture
async def dashboard(patched_config):
    """Create a DashboardServer instance with mocked config."""
    from dashboard.server import DashboardServer
    server = DashboardServer()
    return server


@pytest_asyncio.fixture
async def cli(dashboard):
    """Create an aiohttp test client bound to the DashboardServer app."""
    dashboard._started_at = time.time()
    dashboard._bot_status = "running"

    app = web.Application(middlewares=[dashboard._auth_middleware])
    dashboard._register_routes(app)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    yield client
    await client.close()


# ===================================================================
# Part 1 & 2: Unit Tests — DashboardServer via aiohttp test client
# ===================================================================


class TestPingEndpoint:
    """GET /api/ping — health check."""

    @pytest.mark.asyncio
    async def test_ping_returns_200(self, cli):
        resp = await cli.get("/api/ping")
        assert resp.status == 200, f"Expected 200, got {resp.status}"

    @pytest.mark.asyncio
    async def test_ping_returns_json_with_timestamp(self, cli):
        resp = await cli.get("/api/ping")
        data = await resp.json()
        assert "t" in data, "Ping response must contain 't' (timestamp)"
        assert isinstance(data["t"], (int, float)), "Timestamp must be numeric"
        # Sanity: timestamp should be within the last 60 seconds (in ms)
        now_ms = time.time() * 1000
        assert abs(data["t"] - now_ms) < 60_000, "Timestamp seems stale"


class TestIndexPage:
    """GET / — main dashboard HTML page."""

    @pytest.mark.asyncio
    async def test_index_returns_200(self, cli):
        resp = await cli.get("/")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_index_returns_html(self, cli):
        resp = await cli.get("/")
        ct = resp.content_type
        assert "text/html" in ct, f"Expected text/html, got {ct}"

    @pytest.mark.asyncio
    async def test_index_no_sensitive_data(self, cli):
        """The HTML page must never leak secrets."""
        resp = await cli.get("/")
        body = await resp.text()
        # Check for actual secret VALUES, not form field names.
        # Form fields like `api_key` input and `password` input are expected in login forms.
        sensitive_patterns = ["DELTA_API_KEY", "DASHBOARD_PASSWORD",
                              "sk-ant-", "Bearer sk-"]
        for pattern in sensitive_patterns:
            assert pattern not in body, (
                f"Sensitive data '{pattern}' found in index HTML"
            )


class TestRealStatus:
    """GET /api/real/status — real trading status."""

    @pytest.mark.asyncio
    async def test_returns_200(self, cli):
        resp = await cli.get("/api/real/status")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_returns_valid_json(self, cli):
        resp = await cli.get("/api/real/status")
        data = await resp.json()
        assert isinstance(data, dict)

    @pytest.mark.asyncio
    async def test_schema_when_disabled(self, cli):
        """When no real_manager exists, the fallback schema is returned."""
        resp = await cli.get("/api/real/status")
        data = await resp.json()

        # Required top-level keys
        assert "enabled" in data
        assert isinstance(data["enabled"], bool)

        assert "dry_run" in data
        assert isinstance(data["dry_run"], bool)

        assert "mode" in data
        assert isinstance(data["mode"], str)

        assert "balance" in data
        assert isinstance(data["balance"], (int, float))

        # Circuit breaker sub-object
        assert "circuit_breaker" in data
        cb = data["circuit_breaker"]
        assert isinstance(cb, dict)
        assert "daily_pnl" in cb
        assert isinstance(cb["daily_pnl"], (int, float))
        assert "is_tripped" in cb
        assert isinstance(cb["is_tripped"], bool)

        # Lists
        assert "open_positions" in data
        assert isinstance(data["open_positions"], list)

        assert "recent_trades" in data
        assert isinstance(data["recent_trades"], list)

        # Counters
        assert "open_count" in data
        assert isinstance(data["open_count"], int)

        assert "closed_today" in data
        assert isinstance(data["closed_today"], int)

        assert "total_closed" in data
        assert isinstance(data["total_closed"], int)


class TestRealStatusWithManager:
    """GET /api/real/status with a mocked real_manager attached."""

    @pytest.mark.asyncio
    async def test_schema_with_trades(self, cli, dashboard):
        """Attach a mock real_manager with sample trades and validate schema."""
        mock_mgr = MagicMock()
        mock_mgr.dry_run = False
        mock_mgr.get_status.return_value = {
            "enabled": True,
            "dry_run": False,
            "mode": "LIVE",
            "balance": 1523.45,
            "circuit_breaker": {"daily_pnl": -12.50, "is_tripped": False},
            "open_positions": [
                {"symbol": "BTC/USDT", "side": "long", "entry_price": 67000.0,
                 "size": 0.001, "unrealized_pnl": 5.20}
            ],
            "open_count": 1,
            "closed_today": 3,
            "total_closed": 127,
            "recent_trades": [
                {
                    "trade_id": "T-20260329-001",
                    "symbol": "ETH/USDT",
                    "side": "long",
                    "entry_price": 3200.50,
                    "exit_price": 3250.75,
                    "pnl_usd": 8.42,
                    "reason": "tp1_hit",
                },
                {
                    "trade_id": "T-20260329-002",
                    "symbol": "BTC/USDT",
                    "side": "short",
                    "entry_price": 68500.00,
                    "exit_price": 68200.00,
                    "pnl_usd": 4.50,
                    "reason": "trailing_stop",
                },
            ],
        }
        mock_mgr.update_prices = MagicMock(return_value=None)
        mock_mgr.refresh_balance = MagicMock(return_value=None)

        # Attach to dashboard
        dashboard._real_manager = mock_mgr

        resp = await cli.get("/api/real/status")
        assert resp.status == 200
        data = await resp.json()

        assert data["enabled"] is True
        assert data["mode"] == "LIVE"
        assert data["balance"] == 1523.45
        assert data["open_count"] == 1
        assert len(data["recent_trades"]) == 2

        # Validate each trade
        for trade in data["recent_trades"]:
            assert "trade_id" in trade
            assert isinstance(trade["trade_id"], str)

            assert "symbol" in trade
            assert re.match(r"^[A-Z]+/USDT$", trade["symbol"]), (
                f"Symbol '{trade['symbol']}' does not match XXX/USDT"
            )

            assert trade["side"] in ("long", "short"), (
                f"Side must be 'long' or 'short', got '{trade['side']}'"
            )

            assert "entry_price" in trade
            assert isinstance(trade["entry_price"], (int, float))
            assert trade["entry_price"] > 0

            assert "exit_price" in trade
            assert isinstance(trade["exit_price"], (int, float))
            assert trade["exit_price"] > 0

            assert "pnl_usd" in trade
            assert isinstance(trade["pnl_usd"], (int, float))

            assert "reason" in trade
            assert isinstance(trade["reason"], str)
            assert len(trade["reason"]) > 0


class TestNotFoundHandling:
    """Unknown routes should return 404."""

    @pytest.mark.asyncio
    async def test_unknown_api_returns_404(self, cli):
        resp = await cli.get("/api/nonexistent")
        assert resp.status == 404, f"Expected 404, got {resp.status}"

    @pytest.mark.asyncio
    async def test_unknown_page_returns_404(self, cli):
        resp = await cli.get("/does-not-exist")
        assert resp.status == 404, f"Expected 404, got {resp.status}"


class TestNoSensitiveDataExposure:
    """Responses must never contain secrets."""

    SENSITIVE_WORDS = [
        "api_key", "api_secret", "password", "secret_key",
        "DELTA_API_KEY", "DASHBOARD_PASSWORD", "DASHBOARD_SECRET_KEY",
        "private_key", "passphrase",
    ]

    @pytest.mark.asyncio
    async def test_status_no_secrets(self, cli):
        resp = await cli.get("/api/status")
        body = await resp.text()
        for word in self.SENSITIVE_WORDS:
            assert word.lower() not in body.lower(), (
                f"Sensitive word '{word}' found in /api/status response"
            )

    @pytest.mark.asyncio
    async def test_real_status_no_secrets(self, cli):
        resp = await cli.get("/api/real/status")
        body = await resp.text()
        for word in self.SENSITIVE_WORDS:
            assert word.lower() not in body.lower(), (
                f"Sensitive word '{word}' found in /api/real/status response"
            )

    @pytest.mark.asyncio
    async def test_ping_no_secrets(self, cli):
        resp = await cli.get("/api/ping")
        body = await resp.text()
        for word in self.SENSITIVE_WORDS:
            assert word.lower() not in body.lower(), (
                f"Sensitive word '{word}' found in /api/ping response"
            )


class TestRiskMetrics:
    """GET /api/risk-metrics."""

    @pytest.mark.asyncio
    async def test_returns_200_or_insufficient(self, cli):
        """Risk metrics returns 200 even with insufficient data."""
        resp = await cli.get("/api/risk-metrics")
        assert resp.status == 200
        data = await resp.json()
        # Either returns metrics or an "insufficient_data" error
        if "error" in data:
            assert data["error"] == "insufficient_data"
        else:
            for key in ["sharpe", "sortino", "calmar", "max_drawdown_pct",
                        "total_trades", "profit_factor"]:
                assert key in data, f"Missing key: {key}"


class TestAuthMiddleware:
    """POST endpoints require authentication."""

    @pytest.mark.asyncio
    async def test_post_without_auth_returns_401(self, cli):
        """POST to a protected endpoint without auth should return 401."""
        resp = await cli.post("/api/real/toggle", json={"enabled": True})
        assert resp.status == 401
        data = await resp.json()
        assert data.get("error") == "unauthorized"

    @pytest.mark.asyncio
    async def test_post_emergency_stop_requires_auth(self, cli):
        resp = await cli.post("/api/emergency-stop", json={})
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_get_endpoints_are_public(self, cli):
        """All GET endpoints should be accessible without auth."""
        public_gets = [
            "/api/ping",
            "/api/real/status",
            "/api/risk-metrics",
            "/api/emergency-status",
        ]
        for path in public_gets:
            resp = await cli.get(path)
            assert resp.status == 200, (
                f"GET {path} returned {resp.status}, expected 200"
            )


class TestStatusEndpoint:
    """GET /api/status — general bot status."""

    @pytest.mark.asyncio
    async def test_returns_200(self, cli):
        resp = await cli.get("/api/status")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_schema(self, cli):
        resp = await cli.get("/api/status")
        data = await resp.json()

        assert "bot_name" in data
        assert "bot_version" in data
        assert "bot_status" in data
        assert "mode" in data
        assert "symbols" in data
        assert isinstance(data["symbols"], list)
        assert "uptime" in data
        assert "server_time" in data
        assert "fees" in data
        assert isinstance(data["fees"], dict)


class TestPerformanceEndpoint:
    """GET /api/performance — PnL metrics."""

    @pytest.mark.asyncio
    async def test_returns_200(self, cli):
        resp = await cli.get("/api/performance")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_schema(self, cli):
        resp = await cli.get("/api/performance")
        data = await resp.json()

        for key in ["daily_pnl", "total_pnl", "win_rate",
                     "trades_today", "max_drawdown", "wins", "losses"]:
            assert key in data, f"Missing performance key: {key}"
            assert isinstance(data[key], (int, float)), (
                f"{key} should be numeric, got {type(data[key])}"
            )


# ===================================================================
# Part 3: Live Smoke Tests (only run when --live flag or env is set)
# ===================================================================

import os

LIVE_URL = os.environ.get("DASHBOARD_URL", "http://150.230.171.48:8080")
SKIP_LIVE = not os.environ.get("RUN_LIVE_TESTS", "")


@pytest.mark.skipif(SKIP_LIVE, reason="Set RUN_LIVE_TESTS=1 to run live tests")
class TestLiveSmoke:
    """Live smoke tests against the production dashboard."""

    @pytest.fixture(autouse=True)
    def setup_session(self):
        import aiohttp
        self._session_cls = aiohttp.ClientSession

    @pytest.mark.asyncio
    async def test_live_ping(self):
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{LIVE_URL}/api/ping") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert "t" in data

    @pytest.mark.asyncio
    async def test_live_real_status_schema(self):
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{LIVE_URL}/api/real/status") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert "enabled" in data
                assert "circuit_breaker" in data
                assert "open_positions" in data

    @pytest.mark.asyncio
    async def test_live_risk_metrics(self):
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{LIVE_URL}/api/risk-metrics") as resp:
                assert resp.status == 200

    @pytest.mark.asyncio
    async def test_live_index_page(self):
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{LIVE_URL}/") as resp:
                assert resp.status == 200
                body = await resp.text()
                assert "<html" in body.lower() or "<!doctype" in body.lower()

    @pytest.mark.asyncio
    async def test_live_404(self):
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{LIVE_URL}/api/nonexistent") as resp:
                assert resp.status == 404

    @pytest.mark.asyncio
    async def test_live_no_sensitive_data_in_html(self):
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{LIVE_URL}/") as resp:
                body = await resp.text()
                for word in ["api_key", "api_secret", "password", "secret_key"]:
                    assert word not in body.lower(), (
                        f"Sensitive word '{word}' found in live HTML"
                    )

    @pytest.mark.asyncio
    async def test_live_path_traversal_blocked(self):
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{LIVE_URL}/api/../config/settings.yaml"
            ) as resp:
                assert resp.status in (
                    400, 403, 404
                ), f"Path traversal returned {resp.status}"

    @pytest.mark.asyncio
    async def test_live_xss_not_reflected(self):
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{LIVE_URL}/api/real/status?q=<script>alert(1)</script>"
            ) as resp:
                body = await resp.text()
                assert "<script>alert(1)</script>" not in body

    @pytest.mark.asyncio
    async def test_live_cors_no_wildcard(self):
        import aiohttp
        async with aiohttp.ClientSession() as session:
            headers = {"Origin": "http://evil.com"}
            async with session.get(
                f"{LIVE_URL}/api/ping", headers=headers
            ) as resp:
                acao = resp.headers.get("Access-Control-Allow-Origin", "")
                assert acao != "*", "CORS allows wildcard origin"
                assert "evil.com" not in acao, "CORS allows evil.com"

    @pytest.mark.asyncio
    async def test_live_post_requires_auth(self):
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{LIVE_URL}/api/real/toggle", json={"enabled": True}
            ) as resp:
                assert resp.status == 401
