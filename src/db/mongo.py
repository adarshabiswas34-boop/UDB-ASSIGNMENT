"""
MongoDB Database Client Module for Rate Limiting.

=============================================================================
UNSTRUCTURED DATA MODEL STRUCTURE FOR RATE LIMITING (MONGODB)
=============================================================================

MongoDB provides persistent, flexible document storage. For rate limiting,
MongoDB's TTL (Time-To-Live) index automatically purges expired rate limit
documents in the background.

-----------------------------------------------------------------------------
1. COLLECTION DEFINITION
-----------------------------------------------------------------------------
- Database: {MONGODB_DB_NAME} (e.g. "rate_limiter_db")
- Collection: "rate_limits"

-----------------------------------------------------------------------------
2. DOCUMENT STRUCTURE (Unstructured JSON/BSON)
-----------------------------------------------------------------------------
{
    "_id": "rl:ip_192.168.1.1:/api/v1/resource:1700000000",   // Deterministic composite key
    "client_id": "ip_192.168.1.1",                            // Client identity (IP, user_id, api_key)
    "endpoint": "/api/v1/resource",                           // Endpoint path or resource identifier
    "window_start": ISODate("2026-11-24T16:00:00.000Z"),      // Start of current measurement window
    "request_count": 5,                                       // Current count of requests
    "max_requests": 60,                                       // Request quota allowed for window
    "window_seconds": 60,                                     // Window length in seconds
    "expires_at": ISODate("2026-11-24T16:01:00.000Z"),        // Timestamp when document expires (TTL target)
    "metadata": {                                             // Extensible unstructured details
        "tier": "basic",
        "last_seen": ISODate("2026-11-24T16:00:32.410Z"),
        "user_agent": "HTTPClient/1.0"
    }
}

-----------------------------------------------------------------------------
3. TTL & PERFORMANCE INDEXES
-----------------------------------------------------------------------------
- TTL Index on `expires_at`:
    db.rate_limits.create_index(
        [("expires_at", 1)],
        expireAfterSeconds=0
    )
    MongoDB automatically deletes expired documents once `expires_at` <= current time.

- Compound Query Index:
    db.rate_limits.create_index(
        [("client_id", 1), ("endpoint", 1), ("window_start", -1)]
    )

=============================================================================
"""

import logging
from typing import Optional
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError, PyMongoError
from config.settings import settings

logger = logging.getLogger(__name__)

# Reusable client and database instances
mongo_client: Optional[AsyncIOMotorClient] = None
mongo_db: Optional[AsyncIOMotorDatabase] = None


def get_mongo_db() -> Optional[AsyncIOMotorDatabase]:
    """
    Returns the reusable MongoDB database instance.
    If not yet initialized or unavailable, returns None.
    """
    return mongo_db


def get_mongo_client() -> Optional[AsyncIOMotorClient]:
    """
    Returns the reusable AsyncIOMotorClient instance.
    """
    return mongo_client


async def init_mongo() -> Optional[AsyncIOMotorDatabase]:
    """
    Initializes a reusable MongoDB client and configures TTL indexes.
    Handles connection errors gracefully without crashing the application.
    """
    global mongo_client, mongo_db

    if mongo_db is not None:
        return mongo_db

    try:
        logger.info("Initializing MongoDB connection to %s (db=%s)...",
                    settings.MONGODB_URL, settings.MONGODB_DB_NAME)

        client = AsyncIOMotorClient(
            settings.MONGODB_URL,
            serverSelectionTimeoutMS=settings.MONGODB_SERVER_SELECTION_TIMEOUT_MS,
        )

        # Trigger quick server check to catch connection errors early
        await client.admin.command("ping")

        mongo_client = client
        mongo_db = client[settings.MONGODB_DB_NAME]

        # Ensure TTL index on rate_limits collection (expires_at with expireAfterSeconds=0)
        await mongo_db["rate_limits"].create_index("expires_at", expireAfterSeconds=0)
        await mongo_db["rate_limits"].create_index([("client_id", 1), ("endpoint", 1)])

        logger.info("Successfully connected to MongoDB and verified TTL indexes.")
        return mongo_db

    except (ConnectionFailure, ServerSelectionTimeoutError) as err:
        logger.warning(
            "MongoDB is currently unavailable at %s - %s. Rate limiting fallback will be used if required.",
            settings.MONGODB_URL, err
        )
        mongo_client = None
        mongo_db = None
        return None
    except PyMongoError as err:
        logger.error("PyMongo error during MongoDB initialization: %s", err)
        mongo_client = None
        mongo_db = None
        return None
    except Exception as err:
        logger.error("Unexpected error connecting to MongoDB: %s", err, exc_info=True)
        mongo_client = None
        mongo_db = None
        return None


async def close_mongo() -> None:
    """
    Cleanly closes the MongoDB client connection.
    """
    global mongo_client, mongo_db

    if mongo_client is not None:
        try:
            logger.info("Closing MongoDB connection...")
            mongo_client.close()
        except Exception as err:
            logger.warning("Error while closing MongoDB client: %s", err)
        finally:
            mongo_client = None
            mongo_db = None

    logger.info("MongoDB connection cleanly closed.")
