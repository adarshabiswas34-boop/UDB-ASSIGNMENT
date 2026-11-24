from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Server settings
    PORT: int = 8000
    HOST: str = "0.0.0.0"
    ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "info"

    # Rate Limiter Settings
    # fail-open (False): Allow requests when database is unreachable
    # fail-closed (True): Deny requests when database is unreachable
    RATE_LIMIT_FAIL_CLOSED: bool = False
    RATE_LIMIT_DEFAULT_LIMIT: int = 100
    RATE_LIMIT_DEFAULT_WINDOW: int = 60
    RATE_LIMIT_KEY_PREFIX: str = "ratelimit"

    # Route-Specific Rate Limits
    RATE_LIMIT_DATA_LIMIT: int = 60
    RATE_LIMIT_DATA_WINDOW: int = 60
    RATE_LIMIT_EXPENSIVE_LIMIT: int = 5
    RATE_LIMIT_EXPENSIVE_WINDOW: int = 60

    # Redis settings (Primary in-memory store for rate limiting)
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: Optional[str] = None
    REDIS_DB: int = 0
    REDIS_SOCKET_TIMEOUT: float = 2.0
    REDIS_CONNECT_TIMEOUT: float = 2.0
    REDIS_MAX_CONNECTIONS: int = 20

    # MongoDB settings (Alternative document store for rate limiting)
    MONGODB_URL: str = "mongodb://localhost:27017"
    MONGODB_DB_NAME: str = "rate_limiter_db"
    MONGODB_SERVER_SELECTION_TIMEOUT_MS: int = 2000

    # General / SQL Database settings
    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_NAME: str = "rate_limiter_db"
    DB_USER: str = "postgres"
    DB_PASSWORD: str = "postgres"
    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/rate_limiter_db"


settings = Settings()
