"""
Alias module for camelCase naming convention compatibility (rateLimiter.py).
Re-exports everything from rate_limiter.py.
"""

from src.limiter.rate_limiter import (
    RateLimiter,
    RateLimitResult,
    SlidingWindowLogLimiter,
    TokenBucketLimiter,
    FixedWindowLimiter,
    get_rate_limiter,
)

__all__ = [
    "RateLimiter",
    "RateLimitResult",
    "SlidingWindowLogLimiter",
    "TokenBucketLimiter",
    "FixedWindowLimiter",
    "get_rate_limiter",
]
