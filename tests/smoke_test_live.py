#!/usr/bin/env python3
"""
Live smoke tests + security checks against the VN Edge production dashboard.

Usage:
    python tests/smoke_test_live.py [URL]

Default URL: http://150.230.171.48:8080
"""

import asyncio
import json
import re
import sys
import time

import aiohttp

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://150.230.171.48:8080"

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
WARN = "\033[93mWARN\033[0m"

results = {"pass": 0, "fail": 0, "warn": 0}
defects = []


def report(status, name, detail=""):
    tag = {"pass": PASS, "fail": FAIL, "warn": WARN}[status]
    results[status] += 1
    msg = f"  [{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    if status == "fail":
        defects.append(f"{name}: {detail}")


async def run_all():
    timeout = aiohttp.ClientTimeout(total=15)

    async with aiohttp.ClientSession(timeout=timeout) as s:
        # ==============================================================
        print("\n=== PART 1: SMOKE TESTS ===\n")
        # ==============================================================

        # 1. Health check
        t0 = time.time()
        try:
            async with s.get(f"{BASE_URL}/api/ping") as r:
                elapsed = round((time.time() - t0) * 1000)
                data = await r.json()
                if r.status == 200 and "t" in data:
                    report("pass", "GET /api/ping", f"200 OK, {elapsed}ms")
                else:
                    report("fail", "GET /api/ping", f"status={r.status}")
        except Exception as e:
            report("fail", "GET /api/ping", str(e))

        # 2. Real status
        t0 = time.time()
        try:
            async with s.get(f"{BASE_URL}/api/real/status") as r:
                elapsed = round((time.time() - t0) * 1000)
                data = await r.json()
                if r.status == 200:
                    report("pass", "GET /api/real/status", f"200 OK, {elapsed}ms, keys={list(data.keys())[:8]}")
                    # Parse key fields
                    print(f"         mode={data.get('mode')}, enabled={data.get('enabled')}, "
                          f"open={data.get('open_count')}, balance={data.get('balance')}")
                    cb = data.get("circuit_breaker", {})
                    print(f"         circuit_breaker: daily_pnl={cb.get('daily_pnl')}, tripped={cb.get('is_tripped')}")
                else:
                    report("fail", "GET /api/real/status", f"status={r.status}")
        except Exception as e:
            report("fail", "GET /api/real/status", str(e))

        # 3. Risk metrics
        t0 = time.time()
        try:
            async with s.get(f"{BASE_URL}/api/risk-metrics") as r:
                elapsed = round((time.time() - t0) * 1000)
                data = await r.json()
                if r.status == 200:
                    if "error" in data and data["error"] == "insufficient_data":
                        report("pass", "GET /api/risk-metrics", f"200 OK (insufficient_data, {data.get('trades')} trades), {elapsed}ms")
                    else:
                        report("pass", "GET /api/risk-metrics", f"200 OK, sharpe={data.get('sharpe')}, {elapsed}ms")
                else:
                    report("fail", "GET /api/risk-metrics", f"status={r.status}")
        except Exception as e:
            report("fail", "GET /api/risk-metrics", str(e))

        # 4. Main page
        t0 = time.time()
        try:
            async with s.get(f"{BASE_URL}/") as r:
                elapsed = round((time.time() - t0) * 1000)
                body = await r.text()
                if r.status == 200 and ("<html" in body.lower() or "<!doctype" in body.lower()):
                    report("pass", "GET /", f"200 OK, {len(body)} bytes, {elapsed}ms")
                else:
                    report("fail", "GET /", f"status={r.status}, size={len(body)}")
        except Exception as e:
            report("fail", "GET /", str(e))

        # 5. 404 handling
        try:
            async with s.get(f"{BASE_URL}/api/nonexistent") as r:
                if r.status == 404:
                    report("pass", "GET /api/nonexistent", "404 as expected")
                else:
                    report("fail", "GET /api/nonexistent", f"Expected 404, got {r.status}")
        except Exception as e:
            report("fail", "GET /api/nonexistent", str(e))

        # 6. Response headers
        try:
            async with s.get(f"{BASE_URL}/api/ping") as r:
                ct = r.headers.get("Content-Type", "")
                if "json" in ct:
                    report("pass", "Response Content-Type", f"{ct}")
                else:
                    report("warn", "Response Content-Type", f"Expected json, got {ct}")
        except Exception as e:
            report("fail", "Response headers", str(e))

        # ==============================================================
        print("\n=== PART 2: SCHEMA VALIDATION ===\n")
        # ==============================================================

        try:
            async with s.get(f"{BASE_URL}/api/real/status") as r:
                data = await r.json()

                # Top-level schema
                schema_checks = [
                    ("enabled", bool),
                    ("dry_run", bool),
                    ("mode", str),
                    ("balance", (int, float)),
                    ("open_positions", list),
                    ("open_count", int),
                    ("closed_today", int),
                    ("total_closed", int),
                    ("recent_trades", list),
                ]

                for key, expected_type in schema_checks:
                    if key not in data:
                        report("fail", f"Schema: {key}", "missing from response")
                    elif not isinstance(data[key], expected_type):
                        report("fail", f"Schema: {key}", f"expected {expected_type.__name__}, got {type(data[key]).__name__}")
                    else:
                        report("pass", f"Schema: {key}", f"{type(data[key]).__name__} = {repr(data[key])[:60]}")

                # Circuit breaker
                cb = data.get("circuit_breaker")
                if not isinstance(cb, dict):
                    report("fail", "Schema: circuit_breaker", f"expected dict, got {type(cb)}")
                else:
                    report("pass", "Schema: circuit_breaker", f"dict with keys {list(cb.keys())}")
                    if "daily_pnl" not in cb:
                        report("fail", "Schema: circuit_breaker.daily_pnl", "missing")
                    elif not isinstance(cb["daily_pnl"], (int, float)):
                        report("fail", "Schema: circuit_breaker.daily_pnl", f"not numeric: {type(cb['daily_pnl'])}")
                    else:
                        report("pass", "Schema: circuit_breaker.daily_pnl", str(cb["daily_pnl"]))

                    if "is_tripped" not in cb:
                        report("fail", "Schema: circuit_breaker.is_tripped", "missing")
                    elif not isinstance(cb["is_tripped"], bool):
                        report("fail", "Schema: circuit_breaker.is_tripped", f"not bool: {type(cb['is_tripped'])}")
                    else:
                        report("pass", "Schema: circuit_breaker.is_tripped", str(cb["is_tripped"]))

                # Recent trades validation
                trades = data.get("recent_trades", [])
                if len(trades) > 0:
                    print(f"\n  Validating {len(trades)} recent trades...")
                    for i, trade in enumerate(trades[:5]):  # check up to 5
                        errors = []
                        if not isinstance(trade.get("trade_id"), str):
                            errors.append("trade_id not str")
                        sym = trade.get("symbol", "")
                        if not re.match(r"^[A-Z0-9]+/USDT$", sym):
                            errors.append(f"symbol '{sym}' not XXX/USDT")
                        if trade.get("side") not in ("long", "short"):
                            errors.append(f"side '{trade.get('side')}' invalid")
                        if not isinstance(trade.get("entry_price"), (int, float)) or trade.get("entry_price", 0) <= 0:
                            errors.append("entry_price invalid")
                        if not isinstance(trade.get("exit_price"), (int, float)) or trade.get("exit_price", 0) <= 0:
                            errors.append("exit_price invalid")
                        if not isinstance(trade.get("pnl_usd"), (int, float)):
                            errors.append("pnl_usd not numeric")
                        if not isinstance(trade.get("reason"), str) or len(trade.get("reason", "")) == 0:
                            errors.append("reason missing/empty")

                        if errors:
                            report("fail", f"Trade [{i}] {trade.get('trade_id','?')}", "; ".join(errors))
                        else:
                            report("pass", f"Trade [{i}] {trade.get('trade_id','?')}",
                                   f"{trade['symbol']} {trade['side']} pnl={trade['pnl_usd']}")
                else:
                    report("pass", "Recent trades", "empty (no trades to validate)")

        except Exception as e:
            report("fail", "Schema validation", str(e))

        # ==============================================================
        print("\n=== PART 3: SECURITY CHECKS ===\n")
        # ==============================================================

        # Sensitive data in HTML
        try:
            async with s.get(f"{BASE_URL}/") as r:
                body = await r.text()
                leaked = []
                for word in ["api_key", "api_secret", "password", "secret_key",
                             "BINANCE_API_KEY", "DASHBOARD_PASSWORD"]:
                    if word.lower() in body.lower():
                        leaked.append(word)
                if leaked:
                    report("fail", "Sensitive data in HTML", f"Found: {leaked}")
                else:
                    report("pass", "Sensitive data in HTML", "None found")
        except Exception as e:
            report("fail", "Sensitive data check", str(e))

        # Path traversal
        try:
            async with s.get(f"{BASE_URL}/api/../config/settings.yaml") as r:
                if r.status in (400, 403, 404):
                    report("pass", "Path traversal", f"Blocked with {r.status}")
                elif r.status == 200:
                    body = await r.text()
                    if "exchange" in body or "api_key" in body:
                        report("fail", "Path traversal", "Config file exposed!")
                    else:
                        report("warn", "Path traversal", f"200 returned but may be safe (redirected to index)")
                else:
                    report("pass", "Path traversal", f"Got {r.status}")
        except Exception as e:
            report("fail", "Path traversal", str(e))

        # XSS reflection
        try:
            async with s.get(f"{BASE_URL}/api/real/status?q=<script>alert(1)</script>") as r:
                body = await r.text()
                if "<script>alert(1)</script>" in body:
                    report("fail", "XSS reflection", "Script tag reflected in response")
                else:
                    report("pass", "XSS reflection", "Not reflected")
        except Exception as e:
            report("fail", "XSS check", str(e))

        # CORS
        try:
            headers = {"Origin": "http://evil.com"}
            async with s.get(f"{BASE_URL}/api/ping", headers=headers) as r:
                acao = r.headers.get("Access-Control-Allow-Origin", "")
                if acao == "*":
                    report("fail", "CORS wildcard", "Access-Control-Allow-Origin: * (allows any origin)")
                elif "evil.com" in acao:
                    report("fail", "CORS evil origin", f"ACAO includes evil.com: {acao}")
                else:
                    report("pass", "CORS policy", f"ACAO='{acao}' (safe)")
        except Exception as e:
            report("fail", "CORS check", str(e))

        # Server header leak
        try:
            async with s.get(f"{BASE_URL}/") as r:
                server_hdr = r.headers.get("Server", "")
                powered_by = r.headers.get("X-Powered-By", "")
                if server_hdr:
                    report("warn", "Server header leak", f"Server: {server_hdr}")
                else:
                    report("pass", "Server header", "Not disclosed")
                if powered_by:
                    report("warn", "X-Powered-By leak", f"X-Powered-By: {powered_by}")
                else:
                    report("pass", "X-Powered-By", "Not disclosed")
        except Exception as e:
            report("fail", "Server info check", str(e))

        # POST requires auth
        try:
            async with s.post(f"{BASE_URL}/api/real/toggle", json={"enabled": True}) as r:
                if r.status == 401:
                    report("pass", "POST /api/real/toggle auth", "401 Unauthorized (correct)")
                else:
                    report("fail", "POST /api/real/toggle auth", f"Expected 401, got {r.status}")
        except Exception as e:
            report("fail", "POST auth check", str(e))

        # Emergency stop requires auth
        try:
            async with s.post(f"{BASE_URL}/api/emergency-stop", json={}) as r:
                if r.status == 401:
                    report("pass", "POST /api/emergency-stop auth", "401 Unauthorized (correct)")
                else:
                    report("fail", "POST /api/emergency-stop auth", f"Expected 401, got {r.status}")
        except Exception as e:
            report("fail", "Emergency stop auth", str(e))

    # ==============================================================
    print("\n" + "=" * 60)
    print(f"RESULTS: {results['pass']} passed, {results['fail']} failed, {results['warn']} warnings")
    print("=" * 60)

    if defects:
        print(f"\nDEFECTS ({len(defects)}):")
        for d in defects:
            print(f"  - {d}")

    return results["fail"] == 0


if __name__ == "__main__":
    print(f"VN Edge Dashboard Smoke Tests — {BASE_URL}")
    print("=" * 60)
    ok = asyncio.run(run_all())
    sys.exit(0 if ok else 1)
