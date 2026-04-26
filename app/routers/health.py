from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.health_service import HealthService
from app.services.runtime_state_service import RuntimeStateService
from app.services.setting_service import SettingService
from app.services.upstream_client import UpstreamClientService


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
def readiness(db: Session = Depends(get_db)) -> dict:
    setting = SettingService.get_or_create(db)
    client = UpstreamClientService.get_client()
    return {
        "status": "ready",
        "checks": {
            "database": "ok",
            "settings": "ok",
            "upstream_client": "ok" if client is not None else "missing",
        },
        "active_requests": RuntimeStateService.current_active_requests(),
        "async_request_logging": setting.async_request_logging,
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
