from contextlib import asynccontextmanager
from fastapi import FastAPI
from config.settings import settings
from src.api.routes import api_router as health_api_router
from src.routes.api import router as sample_api_router
from src.db import init_db, close_db
from src.middleware.rate_limit import RateLimitMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize database connections gracefully
    await init_db()
    yield
    # Shutdown: Cleanly close connection pools
    await close_db()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Rate Limiter Service API",
        version="0.1.0",
        description="Backend service with rate limiting capabilities",
        lifespan=lifespan,
    )

    # Add Rate Limiting Middleware with route-specific limits
    app.add_middleware(
        RateLimitMiddleware,
        default_limit=settings.RATE_LIMIT_DEFAULT_LIMIT,
        default_window=settings.RATE_LIMIT_DEFAULT_WINDOW,
        route_limits={
            "GET:/api/data": (settings.RATE_LIMIT_DATA_LIMIT, settings.RATE_LIMIT_DATA_WINDOW),
            "POST:/api/expensive": (settings.RATE_LIMIT_EXPENSIVE_LIMIT, settings.RATE_LIMIT_EXPENSIVE_WINDOW),
        },
    )

    # Include routes
    app.include_router(health_api_router)
    app.include_router(sample_api_router)

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=True,
    )
