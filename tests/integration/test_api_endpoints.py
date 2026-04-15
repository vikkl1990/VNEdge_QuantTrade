"""Integration tests for API endpoints — verifies routes don't break."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest
import asyncio
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop
from aiohttp import web


# Import without triggering full bot initialization
def test_imports_dont_crash():
    """Verify all dashboard route modules import cleanly."""
    from dashboard import admin_routes, user_trading_routes, replay_routes
    from dashboard import twofa_routes, email_routes, backtest_routes
    from dashboard import security_middleware
    assert all([admin_routes, user_trading_routes, replay_routes,
                twofa_routes, email_routes, backtest_routes, security_middleware])


def test_security_middleware_exports():
    """Verify security middleware exports expected functions."""
    from dashboard.security_middleware import (
        security_headers_middleware, rate_limit_middleware,
        generate_csrf_token, verify_csrf, audit_log
    )
    assert callable(security_headers_middleware)
    assert callable(rate_limit_middleware)


def test_backtest_engine_runs():
    """Run backtest with empty config, verify returns correct shape."""
    from ml_training.backtest_engine import run_backtest
    result = run_backtest({"symbols": [], "scanners": []})
    assert "total_trades" in result
    assert "win_rate" in result
    assert "total_pnl" in result
    assert "sharpe" in result


if __name__ == "__main__":
    test_imports_dont_crash()
    test_security_middleware_exports()
    test_backtest_engine_runs()
    print("API integration tests passed")
