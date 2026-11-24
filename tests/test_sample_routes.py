import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from config.settings import settings
from src.main import create_app
from src.limiter.rate_limiter import RateLimitResult


def test_standard_data_route_allowed():
    """Verify GET /api/data uses the standard configured limit (60) and returns 200."""
    app = create_app()
    mock_limiter = AsyncMock()
    mock_limiter.is_allowed.return_value = RateLimitResult(
        allowed=True,
        limit=settings.RATE_LIMIT_DATA_LIMIT,
        remaining=settings.RATE_LIMIT_DATA_LIMIT - 1,
        reset_after=59.0,
        retry_after=None,
        client_id="apikey:user_standard:GET:/api/data",
    )

    with patch("src.middleware.rate_limit.get_rate_limiter", return_value=mock_limiter):
        with TestClient(app) as client:
            response = client.get("/api/data", headers={"x-api-key": "user_standard"})

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "success"
            assert len(data["items"]) == 3
            assert response.headers.get("X-RateLimit-Limit") == str(settings.RATE_LIMIT_DATA_LIMIT)
            assert response.headers.get("X-RateLimit-Remaining") == str(settings.RATE_LIMIT_DATA_LIMIT - 1)
            assert "X-RateLimit-Reset" in response.headers
            mock_limiter.is_allowed.assert_called_once_with(
                client_id="apikey:user_standard:GET:/api/data",
                limit=settings.RATE_LIMIT_DATA_LIMIT,
                window_seconds=settings.RATE_LIMIT_DATA_WINDOW,
            )


def test_expensive_route_stricter_limit():
    """Verify POST /api/expensive uses the stricter limit (5) and returns 200."""
    app = create_app()
    mock_limiter = AsyncMock()
    mock_limiter.is_allowed.return_value = RateLimitResult(
        allowed=True,
        limit=settings.RATE_LIMIT_EXPENSIVE_LIMIT,
        remaining=settings.RATE_LIMIT_EXPENSIVE_LIMIT - 1,
        reset_after=55.0,
        retry_after=None,
        client_id="apikey:user_expensive:POST:/api/expensive",
    )

    with patch("src.middleware.rate_limit.get_rate_limiter", return_value=mock_limiter):
        with TestClient(app) as client:
            response = client.post(
                "/api/expensive",
                json={"operation": "simulation", "records": 100},
                headers={"x-api-key": "user_expensive"},
            )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "success"
            assert data["operation"] == "simulation"
            assert response.headers.get("X-RateLimit-Limit") == str(settings.RATE_LIMIT_EXPENSIVE_LIMIT)
            assert response.headers.get("X-RateLimit-Remaining") == str(settings.RATE_LIMIT_EXPENSIVE_LIMIT - 1)
            mock_limiter.is_allowed.assert_called_once_with(
                client_id="apikey:user_expensive:POST:/api/expensive",
                limit=settings.RATE_LIMIT_EXPENSIVE_LIMIT,
                window_seconds=settings.RATE_LIMIT_EXPENSIVE_WINDOW,
            )


def test_expensive_route_rate_limit_exceeded_returns_429():
    """Verify POST /api/expensive returns 429 when stricter quota is exhausted."""
    app = create_app()
    mock_limiter = AsyncMock()
    mock_limiter.is_allowed.return_value = RateLimitResult(
        allowed=False,
        limit=settings.RATE_LIMIT_EXPENSIVE_LIMIT,
        remaining=0,
        reset_after=28.4,
        retry_after=28.4,
        client_id="apikey:user_exceeded:POST:/api/expensive",
    )

    with patch("src.middleware.rate_limit.get_rate_limiter", return_value=mock_limiter):
        with TestClient(app) as client:
            response = client.post(
                "/api/expensive",
                json={"operation": "heavy_export"},
                headers={"x-api-key": "user_exceeded"},
            )

            assert response.status_code == 429
            assert response.headers.get("Retry-After") == "29"
            assert response.headers.get("X-RateLimit-Limit") == str(settings.RATE_LIMIT_EXPENSIVE_LIMIT)
            assert response.headers.get("X-RateLimit-Remaining") == "0"

            body = response.json()
            assert body["error"] == "Too Many Requests"
            assert body["retry_after"] == 29
            assert body["limit"] == settings.RATE_LIMIT_EXPENSIVE_LIMIT


def test_route_scoping_isolation():
    """Verify calling /api/expensive does not affect /api/data limits."""
    from src.middleware.rate_limit import RateLimitMiddleware

    mw = RateLimitMiddleware(app=None)
    limit_data, win_data, key_data = mw.resolve_route_limits("GET", "/api/data")
    assert limit_data == settings.RATE_LIMIT_DATA_LIMIT
    assert win_data == settings.RATE_LIMIT_DATA_WINDOW
    assert key_data == "GET:/api/data"

    limit_exp, win_exp, key_exp = mw.resolve_route_limits("POST", "/api/expensive")
    assert limit_exp == settings.RATE_LIMIT_EXPENSIVE_LIMIT
    assert win_exp == settings.RATE_LIMIT_EXPENSIVE_WINDOW
    assert key_exp == "POST:/api/expensive"

    # Default fallback for other paths
    limit_other, win_other, key_other = mw.resolve_route_limits("GET", "/api/unknown")
    assert limit_other == settings.RATE_LIMIT_DEFAULT_LIMIT
    assert key_other == "default"
