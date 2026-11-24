from fastapi import APIRouter
from src.api.routes.health import router as health_router
from src.api.routes.example import router as example_router

api_router = APIRouter()
api_router.include_router(health_router, tags=["health"])
api_router.include_router(example_router, tags=["resource"])

__all__ = ["api_router"]
