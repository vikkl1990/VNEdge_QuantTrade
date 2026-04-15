"""Security middleware: CSRF tokens, rate limiting, CSP headers, structured audit logs."""
import logging
import secrets
import time
from collections import defaultdict, deque
from aiohttp import web

logger = logging.getLogger("dashboard.security")

# In-memory rate limit tracking (per IP)
_rate_limits = defaultdict(lambda: deque(maxlen=100))
RATE_LIMIT_PER_MIN = 120  # general
RATE_LIMIT_LOGIN = 10     # login endpoint
RATE_LIMIT_ADMIN = 30     # admin endpoints

# CSRF tokens per session
_csrf_tokens = {}


@web.middleware
async def security_headers_middleware(request: web.Request, handler):
    """Add comprehensive security headers + CSP."""
    response = await handler(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=(), payment=()"
    # Strict CSP — only allow self + Chart.js CDN + Google Fonts
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: blob:; "
        "connect-src 'self' https://api.telegram.org; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Server"] = "VNEdge"
    return response


@web.middleware
async def rate_limit_middleware(request: web.Request, handler):
    """Per-IP rate limiting with endpoint-specific limits."""
    ip = request.remote or "unknown"
    path = request.path
    now = time.time()

    # Determine limit
    if path == "/api/login":
        limit = RATE_LIMIT_LOGIN
    elif path.startswith("/api/admin/"):
        limit = RATE_LIMIT_ADMIN
    else:
        limit = RATE_LIMIT_PER_MIN

    # Track this request
    bucket = _rate_limits[(ip, path[:30])]
    # Drop entries older than 60s
    cutoff = now - 60
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= limit:
        logger.warning("Rate limit hit: %s %s (%d/%d in 60s)", ip, path, len(bucket), limit)
        return web.json_response({"error": "rate_limited", "retry_after": 60}, status=429)
    bucket.append(now)
    return await handler(request)


def generate_csrf_token(session_token: str) -> str:
    """Generate or retrieve a CSRF token for a session."""
    if session_token not in _csrf_tokens:
        _csrf_tokens[session_token] = secrets.token_urlsafe(32)
    return _csrf_tokens[session_token]


def verify_csrf(request: web.Request) -> bool:
    """Verify CSRF token from header matches the session's token.

    Browsers must send X-CSRF-Token header on POST/PUT/DELETE.
    For backward compat: only enforces if token is set in session cache.
    """
    method = request.method
    if method in ("GET", "HEAD", "OPTIONS"):
        return True
    if request.path in ("/api/login", "/api/register"):
        return True  # bootstrap endpoints

    cookie = request.cookies.get("vn_session", "")
    if not cookie:
        return True  # no session yet, let auth middleware handle

    expected = _csrf_tokens.get(cookie)
    if not expected:
        return True  # token not yet generated for this session — soft mode

    sent = request.headers.get("X-CSRF-Token", "")
    return secrets.compare_digest(sent, expected) if sent else False


async def audit_log(db_pool, action: str, user_id: str, ip: str, details: dict = None):
    """Persist structured audit log entry to DB."""
    if not db_pool:
        return
    import json
    try:
        async with db_pool.acquire() as conn:
            # Reuse login_history table for now (extend to audit_log later)
            await conn.execute("""
                INSERT INTO login_history (user_id, email, ip_address, success, failure_reason)
                VALUES ($1, $2, $3, TRUE, $4)
            """, user_id if user_id else None, action[:255], ip[:45],
                 json.dumps(details or {})[:100])
    except Exception as e:
        logger.debug("audit_log failed: %s", e)
