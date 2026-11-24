import pytest
from unittest.mock import AsyncMock, MagicMock
from redis.exceptions import ConnectionError as RedisConnError, TimeoutError as RedisTimeoutError
from src.limiter.rate_limiter import (
    RateLimiter,
    RateLimitResult,
    SlidingWindowLogLimiter,
    TokenBucketLimiter,
    FixedWindowLimiter,
)
from src.limiter.rateLimiter import RateLimiter as CamelCaseRateLimiter


def test_rate_limiter_module_exports():
    """Verify camelCase and snake_case modules both export cleanly."""
    assert CamelCaseRateLimiter is RateLimiter


@pytest.mark.anyio
async def test_fail_open_when_redis_unavailable():
    """
    Test Fail-Open behavior:
    When Redis is unavailable and fail_closed=False (default),
    requests must be permitted to avoid impacting users during outages.
    """
    mock_redis = AsyncMock()
    mock_redis.eval.side_effect = RedisConnError("Connection refused")

    limiter = RateLimiter(redis=mock_redis, fail_closed=False)
    result = await limiter.is_allowed("client_ip_127.0.0.1", limit=10, window_seconds=60)

    assert result.allowed is True
    assert result.limit == 10
    assert result.remaining == 10
    assert result.retry_after is None
    assert result.client_id == "client_ip_127.0.0.1"


@pytest.mark.anyio
async def test_fail_closed_when_redis_unavailable():
    """
    Test Fail-Closed behavior:
    When Redis is unavailable and fail_closed=True,
    requests must be rejected to protect downstream backends from overload.
    """
    mock_redis = AsyncMock()
    mock_redis.eval.side_effect = RedisTimeoutError("Socket timed out")

    limiter = RateLimiter(redis=mock_redis, fail_closed=True)
    result = await limiter.is_allowed("api_key_xyz", limit=5, window_seconds=30)

    assert result.allowed is False
    assert result.limit == 5
    assert result.remaining == 0
    assert result.retry_after == 30.0
    assert result.client_id == "api_key_xyz"


@pytest.mark.anyio
async def test_sliding_window_log_allowed_request():
    """Verify sliding window log allows request when under quota."""
    mock_redis = AsyncMock()
    # Mock Redis Lua response: {allowed=1, remaining=4, reset_after=58.2, retry_after=0.0}
    mock_redis.eval.return_value = [1, 4, "58.200", "0.0"]

    limiter = SlidingWindowLogLimiter(redis=mock_redis)
    result = await limiter.is_allowed("user_100", limit=5, window_seconds=60)

    assert result.allowed is True
    assert result.limit == 5
    assert result.remaining == 4
    assert result.reset_after == 58.2
    assert result.retry_after is None
    mock_redis.eval.assert_called_once()


@pytest.mark.anyio
async def test_sliding_window_log_rate_limit_exceeded():
    """Verify sliding window log rejects request when quota exceeded."""
    mock_redis = AsyncMock()
    # Mock Redis Lua response: {allowed=0, remaining=0, reset_after=22.45, retry_after=22.45}
    mock_redis.eval.return_value = [0, 0, "22.450", "22.450"]

    limiter = SlidingWindowLogLimiter(redis=mock_redis)
    result = await limiter.is_allowed("user_100", limit=5, window_seconds=60)

    assert result.allowed is False
    assert result.remaining == 0
    assert result.reset_after == 22.45
    assert result.retry_after == 22.45


@pytest.mark.anyio
async def test_token_bucket_limiter():
    """Verify Token Bucket rate limiter evaluations."""
    mock_redis = AsyncMock()
    # Allowed response
    mock_redis.eval.return_value = [1, 7, "1.500", "0.0"]

    tb_limiter = TokenBucketLimiter(redis=mock_redis)
    res1 = await tb_limiter.is_allowed("client_tb_1", limit=10, window_seconds=10)
    assert res1.allowed is True
    assert res1.remaining == 7
    assert res1.retry_after is None

    # Exceeded response
    mock_redis.eval.return_value = [0, 0, "4.000", "2.500"]
    res2 = await tb_limiter.is_allowed("client_tb_1", limit=10, window_seconds=10)
    assert res2.allowed is False
    assert res2.remaining == 0
    assert res2.retry_after == 2.5


@pytest.mark.anyio
async def test_fixed_window_limiter():
    """Verify Fixed Window rate limiter evaluations."""
    mock_redis = AsyncMock()
    mock_redis.eval.return_value = [1, 9, "45.0", "0.0"]

    fw_limiter = FixedWindowLimiter(redis=mock_redis)
    res = await fw_limiter.is_allowed("client_fw", limit=10, window_seconds=60)
    assert res.allowed is True
    assert res.remaining == 9
