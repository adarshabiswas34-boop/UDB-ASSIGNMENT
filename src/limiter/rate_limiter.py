"""
Rate Limiter Core Logic Module.

Provides atomic rate limiting backed by Redis (with in-memory fallback) using
the Sliding Window Log algorithm, with support for Token Bucket and Fixed Window.

Database Failure Handling:
--------------------------
Configurable via `fail_closed` parameter or `settings.RATE_LIMIT_FAIL_CLOSED`:
- Fail-Open (default: False -> fail-open):
    If Redis or the database is unavailable, allows requests through to avoid
    disrupting service for legitimate clients during an outage.
- Fail-Closed (True):
    If Redis or the database is unavailable, rejects requests (allowed=False)
    to protect downstream backends from catastrophic overloads.
"""

import math
import time
import uuid
import logging
from dataclasses import dataclass
from typing import Optional
from redis.asyncio import Redis
from redis.exceptions import RedisError, ConnectionError as RedisConnError, TimeoutError as RedisTimeoutError
from config.settings import settings
from src.db.redis import get_redis_client

logger = logging.getLogger(__name__)


@dataclass
class RateLimitResult:
    """
    Result returned by the rate limiter for each check.

    Attributes:
        allowed: Whether the request is permitted under current quota.
        limit: The maximum allowed requests within the configured window.
        remaining: Quota remaining in the current window (>= 0).
        reset_after: Floating-point seconds until quota resets or refreshes.
        retry_after: Floating-point seconds client must wait if rejected (None if allowed).
        client_id: The client identifier evaluated.
    """
    allowed: bool
    limit: int
    remaining: int
    reset_after: float
    retry_after: Optional[float] = None
    client_id: str = ""


# =============================================================================
# ATOMIC LUA SCRIPTS FOR REDIS
# =============================================================================

# Sliding Window Log Lua Script:
# Executes atomically in Redis. Prunes expired requests older than (now - window),
# counts elements in sorted set, and adds new request if quota permits.
SLIDING_WINDOW_LOG_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local request_id = ARGV[4]
local cost = tonumber(ARGV[5])
local expire_seconds = tonumber(ARGV[6])

local clear_before = now - window_ms

-- 1. Remove entries outside sliding window
redis.call('ZREMRANGEBYSCORE', key, '-inf', clear_before)

-- 2. Current request count within window
local current_count = redis.call('ZCARD', key)

local allowed = 0
local remaining = 0
local reset_after = 0.0
local retry_after = 0.0

-- Find oldest timestamp in current window to determine reset time
local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
local oldest_ts = now
if #oldest > 0 then
    oldest_ts = tonumber(oldest[2])
end
reset_after = math.max(0, ((oldest_ts + window_ms) - now) / 1000)

if (current_count + cost) <= limit then
    allowed = 1
    redis.call('ZADD', key, now, request_id)
    redis.call('EXPIRE', key, expire_seconds)
    current_count = current_count + cost
    remaining = math.max(0, limit - current_count)
    retry_after = 0.0
else
    allowed = 0
    remaining = math.max(0, limit - current_count)
    retry_after = reset_after
    if retry_after <= 0 then
        retry_after = 0.001
    end
end

return {allowed, remaining, tostring(reset_after), tostring(retry_after)}
"""

# Token Bucket Lua Script:
# Maintains token count and last refill timestamp atomically.
TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local refill_rate = tonumber(ARGV[3]) -- tokens per second
local cost = tonumber(ARGV[4])
local expire_seconds = tonumber(ARGV[5])

local data = redis.call('HMGET', key, 'tokens', 'last_updated')
local tokens = tonumber(data[1])
local last_updated = tonumber(data[2])

if not tokens then
    tokens = capacity
    last_updated = now
else
    local elapsed = math.max(0, (now - last_updated) / 1000)
    tokens = math.min(capacity, tokens + (elapsed * refill_rate))
    last_updated = now
end

local allowed = 0
local remaining = 0
local retry_after = 0.0
local reset_after = math.max(0, (capacity - tokens) / refill_rate)

if tokens >= cost then
    allowed = 1
    tokens = tokens - cost
    remaining = math.floor(tokens)
    retry_after = 0.0
else
    allowed = 0
    remaining = math.floor(tokens)
    local needed = cost - tokens
    retry_after = math.max(0.001, needed / refill_rate)
end

redis.call('HMSET', key, 'tokens', tokens, 'last_updated', last_updated)
redis.call('EXPIRE', key, expire_seconds)

return {allowed, remaining, tostring(reset_after), tostring(retry_after)}
"""

