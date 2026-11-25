"""
Database client package providing reusable connection instances for Redis and MongoDB.
"""

from typing import Optional
from config.settings import settings
from src.db.redis import (
    redis_client,
    get_redis_client,
    init_redis,
    close_redis,
)
from src.db.mongo import (
    mongo_client,
    mongo_db,
    get_mongo_client,
    get_mongo_db,
    init_mongo,
    close_mongo,
)


async def init_db() -> dict:
    """
    Initializes database connections (Redis & MongoDB).
    Returns a dictionary of connection statuses.
    Skips MongoDB when pointing to localhost in production (no server available).
    """
    redis_inst = await init_redis()

    # Skip MongoDB init if we're in production but still pointing at localhost
    # (no MongoDB deployed) — avoids noisy connection-refused errors in logs
    skip_mongo = (
        settings.ENVIRONMENT == "production"
        and "localhost" in settings.MONGODB_URL
    )
    if skip_mongo:
        import logging
        logging.getLogger(__name__).info(
            "Skipping MongoDB init in production (MONGODB_URL points to localhost)."
        )
        mongo_inst = None
    else:
        mongo_inst = await init_mongo()

    return {
        "redis_connected": redis_inst is not None,
        "mongo_connected": mongo_inst is not None,
    }


async def close_db() -> None:
    """
    Closes all active database client connections.
    """
    await close_redis()
    await close_mongo()


__all__ = [
    # Redis
    "redis_client",
    "get_redis_client",
    "init_redis",
    "close_redis",
    # MongoDB
    "mongo_client",
    "mongo_db",
    "get_mongo_client",
    "get_mongo_db",
    "init_mongo",
    "close_mongo",
    # Lifecycle
    "init_db",
    "close_db",
]
