import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from src.middleware.rate_limit import RateLimitMiddleware, extract_client_identifier
from src.middleware.rateLimit import RateLimitMiddleware as CamelCaseRateLimitMiddleware
from src.limiter.rate_limiter import RateLimiter, RateLimitResult


def test_middleware_camel_case_import():
    """Verify camelCase alias re-exports correctly."""
    assert CamelCaseRateLimitMiddleware is RateLimitMiddleware


def test_extract_client_identifier():
    """Test client identifier extraction priority order."""
    # 1. API key header priority
    req1 = Request(scope={
        "type": "http",
        "headers": [(b"x-api-key", b"test_key_123"), (b"x-forwarded-for", b"10.0.0.1")],
        "client": ("192.168.1.1", 12345),
    })
    assert extract_client_identifier(req1) == "apikey:test_key_123"

    # 2. Bearer token
    req2 = Request(scope={
        "type": "http",
        "headers": [(b"authorization", b"Bearer token_secret_456")],
        "client": ("192.168.1.1", 12345),
    })
    assert extract_client_identifier(req2) == "bearer:token_secret_456"

    # 3. X-Forwarded-For header
    req3 = Request(scope={
        "type": "http",
        "headers": [(b"x-forwarded-for", b"203.0.113.195, 70.41.3.18")],
        "client": ("192.168.1.1", 12345),
    })
    assert extract_client_identifier(req3) == "ip:203.0.113.195"

    # 4. Direct socket client host
    req4 = Request(scope={
        "type": "http",
        "headers": [],
        "client": ("192.168.1.50", 12345),
    })
    assert extract_client_identifier(req4) == "ip:192.168.1.50"

    # 5. Fallback anonymous
    req5 = Request(scope={
        "type": "http",
        "headers": [],
        "client": None,
    })
    assert extract_client_identifier(req5) == "ip:anonymous"


def test_middleware_allowed_request():
    """Verify middleware injects standard headers on allowed requests."""
    app = FastAPI()
    mock_limiter = MagicMock(spec=RateLimiter)
    mock_limiter.is_allowed = AsyncMock(return_value=RateLimitResult(
        allowed=True,
        limit=50,
        remaining=49,
        reset_after=30.4,
        retry_after=None,
        client_id="apikey:valid_key",
    ))

    app.add_middleware(RateLimitMiddleware, limiter=mock_limiter, default_limit=50, default_window=60)

    @app.get("/test")
    def get_test():
        return {"data": "hello"}

    client = TestClient(app)
    response = client.get("/test", headers={"x-api-key": "valid_key"})

    assert response.status_code == 200
    assert response.json() == {"data": "hello"}
    assert response.headers.get("X-RateLimit-Limit") == "50"
    assert response.headers.get("X-RateLimit-Remaining") == "49"
    assert response.headers.get("X-RateLimit-Reset") == "31"
    assert "Retry-After" not in response.headers


def test_middleware_rate_limit_exceeded_returns_429():
    """Verify middleware returns HTTP 429 with JSON payload and Retry-After header."""
    app = FastAPI()
    mock_limiter = MagicMock(spec=RateLimiter)
    mock_limiter.is_allowed = AsyncMock(return_value=RateLimitResult(
        allowed=False,
        limit=10,
        remaining=0,
        reset_after=15.2,
        retry_after=15.2,
        client_id="apikey:blocked_key",
    ))

    app.add_middleware(RateLimitMiddleware, limiter=mock_limiter, default_limit=10, default_window=60)

    @app.get("/test")
    def get_test():
        return {"data": "should not reach"}

    client = TestClient(app)
    response = client.get("/test", headers={"x-api-key": "blocked_key"})

    assert response.status_code == 429
    assert response.headers.get("Retry-After") == "16"
    assert response.headers.get("X-RateLimit-Limit") == "10"
    assert response.headers.get("X-RateLimit-Remaining") == "0"
    assert response.headers.get("X-RateLimit-Reset") == "16"

    body = response.json()
    assert body["error"] == "Too Many Requests"
    assert "Rate limit exceeded" in body["message"]
    assert body["retry_after"] == 16
    assert body["limit"] == 10
    assert body["remaining"] == 0


def test_middleware_excluded_paths_bypassed():
    """Verify excluded paths (e.g. /docs) bypass rate limiter completely."""
    app = FastAPI()
    mock_limiter = MagicMock(spec=RateLimiter)
    mock_limiter.is_allowed = AsyncMock()

    app.add_middleware(
        RateLimitMiddleware,
        limiter=mock_limiter,
        excluded_paths={"/docs", "/health"},
    )

    @app.get("/health")
    def get_health():
        return {"status": "ok"}

    client = TestClient(app)
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    # Limiter should not have been called for excluded path
    mock_limiter.is_allowed.assert_not_called()
