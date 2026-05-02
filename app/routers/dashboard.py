from datetime import datetime, timedelta

from sqlalchemy import case, func, literal, select
from sqlalchemy.orm import Session

from fastapi import APIRouter, Depends

from app.database import get_db
from app.models.model_catalog import ModelCatalog
from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.models.request_log import RequestLog
from app.services.api_key_admin_service import ApiKeyAdminService
from app.services.cache_service import CacheService
from app.services.log_service import LogService
from app.services.setting_service import SettingService


router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])
_DASHBOARD_USAGE_CACHE_TTL_SECONDS = 30
_DASHBOARD_USAGE_TOP_LIMIT = 12


def _int_value(value) -> int:
    return int(value or 0)


def _float_value(value) -> float:
    return round(float(value or 0), 6)


def _dashboard_usage_row_to_dict(row, *, include_provider: bool, include_model: bool) -> dict:
    item = {
        "total_requests": _int_value(row.total_requests),
        "failed_requests": _int_value(row.failed_requests),
        "prompt_tokens": _int_value(row.prompt_tokens),
        "completion_tokens": _int_value(row.completion_tokens),
        "total_tokens": _int_value(row.total_tokens),
        "total_cost": _float_value(row.total_cost),
    }
    if include_provider:
        item.update(
            {
                "provider_id": row.provider_id,
                "provider_name": row.provider_name or "未命名中转站",
            }
        )
    if include_model:
        item["model_name"] = row.model_name or "未指定模型"
    return item


def _dashboard_usage_overview(db: Session) -> dict:
    cached = CacheService.get("dashboard-usage-overview")
    if cached is not None:
        return cached

    route_expr = LogService._route_traffic_expr()
    model_expr = func.coalesce(RequestLog.requested_model, RequestLog.model_name, literal("未指定模型"))
    total_stmt = select(
        func.count(RequestLog.id).label("total_requests"),
        func.sum(case((RequestLog.success.is_(False), 1), else_=0)).label("failed_requests"),
        func.sum(RequestLog.prompt_tokens).label("prompt_tokens"),
        func.sum(RequestLog.completion_tokens).label("completion_tokens"),
        func.sum(RequestLog.total_tokens).label("total_tokens"),
        func.sum(RequestLog.total_cost).label("total_cost"),
    ).where(route_expr)
    total_row = db.execute(total_stmt).one()

    base_metric_columns = [
        func.count(RequestLog.id).label("total_requests"),
        func.sum(case((RequestLog.success.is_(False), 1), else_=0)).label("failed_requests"),
        func.sum(RequestLog.prompt_tokens).label("prompt_tokens"),
        func.sum(RequestLog.completion_tokens).label("completion_tokens"),
        func.sum(RequestLog.total_tokens).label("total_tokens"),
        func.sum(RequestLog.total_cost).label("total_cost"),
    ]
    top_models = [
        _dashboard_usage_row_to_dict(row, include_provider=False, include_model=True)
        for row in db.execute(
            select(model_expr.label("model_name"), *base_metric_columns)
            .where(route_expr)
            .group_by(model_expr)
            .order_by(func.sum(RequestLog.total_tokens).desc(), func.count(RequestLog.id).desc())
            .limit(_DASHBOARD_USAGE_TOP_LIMIT)
        )
    ]
    top_providers = [
        _dashboard_usage_row_to_dict(row, include_provider=True, include_model=False)
        for row in db.execute(
            select(RequestLog.provider_id, RequestLog.provider_name, *base_metric_columns)
            .where(route_expr)
            .group_by(RequestLog.provider_id, RequestLog.provider_name)
            .order_by(func.sum(RequestLog.total_cost).desc(), func.count(RequestLog.id).desc())
            .limit(_DASHBOARD_USAGE_TOP_LIMIT)
        )
    ]
    top_provider_models = [
        _dashboard_usage_row_to_dict(row, include_provider=True, include_model=True)
        for row in db.execute(
            select(RequestLog.provider_id, RequestLog.provider_name, model_expr.label("model_name"), *base_metric_columns)
            .where(route_expr)
            .group_by(RequestLog.provider_id, RequestLog.provider_name, model_expr)
            .order_by(func.sum(RequestLog.total_cost).desc(), func.sum(RequestLog.total_tokens).desc())
            .limit(_DASHBOARD_USAGE_TOP_LIMIT)
        )
    ]
    payload = {
        "summary": _dashboard_usage_row_to_dict(total_row, include_provider=False, include_model=False),
        "top_models": top_models,
        "top_providers": top_providers,
        "top_provider_models": top_provider_models,
        "top_limit": _DASHBOARD_USAGE_TOP_LIMIT,
        "cache_ttl_seconds": _DASHBOARD_USAGE_CACHE_TTL_SECONDS,
    }
    return CacheService.set("dashboard-usage-overview", payload, ttl_seconds=_DASHBOARD_USAGE_CACHE_TTL_SECONDS)


