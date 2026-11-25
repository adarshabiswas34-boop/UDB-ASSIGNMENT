"""
Rate Limiting Middleware for FastAPI / Starlette.

Extracts client identifier from headers (x-api-key, Authorization Bearer, X-Forwarded-For, or client IP),
evaluates request against the rate limiter service with route-specific or default options,
sets standard rate limit headers, and returns HTTP 429 when limits are exceeded.
"""

import math
import logging
from typing import Optional, Set, Iterable, Dict, Tuple
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response, JSONResponse
from config.settings import settings
from src.limiter import get_rate_limiter, RateLimiter, RateLimitResult

logger = logging.getLogger(__name__)


def extract_client_identifier(request: Request) -> str:
    """
    Extracts a unique client identifier from incoming HTTP request headers or network client.

    Resolution order:
    1. `x-api-key` header (API Key authentication)
    2. `Authorization: Bearer <token>` header
    3. `X-Forwarded-For` header (reverse proxy / load balancer forwarded IP)
    4. Direct socket client host (`request.client.host`)
    5. Fallback anonymous identifier
    """
    # 1. API Key Header
    api_key = request.headers.get("x-api-key")
    if api_key and api_key.strip():
        return f"apikey:{api_key.strip()}"

    # 2. Authorization Bearer Token
    auth_header = request.headers.get("authorization")
    if auth_header and auth_header.strip().lower().startswith("bearer "):
        token = auth_header.strip()[7:].strip()
        if token:
            return f"bearer:{token[:24]}"

    # 3. X-Forwarded-For Header (takes first IP in proxy chain)
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for and forwarded_for.strip():
        client_ip = forwarded_for.split(",")[0].strip()
        if client_ip:
            return f"ip:{client_ip}"

    # 4. Direct Client Socket IP
    if request.client and request.client.host:
        return f"ip:{request.client.host}"

    # 5. Fallback
    return "ip:anonymous"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    FastAPI / Starlette Middleware enforcing global or route-specific rate limits.

    Headers injected on every response:
    - `X-RateLimit-Limit`: Maximum permitted requests in the active window.
    - `X-RateLimit-Remaining`: Remaining request quota in the active window.
    - `X-RateLimit-Reset`: Seconds remaining until window resets or oldest request expires.
    - `Retry-After`: (On HTTP 429) Minimum seconds client must wait before retrying.
    """

    DEFAULT_EXCLUDED_PATHS = {"/", "/docs", "/redoc", "/openapi.json"}

    def __init__(
        self,
        app,
        limiter: Optional[RateLimiter] = None,
        default_limit: Optional[int] = None,
        default_window: Optional[int] = None,
        route_limits: Optional[Dict[str, Tuple[int, int]]] = None,
        excluded_paths: Optional[Iterable[str]] = None,
    ):
        super().__init__(app)
        self.limiter = limiter
        self.default_limit = default_limit or settings.RATE_LIMIT_DEFAULT_LIMIT
        self.default_window = default_window or settings.RATE_LIMIT_DEFAULT_WINDOW
        self.excluded_paths: Set[str] = (
            set(excluded_paths) if excluded_paths is not None else set(self.DEFAULT_EXCLUDED_PATHS)
        )
        # Route-specific limit mapping:
        # e.g. {"GET:/api/data": (60, 60), "POST:/api/expensive": (5, 60)}
        if route_limits is not None:
            self.route_limits = route_limits
        else:
            self.route_limits = {
                "GET:/api/data": (settings.RATE_LIMIT_DATA_LIMIT, settings.RATE_LIMIT_DATA_WINDOW),
                "POST:/api/expensive": (settings.RATE_LIMIT_EXPENSIVE_LIMIT, settings.RATE_LIMIT_EXPENSIVE_WINDOW),
            }

    def _get_limiter(self) -> RateLimiter:
        return self.limiter or get_rate_limiter()

    def resolve_route_limits(self, method: str, path: str) -> Tuple[int, int, str]:
        """
        Determines the limit, window, and scoping route key for a given method and path.
        """
        method_upper = method.upper()
        method_path_key = f"{method_upper}:{path}"

        # 1. Exact Method + Path match
        if method_path_key in self.route_limits:
            limit, window = self.route_limits[method_path_key]
            return limit, window, method_path_key

        # 2. Path-only match
        if path in self.route_limits:
            limit, window = self.route_limits[path]
            return limit, window, path

        # 3. Default fallback
        return self.default_limit, self.default_window, "default"

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Skip rate limiting for explicitly excluded paths (docs, openapi schema, etc.)
        if request.url.path in self.excluded_paths:
            return await call_next(request)

        # Extract client identifier
        client_id = extract_client_identifier(request)

        # Resolve route-specific or default limits
        limit, window, route_key = self.resolve_route_limits(request.method, request.url.path)

        # Scope the client ID to the route key so limits on different endpoints are isolated
        scoped_client_id = f"{client_id}:{route_key}"

        # Invoke rate limiter
        limiter = self._get_limiter()
        result: RateLimitResult = await limiter.is_allowed(
            client_id=scoped_client_id,
            limit=limit,
            window_seconds=window,
        )

        reset_seconds = int(math.ceil(result.reset_after))

        # Handle rate limit exceeded: HTTP 429
        if not result.allowed:
            retry_seconds = int(math.ceil(result.retry_after or 1.0))
            logger.warning(
                "Rate limit exceeded for '%s' on %s %s. Limit: %d, Retry-After: %ds",
                client_id,
                request.method,
                request.url.path,
                result.limit,
                retry_seconds,
            )

            error_payload = {
                "error": "Too Many Requests",
                "message": f"Rate limit exceeded. Try again in {retry_seconds} seconds.",
                "retry_after": retry_seconds,
                "limit": result.limit,
                "remaining": result.remaining,
            }

            headers = {
                "X-RateLimit-Limit": str(result.limit),
                "X-RateLimit-Remaining": str(result.remaining),
                "X-RateLimit-Reset": str(reset_seconds),
                "Retry-After": str(retry_seconds),
            }

            return JSONResponse(
                status_code=429,
                content=error_payload,
                headers=headers,
            )

        # Request is allowed: proceed downstream
        response = await call_next(request)

        # Attach standard rate limit headers to downstream response
        response.headers["X-RateLimit-Limit"] = str(result.limit)
        response.headers["X-RateLimit-Remaining"] = str(result.remaining)
        response.headers["X-RateLimit-Reset"] = str(reset_seconds)

        return response
