from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.log import MetricItem, MetricListResponse
from app.schemas.log import MetricPeriodItem, MetricPeriodResponse
from app.schemas.log import MetricTimeSeriesItem, MetricTimeSeriesResponse
from app.schemas.setting import SettingOut, SettingUpdate
from app.services.admin_audit_service import AdminAuditService
from app.services.log_service import LogService
from app.services.setting_service import SettingService
from app.services.system_metrics_service import SystemMetricsService
from app.services.user_auth_service import require_admin_api_user
from app.tasks import configure_scheduler


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
    window_minutes: int = Query(default=180, ge=5, le=43200),
    bucket_minutes: int = Query(default=15, ge=1, le=1440),
    db: Session = Depends(get_db),
) -> MetricTimeSeriesResponse:
    items = [
        MetricTimeSeriesItem.model_validate(item)
        for item in LogService.metric_timeseries(db, window_minutes=window_minutes, bucket_minutes=bucket_minutes)
    ]
    return MetricTimeSeriesResponse(window_minutes=window_minutes, bucket_minutes=bucket_minutes, items=items)


@router.get("/period", response_model=MetricPeriodResponse)
def metrics_period_report(
    period_type: str = Query(default="day", pattern="^(day|week|month)$"),
    window_days: int = Query(default=30, ge=1, le=365),
    db: Session = Depends(get_db),
) -> MetricPeriodResponse:
    items = [
        MetricPeriodItem.model_validate(item)
        for item in LogService.metric_period_report(db, window_days=window_days, period_type=period_type)
    ]
    return MetricPeriodResponse(period_type=period_type, window_days=window_days, items=items)


@router.get("/system")
def system_metrics(
    window_minutes: int = Query(default=5, ge=1, le=1440),
    refresh_alerts: bool = Query(default=False),
    network_bandwidth_mbps: float | None = Query(default=None, ge=0, le=100000),
    db: Session = Depends(get_db),
) -> dict:
    return SystemMetricsService.collect(
        db,
        window_minutes=window_minutes,
        refresh_alerts=refresh_alerts,
        network_bandwidth_mbps=network_bandwidth_mbps,
    )


@router.post("/system/configuration-profiles/{profile_id}/apply", response_model=SettingOut)
def apply_system_configuration_profile(
    profile_id: str,
    network_bandwidth_mbps: float | None = Query(default=None, ge=0, le=100000),
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> SettingOut:
    metrics = SystemMetricsService.collect(
        db,
        window_minutes=5,
        refresh_alerts=False,
        network_bandwidth_mbps=network_bandwidth_mbps,
    )
    recommendations = metrics.get("configuration_recommendations") or {}
    if not recommendations.get("available"):
        raise HTTPException(status_code=400, detail=recommendations.get("reason") or "当前宿主信息不足，无法套用推荐配置")
    profiles = recommendations.get("profiles") or []
    profile = next((item for item in profiles if item.get("id") == profile_id), None)
    if profile is None:
        raise HTTPException(status_code=404, detail="配置方案不存在")
    settings_patch = {
        key: value
        for key, value in (profile.get("settings_patch") or {}).items()
        if key in SettingUpdate.model_fields
    }
    if not settings_patch:
        raise HTTPException(status_code=400, detail="该配置方案没有可在线套用的系统设置")

    setting = SettingService.get_or_create(db)
    before = {field: getattr(setting, field, None) for field in settings_patch}
    payload_data = {
        field: getattr(setting, field)
        for field in SettingUpdate.model_fields
    }
    payload_data.update(settings_patch)
    try:
        updated = SettingService.update(db, SettingUpdate(**payload_data))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    configure_scheduler()
    after = {field: getattr(updated, field, None) for field in settings_patch}
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="apply",
        entity_type="setting",
        entity_id=updated.id,
        entity_name="app_settings",
        summary=f"套用宿主推荐配置：{profile.get('name') or profile_id}",
        detail={
            "profile_id": profile_id,
            "profile_name": profile.get("name"),
            "host_class": recommendations.get("host_class"),
            "estimated_capacity": profile.get("estimated_capacity"),
            "settings_patch": settings_patch,
            "env_suggestions": profile.get("env_suggestions"),
        },
        before=before,
        after=after,
        changed_fields=settings_patch,
    )
    return SettingOut.model_validate(updated)
