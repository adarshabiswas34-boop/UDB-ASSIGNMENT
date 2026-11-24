from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1")


class ResourceResponse(BaseModel):
    message: str
    status: str


@router.get("/resource", response_model=ResourceResponse)
async def get_protected_resource() -> ResourceResponse:
    """Protected resource endpoint to demonstrate rate limiting."""
    return ResourceResponse(
        message="Access granted to protected resource",
        status="success",
    )
