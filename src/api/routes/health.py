from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter
from pydantic import BaseModel
from config.settings import settings
from src.db.redis import get_redis_client
from src.db.mongo import get_mongo_db

router = APIRouter()


class DatabaseHealth(BaseModel):
    redis: bool
    mongodb: bool


class HealthResponse(BaseModel):
    status: str
    environment: str
    timestamp: str
    database: Optional[DatabaseHealth] = None


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Basic health check endpoint reporting service and database status."""
    return HealthResponse(
        status="ok",
        environment=settings.ENVIRONMENT,
        timestamp=datetime.now(timezone.utc).isoformat(),
        database=DatabaseHealth(
            redis=get_redis_client() is not None,
            mongodb=get_mongo_db() is not None,
        ),
    )