# Fixed Window Lua Script:
# Increments atomic integer counter with window TTL.
FIXED_WINDOW_LUA = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local window_seconds = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])

local current = redis.call('INCRBY', key, cost)
local ttl = redis.call('TTL', key)

if ttl == -1 or current == cost then
    redis.call('EXPIRE', key, window_seconds)
    ttl = window_seconds
end

local allowed = 0
local remaining = 0
local retry_after = 0.0

if current <= limit then
    allowed = 1
    remaining = limit - current
    retry_after = 0.0
else
    allowed = 0
    remaining = 0
    retry_after = math.max(0.001, ttl)
end

return {allowed, remaining, tostring(ttl), tostring(retry_after)}
"""


class SlidingWindowLogLimiter:
    """
    Sliding Window Log rate limiter backed by Redis.
    Uses atomic Lua scripts and Sorted Sets (ZSET) to record exact timestamps.
    """

    def __init__(self, redis: Optional[Redis] = None, prefix: str = "ratelimit:swl"):
        self.redis = redis
        self.prefix = prefix
        self._script = None

    def _get_client(self) -> Optional[Redis]:
        return self.redis or get_redis_client()

    async def is_allowed(
        self,
        client_id: str,
        limit: int,
        window_seconds: int,
        cost: int = 1,
    ) -> RateLimitResult:
        client = self._get_client()
        if client is None:
            raise RedisConnError("Redis client is not connected")

        now_ms = int(time.time() * 1000)
        window_ms = int(window_seconds * 1000)
        req_id = f"{now_ms}:{uuid.uuid4().hex[:8]}"
        expire_seconds = int(math.ceil(window_seconds + 5))
        key = f"{self.prefix}:{client_id}"

        # Register / run Lua script atomically
        res = await client.eval(
            SLIDING_WINDOW_LOG_LUA,
            1,
            key,
            now_ms,
            window_ms,
            limit,
            req_id,
            cost,
            expire_seconds,
        )

        allowed = bool(res[0] == 1)
        remaining = int(res[1])
        reset_after = float(res[2])
        retry_val = float(res[3])
        retry_after = retry_val if not allowed else None

        return RateLimitResult(
            allowed=allowed,
            limit=limit,
            remaining=remaining,
            reset_after=round(reset_after, 3),
            retry_after=round(retry_after, 3) if retry_after is not None else None,
            client_id=client_id,
        )


class TokenBucketLimiter:
    """
    Token Bucket rate limiter backed by Redis.
    Allows bursts up to bucket capacity and refills tokens at a constant rate.
    """

    def __init__(self, redis: Optional[Redis] = None, prefix: str = "ratelimit:tb"):
        self.redis = redis
        self.prefix = prefix

    def _get_client(self) -> Optional[Redis]:
        return self.redis or get_redis_client()

    async def is_allowed(
        self,
        client_id: str,
        limit: int,
        window_seconds: int,
        cost: int = 1,
    ) -> RateLimitResult:
        client = self._get_client()
        if client is None:
            raise RedisConnError("Redis client is not connected")

        now_ms = int(time.time() * 1000)
        refill_rate = limit / float(window_seconds)  # tokens per second
        expire_seconds = int(math.ceil(limit / refill_rate) + 10)
        key = f"{self.prefix}:{client_id}"

        res = await client.eval(
            TOKEN_BUCKET_LUA,
            1,
            key,
            now_ms,
            limit,
            refill_rate,
            cost,
            expire_seconds,
        )

        allowed = bool(res[0] == 1)
        remaining = int(res[1])
        reset_after = float(res[2])
        retry_val = float(res[3])
        retry_after = retry_val if not allowed else None

        return RateLimitResult(
            allowed=allowed,
            limit=limit,
            remaining=remaining,
            reset_after=round(reset_after, 3),
            retry_after=round(retry_after, 3) if retry_after is not None else None,
            client_id=client_id,
        )


class FixedWindowLimiter:
    """
    Fixed Window rate limiter backed by Redis.
    Uses atomic counter increments with window expiration.
    """

    def __init__(self, redis: Optional[Redis] = None, prefix: str = "ratelimit:fixed"):
        self.redis = redis
        self.prefix = prefix

    def _get_client(self) -> Optional[Redis]:
        return self.redis or get_redis_client()

    async def is_allowed(
        self,
        client_id: str,
        limit: int,
        window_seconds: int,
        cost: int = 1,
    ) -> RateLimitResult:
        client = self._get_client()
        if client is None:
            raise RedisConnError("Redis client is not connected")

        window_id = int(time.time() // window_seconds)
        key = f"{self.prefix}:{client_id}:{window_id}"

        res = await client.eval(
            FIXED_WINDOW_LUA,
            1,
            key,
            limit,
            window_seconds,
            cost,
        )

        allowed = bool(res[0] == 1)
        remaining = int(res[1])
        reset_after = float(res[2])
        retry_val = float(res[3])
        retry_after = retry_val if not allowed else None

        return RateLimitResult(
            allowed=allowed,
            limit=limit,
            remaining=remaining,
            reset_after=round(reset_after, 3),
            retry_after=round(retry_after, 3) if retry_after is not None else None,
            client_id=client_id,
        )


class RateLimiter:
    """
    High-level Rate Limiter facade.

    Features:
    - Default algorithm: Sliding Window Log (precise, burst-resistant).
    - Database failure handling:
        * When Redis is unavailable:
            - If fail_closed=False (fail-open, default): Request is allowed.
            - If fail_closed=True (fail-closed): Request is blocked.
    """

    def __init__(
        self,
        redis: Optional[Redis] = None,
        fail_closed: Optional[bool] = None,
        default_limit: Optional[int] = None,
        default_window: Optional[int] = None,
        algorithm: str = "sliding_window_log",
    ):
        self.redis = redis
        self.fail_closed = fail_closed if fail_closed is not None else settings.RATE_LIMIT_FAIL_CLOSED
        self.default_limit = default_limit or settings.RATE_LIMIT_DEFAULT_LIMIT
        self.default_window = default_window or settings.RATE_LIMIT_DEFAULT_WINDOW

        # Select underlying algorithm implementation
        if algorithm == "token_bucket":
            self.backend = TokenBucketLimiter(redis=redis)
        elif algorithm == "fixed_window":
            self.backend = FixedWindowLimiter(redis=redis)
        else:
            self.backend = SlidingWindowLogLimiter(redis=redis)

    async def is_allowed(
        self,
        client_id: str,
        limit: Optional[int] = None,
        window_seconds: Optional[int] = None,
        cost: int = 1,
    ) -> RateLimitResult:
        """
        Evaluates whether a request by `client_id` is permitted under rate limit quotas.

        Args:
            client_id: Unique client identity (API key, user ID, or IP address).
            limit: Maximum requests allowed in the window (defaults to settings).
            window_seconds: Duration of rate limit window (defaults to settings).
            cost: Weight / token cost of the incoming request (default: 1).

        Returns:
            RateLimitResult with allowed status, remaining quota, reset_after, and retry_after.
        """
        eff_limit = limit or self.default_limit
        eff_window = window_seconds or self.default_window

        try:
            return await self.backend.is_allowed(
                client_id=client_id,
                limit=eff_limit,
                window_seconds=eff_window,
                cost=cost,
            )

        except (RedisConnError, RedisTimeoutError, RedisError, Exception) as err:
            logger.warning(
                "Rate limiter backend failure for client '%s': %s. Fail-mode: %s",
                client_id,
                err,
                "FAIL-CLOSED" if self.fail_closed else "FAIL-OPEN",
            )

            if self.fail_closed:
                # Fail-Closed: Block requests to protect downstream infrastructure
                return RateLimitResult(
                    allowed=False,
                    limit=eff_limit,
                    remaining=0,
                    reset_after=float(eff_window),
                    retry_after=float(eff_window),
                    client_id=client_id,
                )
            else:
                # Fail-Open: Allow requests to avoid disrupting legitimate clients
                return RateLimitResult(
                    allowed=True,
                    limit=eff_limit,
                    remaining=eff_limit,
                    reset_after=0.0,
                    retry_after=None,
                    client_id=client_id,
                )


# Global singleton instance
_default_limiter: Optional[RateLimiter] = None


def get_rate_limiter() -> RateLimiter:
    """
    Returns the application-wide default RateLimiter instance.
    """
    global _default_limiter
    if _default_limiter is None:
        _default_limiter = RateLimiter()
    return _default_limiter
