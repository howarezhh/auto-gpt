from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.log import MetricItem, MetricListResponse
from app.services.log_service import LogService


router = APIRouter(prefix="/api/metrics", tags=["metrics"])


@router.get("/summary", response_model=MetricListResponse)
def metrics_summary(
    window_minutes: int = Query(default=60, ge=1, le=1440),
    db: Session = Depends(get_db),
) -> MetricListResponse:
    items = [MetricItem.model_validate(item) for item in LogService.metric_summary(db, window_minutes=window_minutes)]
    return MetricListResponse(window_minutes=window_minutes, items=items)
