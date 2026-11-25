"""
Redis Database Client Module for Rate Limiting.

=============================================================================
UNSTRUCTURED DATA MODEL STRUCTURE FOR RATE LIMITING (REDIS)
=============================================================================

Redis is the industry-standard database for distributed rate limiting due to
its sub-millisecond in-memory read/write performance, atomic operations, and
built-in key TTL (Time-To-Live) expiration.

Below are the core unstructured data models supported by this client:

-----------------------------------------------------------------------------
1. FIXED WINDOW COUNTER MODEL
-----------------------------------------------------------------------------
- Data Type: String (Integer Counter)
- Key Pattern:
    ratelimit:fixed:{client_id}:{endpoint}:{window_timestamp}
    Example: "ratelimit:fixed:ip_192.168.1.10:get_health:1700000000"
- Fields / Value:
    Integer representing request count (e.g. "1", "2", "3" ...)
- TTL (Expiration):
    Set to window duration (e.g. 60 seconds).
    Once expired, Redis automatically evicts the key, naturally resetting the count.
- Atomic Operations:
    INCR key -> if 1, EXPIRE key 60 -> if > limit, REJECT (HTTP 429).

-----------------------------------------------------------------------------
2. SLIDING WINDOW LOG MODEL (Exact Sliding Window)
-----------------------------------------------------------------------------
- Data Type: Sorted Set (ZSET)
- Key Pattern:
    ratelimit:sliding_log:{client_id}:{endpoint}
    Example: "ratelimit:sliding_log:usr_8921:checkout"
- Score:
    Unix timestamp in milliseconds (float: e.g. 1700000000123.45)
- Member:
    Unique request identifier: "{timestamp_ms}:{uuid4}"
- TTL (Expiration):
    Window duration + safety buffer (e.g. window_seconds + 5).
- Atomic Operations:
    1. ZREMRANGEBYSCORE key 0 (now_ms - window_ms)   # Remove logs outside window
    2. ZCARD key                                      # Count requests in window
    3. If count < limit: ZADD key now_ms req_id       # Record new request
    4. EXPIRE key window_seconds

-----------------------------------------------------------------------------
3. SLIDING WINDOW COUNTER MODEL (Hybrid / Segmented)
-----------------------------------------------------------------------------
- Data Type: Hash (HSET)
- Key Pattern:
    ratelimit:sliding_counter:{client_id}:{endpoint}
    Example: "ratelimit:sliding_counter:apikey_prod_99:search"
- Fields (Hash Fields):
    "{prev_window_timestamp}" (int): Total requests recorded in previous window
    "{curr_window_timestamp}" (int): Total requests recorded in current window
- TTL (Expiration):
    2 * window_duration_seconds (covers current and previous window periods).
- Calculation:
    weight = (window_size - (now - curr_window_start)) / window_size
    estimated_requests = (prev_count * weight) + curr_count

-----------------------------------------------------------------------------
4. TOKEN BUCKET MODEL
-----------------------------------------------------------------------------
- Data Type: Hash (HSET)
- Key Pattern:
    ratelimit:token_bucket:{client_id}:{endpoint}
    Example: "ratelimit:token_bucket:ip_10.0.0.1:transcode_video"
- Fields (Hash Fields):
    "tokens" (float): Number of tokens currently remaining in the bucket (e.g. "4.75")
    "last_updated" (float): Unix epoch timestamp of last refill (e.g. "1700000000.125")
- TTL (Expiration):
    ceil(bucket_capacity / refill_rate_per_sec) seconds.
    Evicts the key when idle long enough for the bucket to naturally be full.
- Atomic Operations:
    Evaluated atomically via Lua script to prevent race conditions during token refill.

=============================================================================
"""

import logging
from typing import Optional
import redis.asyncio as aioredis
from redis.asyncio import Redis, ConnectionPool
from redis.exceptions import ConnectionError as RedisConnectionError, TimeoutError as RedisTimeoutError, RedisError
from config.settings import settings

logger = logging.getLogger(__name__)

# Reusable connection pool and client instances
_redis_pool: Optional[ConnectionPool] = None
redis_client: Optional[Redis] = None


def get_redis_client() -> Optional[Redis]:
    """
    Returns the reusable Redis client instance.
    If not yet initialized or unavailable, returns None.
    """
    return redis_client


async def init_redis() -> Optional[Redis]:
    """
    Initializes a reusable Redis connection pool using environment variables.
    Handles connection errors gracefully without crashing the application.
    """
    global _redis_pool, redis_client

    if redis_client is not None:
        return redis_client

    try:
        logger.info("Initializing Redis connection to %s:%s (db=%s)...",
                    settings.REDIS_HOST, settings.REDIS_PORT, settings.REDIS_DB)

        # Build connection pool using settings
        if settings.REDIS_URL:
            _redis_pool = ConnectionPool.from_url(
                settings.REDIS_URL,
                max_connections=settings.REDIS_MAX_CONNECTIONS,
                socket_timeout=settings.REDIS_SOCKET_TIMEOUT,
                socket_connect_timeout=settings.REDIS_CONNECT_TIMEOUT,
                decode_responses=True,
            )
        else:
            _redis_pool = ConnectionPool(
                host=settings.REDIS_HOST,
                port=settings.REDIS_PORT,
                password=settings.REDIS_PASSWORD,
                db=settings.REDIS_DB,
                max_connections=settings.REDIS_MAX_CONNECTIONS,
                socket_timeout=settings.REDIS_SOCKET_TIMEOUT,
                socket_connect_timeout=settings.REDIS_CONNECT_TIMEOUT,
                decode_responses=True,
            )

        client = Redis(connection_pool=_redis_pool)

        # Ping Redis to verify connection health
        await client.ping()
        redis_client = client
        logger.info("Successfully connected to Redis at %s:%s", settings.REDIS_HOST, settings.REDIS_PORT)
        return redis_client

    except (RedisConnectionError, RedisTimeoutError) as err:
        logger.warning(
            "Redis is currently unavailable at %s:%s - %s. Rate limiting fallback will be used if required.",
            settings.REDIS_HOST, settings.REDIS_PORT, err
        )
        redis_client = None
        return None
    except RedisError as err:
        logger.error("Redis error encountered during initialization: %s", err)
        redis_client = None
        return None
    except Exception as err:
        logger.error("Unexpected error connecting to Redis: %s", err, exc_info=True)
        redis_client = None
        return None


async def close_redis() -> None:
    """
    Cleanly closes the Redis connection pool and releases resources.
    """
    global _redis_pool, redis_client

    if redis_client is not None:
        try:
            logger.info("Closing Redis client connection...")
            await redis_client.aclose()
        except Exception as err:
            logger.warning("Error while closing Redis client: %s", err)
        finally:
            redis_client = None

    if _redis_pool is not None:
        try:
            await _redis_pool.disconnect()
        except Exception as err:
            logger.warning("Error while disconnecting Redis connection pool: %s", err)
        finally:
            _redis_pool = None

    logger.info("Redis connection cleanly closed.")
