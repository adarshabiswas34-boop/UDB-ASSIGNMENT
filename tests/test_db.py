import pytest
from unittest.mock import AsyncMock, patch
from config.settings import settings
from src.db.redis import get_redis_client, init_redis, close_redis
from src.db.mongo import get_mongo_client, get_mongo_db, init_mongo, close_mongo
from src.db import init_db, close_db


def test_database_settings_loaded():
    """Verify database settings are loaded with sensible defaults."""
    assert settings.REDIS_HOST is not None
    assert settings.REDIS_PORT == 6379
    assert settings.REDIS_DB == 0
    assert settings.MONGODB_URL is not None
    assert settings.MONGODB_DB_NAME == "rate_limiter_db"


@pytest.mark.anyio
async def test_redis_connection_error_handled_cleanly():
    """Verify Redis init handles unreachable server gracefully without throwing."""
    # Point to non-existent port to force connection failure
    with patch.object(settings, "REDIS_HOST", "127.0.0.1"), \
         patch.object(settings, "REDIS_PORT", 59999), \
         patch.object(settings, "REDIS_URL", "redis://127.0.0.1:59999/0"), \
         patch.object(settings, "REDIS_CONNECT_TIMEOUT", 0.1):

        client = await init_redis()
        assert client is None
        assert get_redis_client() is None

        # Clean up
        await close_redis()


@pytest.mark.anyio
async def test_mongo_connection_error_handled_cleanly():
    """Verify MongoDB init handles unreachable server gracefully without throwing."""
    with patch.object(settings, "MONGODB_URL", "mongodb://127.0.0.1:59998"), \
         patch.object(settings, "MONGODB_SERVER_SELECTION_TIMEOUT_MS", 200):

        db = await init_mongo()
        assert db is None
        assert get_mongo_db() is None

        # Clean up
        await close_mongo()


@pytest.mark.anyio
async def test_init_and_close_db_lifecycle():
    """Verify init_db and close_db lifecycle completes without error."""
    statuses = await init_db()
    assert isinstance(statuses, dict)
    assert "redis_connected" in statuses
    assert "mongo_connected" in statuses

    # Closing should run cleanly
    await close_db()
    assert get_redis_client() is None
    assert get_mongo_db() is None
