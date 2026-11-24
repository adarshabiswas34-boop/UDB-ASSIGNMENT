"""
Database client package providing reusable connection instances for Redis and MongoDB.
"""

from typing import Optional
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
    """
    redis_inst = await init_redis()
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
