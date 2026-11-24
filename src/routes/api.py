"""
Sample API routes demonstrating standard and strict rate limits.
"""

from typing import List, Optional
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api", tags=["api"])


class DataItem(BaseModel):
    id: int
    name: str
    value: float


class DataResponse(BaseModel):
    status: str
    message: str
    items: List[DataItem]


class ExpensiveRequest(BaseModel):
    operation: Optional[str] = "heavy_report"
    records: Optional[int] = 500


class ExpensiveResponse(BaseModel):
    status: str
    message: str
    operation: str
    processed_records: int


@router.get("/data", response_model=DataResponse)
async def get_data() -> DataResponse:
    """
    Standard protected endpoint with default rate limit.
    Configured via settings.RATE_LIMIT_DATA_LIMIT / settings.RATE_LIMIT_DATA_WINDOW.
    """
    return DataResponse(
        status="success",
        message="Standard protected data retrieved successfully",
        items=[
            DataItem(id=1, name="Transaction Volume", value=1420.5),
            DataItem(id=2, name="Latency P99", value=12.4),
            DataItem(id=3, name="Cache Hit Ratio", value=0.985),
        ],
    )


@router.post("/expensive", response_model=ExpensiveResponse)
async def post_expensive(payload: ExpensiveRequest = ExpensiveRequest()) -> ExpensiveResponse:
    """
    Stricter protected endpoint with lower rate limit to protect heavy compute/DB operations.
    Configured via settings.RATE_LIMIT_EXPENSIVE_LIMIT / settings.RATE_LIMIT_EXPENSIVE_WINDOW.
    """
    return ExpensiveResponse(
        status="success",
        message="Expensive computation executed successfully",
        operation=payload.operation or "heavy_report",
        processed_records=payload.records or 500,
    )
