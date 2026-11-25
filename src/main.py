from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from config.settings import settings
from src.api.routes import api_router as health_api_router
from src.routes.api import router as sample_api_router
from src.db import init_db, close_db
from src.middleware.rate_limit import RateLimitMiddleware

# Resolve static directory relative to this file
_STATIC_DIR = Path(__file__).parent / "static"


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

    # Mount static files (CSS/JS/HTML assets)
    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # Include routes
    app.include_router(health_api_router)
    app.include_router(sample_api_router)

    @app.api_route("/", methods=["GET", "HEAD"], tags=["root"], include_in_schema=True)
    async def root(request: Request):
        """Landing page — returns HTML UI for browsers, supports HEAD for health probes."""
        html_file = _STATIC_DIR / "index.html"
        if html_file.exists():
            return HTMLResponse(content=html_file.read_text(encoding="utf-8"))
        # Fallback to JSON if static file is missing
        return {
            "name": "Rate Limiter Service API",
            "status": "online",
            "version": "0.1.0",
            "documentation": "/docs",
            "endpoints": {
                "health_check": "/health",
                "standard_rate_limited_data": "/api/data",
                "strict_rate_limited_expensive": "/api/expensive",
                "swagger_ui": "/docs",
                "redoc": "/redoc",
            },
        }

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
