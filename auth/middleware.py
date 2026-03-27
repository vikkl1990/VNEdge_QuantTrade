"""aiohttp authentication middleware."""
import logging
from aiohttp import web

logger = logging.getLogger(__name__)


class AuthMiddleware:
    """Database-backed auth middleware for aiohttp.

    Validates session tokens from cookies, injects user info into request.
    """

    PUBLIC_PATHS = {
        "/api/login", "/api/register", "/api/ping", "/favicon.ico",
        "/api/health",
    }
    PUBLIC_PREFIXES = ("/static/",)

    def __init__(self, auth_service):
        self.auth_service = auth_service

    @web.middleware
    async def middleware(self, request: web.Request, handler):
        path = request.path

        # Allow public paths
        if path in self.PUBLIC_PATHS:
            return await handler(request)
        if any(path.startswith(p) for p in self.PUBLIC_PREFIXES):
            return await handler(request)

        # Check for page requests (serve login page)
        if path == "/" or not path.startswith("/api/"):
            # For HTML pages, check auth but don't block — JS will handle login overlay
            cookie = request.cookies.get("vn_session")
            if cookie:
                token = cookie.split(":", 1)[0]
                session = await self.auth_service.verify_session(token)
                if session:
                    request["user"] = session
            return await handler(request)

        # API requests require auth
        cookie = request.cookies.get("vn_session")
        if not cookie:
            return web.json_response({"error": "unauthorized"}, status=401)

        token = cookie.split(":", 1)[0]
        session = await self.auth_service.verify_session(token)
        if not session:
            return web.json_response({"error": "session_expired"}, status=401)

        # Inject user info into request
        request["user"] = session
        return await handler(request)


def require_role(*roles):
    """Decorator to require specific roles for an endpoint."""
    def decorator(handler):
        async def wrapper(request: web.Request) -> web.Response:
            user = request.get("user")
            if not user:
                return web.json_response({"error": "unauthorized"}, status=401)
            if user.get("role") not in roles:
                return web.json_response(
                    {"error": "forbidden", "required_role": list(roles)}, status=403
                )
            return await handler(request)
        wrapper.__name__ = handler.__name__
        return wrapper
    return decorator