@router.get("")
def get_dashboard(db: Session = Depends(get_db)) -> dict:
    settings = SettingService.get_or_create(db)
    api_key_summary = ApiKeyAdminService.get_summary(db)
    default_provider = db.get(Provider, settings.default_provider_id) if settings.default_provider_id else None
    usage_overview = _dashboard_usage_overview(db)
    usage_summary = usage_overview["summary"]
    recent_since = datetime.utcnow() - timedelta(hours=24)
    recent_requests = db.scalar(
        select(func.count()).select_from(RequestLog).where(RequestLog.created_at >= recent_since, LogService._route_traffic_expr())
    ) or 0
    recent_failures = db.scalar(
        select(func.count()).select_from(RequestLog).where(RequestLog.created_at >= recent_since, LogService._route_traffic_expr(), RequestLog.success.is_(False))
    ) or 0
    recent_failure_rate = round((recent_failures / recent_requests) * 100, 2) if recent_requests else 0.0
    recent_tokens = db.scalar(
        select(func.sum(RequestLog.total_tokens)).where(RequestLog.created_at >= recent_since, LogService._route_traffic_expr())
    ) or 0
    conversation_count = db.scalar(
        select(func.count(func.distinct(RequestLog.conversation_key))).where(RequestLog.conversation_key.is_not(None), LogService._route_traffic_expr())
    ) or 0

    return {
        "provider_count": db.scalar(select(func.count()).select_from(Provider)) or 0,
        "healthy_count": db.scalar(select(func.count()).select_from(Provider).where(Provider.health_status == "healthy")) or 0,
        "degraded_count": db.scalar(select(func.count()).select_from(Provider).where(Provider.health_status == "degraded")) or 0,
        "unhealthy_count": db.scalar(select(func.count()).select_from(Provider).where(Provider.health_status == "unhealthy")) or 0,
        "model_count": db.scalar(select(func.count()).select_from(ModelCatalog)) or 0,
        "healthy_model_count": db.scalar(select(func.count()).select_from(ProviderModel).where(ProviderModel.health_status == "healthy")) or 0,
        "degraded_model_count": db.scalar(select(func.count()).select_from(ProviderModel).where(ProviderModel.health_status == "degraded")) or 0,
        "unhealthy_model_count": db.scalar(select(func.count()).select_from(ProviderModel).where(ProviderModel.health_status == "unhealthy")) or 0,
        "total_requests": usage_summary["total_requests"],
        "total_tokens": usage_summary["total_tokens"],
        "total_cost": usage_summary["total_cost"],
        "total_failures": usage_summary["failed_requests"],
        "recent_requests": recent_requests,
        "recent_tokens": int(recent_tokens or 0),
        "conversation_count": int(conversation_count or 0),
        "recent_failure_rate": recent_failure_rate,
        "usage_overview": usage_overview,
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
