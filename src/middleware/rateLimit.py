"""
Alias module for camelCase naming convention compatibility (rateLimit.py).
Re-exports everything from rate_limit.py.
"""

from src.middleware.rate_limit import (
    RateLimitMiddleware,
    extract_client_identifier,
)

__all__ = [
    "RateLimitMiddleware",
    "extract_client_identifier",
]
