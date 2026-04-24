from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fastapi import APIRouter, Depends

from app.database import get_db
from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.models.request_log import RequestLog
from app.services.api_key_admin_service import ApiKeyAdminService
from app.services.setting_service import SettingService


router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("")
def get_dashboard(db: Session = Depends(get_db)) -> dict:
    settings = SettingService.get_or_create(db)
    api_key_summary = ApiKeyAdminService.get_summary(db)
    default_provider = db.get(Provider, settings.default_provider_id) if settings.default_provider_id else None
    recent_since = datetime.utcnow() - timedelta(hours=24)
    recent_requests = db.scalar(
        select(func.count()).select_from(RequestLog).where(RequestLog.created_at >= recent_since)
    ) or 0
    recent_failures = db.scalar(
        select(func.count()).select_from(RequestLog).where(RequestLog.created_at >= recent_since, RequestLog.success.is_(False))
    ) or 0
    recent_failure_rate = round((recent_failures / recent_requests) * 100, 2) if recent_requests else 0.0
    recent_tokens = db.scalar(
        select(func.sum(RequestLog.total_tokens)).where(RequestLog.created_at >= recent_since)
    ) or 0
    conversation_count = db.scalar(
        select(func.count(func.distinct(RequestLog.conversation_key))).where(RequestLog.conversation_key.is_not(None))
    ) or 0

    return {
        "provider_count": db.scalar(select(func.count()).select_from(Provider)) or 0,
        "healthy_count": db.scalar(select(func.count()).select_from(Provider).where(Provider.health_status == "healthy")) or 0,
        "degraded_count": db.scalar(select(func.count()).select_from(Provider).where(Provider.health_status == "degraded")) or 0,
        "unhealthy_count": db.scalar(select(func.count()).select_from(Provider).where(Provider.health_status == "unhealthy")) or 0,
        "model_count": db.scalar(select(func.count()).select_from(ProviderModel)) or 0,
        "healthy_model_count": db.scalar(select(func.count()).select_from(ProviderModel).where(ProviderModel.health_status == "healthy")) or 0,
        "degraded_model_count": db.scalar(select(func.count()).select_from(ProviderModel).where(ProviderModel.health_status == "degraded")) or 0,
        "unhealthy_model_count": db.scalar(select(func.count()).select_from(ProviderModel).where(ProviderModel.health_status == "unhealthy")) or 0,
        "total_requests": db.scalar(select(func.count()).select_from(RequestLog)) or 0,
        "total_failures": db.scalar(select(func.count()).select_from(RequestLog).where(RequestLog.success.is_(False))) or 0,
        "recent_requests": recent_requests,
        "recent_tokens": int(recent_tokens or 0),
        "conversation_count": int(conversation_count or 0),
        "recent_failure_rate": recent_failure_rate,
        "default_provider": default_provider.name if default_provider else None,
        "route_mode": settings.route_mode,
        "api_key_total": api_key_summary.total_keys,
        "api_key_enabled": api_key_summary.enabled_keys,
        "api_key_disabled": api_key_summary.disabled_keys,
        "api_key_expired": api_key_summary.expired_keys,
        "api_key_quota_exhausted": api_key_summary.quota_exhausted_keys,
        "api_key_total_requests": api_key_summary.total_requests,
        "api_key_total_prompt_tokens": api_key_summary.total_prompt_tokens,
        "api_key_total_completion_tokens": api_key_summary.total_completion_tokens,
        "api_key_total_tokens": api_key_summary.total_tokens,
        "logging": {
            "enable_token_logging": settings.enable_token_logging,
            "enable_payload_logging": settings.enable_payload_logging,
            "enable_stream_response_persist": settings.enable_stream_response_persist,
            "max_logged_body_bytes": settings.max_logged_body_bytes,
        },
    }
