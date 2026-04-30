from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.health_service import HealthService
from app.services.runtime_state_service import RuntimeStateService
from app.services.system_metrics_service import SystemMetricsService


router = APIRouter(tags=["health"])


@router.get("/live")
def liveness() -> dict:
    return {
        "status": "ok",
        "checks": {
            "process": "up",
        },
        "active_requests": RuntimeStateService.current_active_requests(),
    }


@router.get("/ready")
def readiness(response: Response, db: Session = Depends(get_db)) -> dict:
    metrics = SystemMetricsService.collect(db, window_minutes=5, refresh_alerts=True)
    if metrics["status"] != "ready":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": metrics["status"],
        "checks": {
            "database": metrics["database"]["status"],
            "redis": metrics["redis"]["status"],
            "settings": "ok",
            "background": "ok" if (metrics["background"].get("pending_finalize_logs") or 0) < SystemMetricsService.BACKGROUND_BACKLOG_WARNING_THRESHOLD else "backlog",
        },
        "active_requests": RuntimeStateService.current_active_requests(),
        "redis_active_requests": metrics["redis"].get("active_requests"),
        "redis_active_streams": metrics["redis"].get("active_streams"),
        "redis_error": metrics["redis"].get("error"),
        "database_error": metrics["database"].get("error"),
        "database_pool": metrics["database"].get("pool"),
        "background": metrics["background"],
        "alerts": metrics["alerts"],
    }


@router.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    stats = HealthService.cached_provider_status_summary(db)
    return {
        "status": "ok" if stats["unhealthy_provider_count"] == 0 else "degraded",
        "provider_summary": stats,
        "active_requests": RuntimeStateService.current_active_requests(),
        "peak_active_requests": RuntimeStateService.peak_active_requests(),
    }


@router.get("/metrics")
def public_metrics(db: Session = Depends(get_db)) -> dict:
    return SystemMetricsService.collect(db, window_minutes=5, refresh_alerts=False)
