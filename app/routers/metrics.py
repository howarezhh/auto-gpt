from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.log import MetricItem, MetricListResponse
from app.schemas.log import MetricTimeSeriesItem, MetricTimeSeriesResponse
from app.services.log_service import LogService


router = APIRouter(prefix="/api/metrics", tags=["metrics"])


@router.get("/summary", response_model=MetricListResponse)
def metrics_summary(
    window_minutes: int = Query(default=60, ge=1, le=1440),
    db: Session = Depends(get_db),
) -> MetricListResponse:
    items = [MetricItem.model_validate(item) for item in LogService.metric_summary(db, window_minutes=window_minutes)]
    return MetricListResponse(window_minutes=window_minutes, items=items)


@router.get("/timeseries", response_model=MetricTimeSeriesResponse)
def metrics_timeseries(
    window_minutes: int = Query(default=180, ge=5, le=1440),
    bucket_minutes: int = Query(default=15, ge=1, le=240),
    db: Session = Depends(get_db),
) -> MetricTimeSeriesResponse:
    items = [
        MetricTimeSeriesItem.model_validate(item)
        for item in LogService.metric_timeseries(db, window_minutes=window_minutes, bucket_minutes=bucket_minutes)
    ]
    return MetricTimeSeriesResponse(window_minutes=window_minutes, bucket_minutes=bucket_minutes, items=items)
