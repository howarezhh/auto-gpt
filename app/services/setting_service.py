from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.app_setting import AppSetting
from app.models.provider import Provider
from app.schemas.setting import SettingUpdate


DEFAULT_SETTING = {
    "id": 1,
    "route_mode": "failover",
    "default_provider_id": None,
    "manual_allow_fallback": True,
    "global_timeout_ms": 30000,
    "global_max_retries": 2,
    "circuit_breaker_threshold": 3,
    "auto_health_check": True,
    "health_check_interval_sec": 60,
    "recovery_probe_interval_sec": 30,
    "enable_token_logging": True,
    "enable_payload_logging": True,
    "enable_stream_response_persist": True,
    "mask_sensitive_fields": True,
    "max_logged_body_bytes": 16384,
    "allow_public_user_registration": False,
    "request_log_retention_days": 90,
    "admin_audit_log_retention_days": 180,
    "route_candidate_cache_ttl_sec": 10,
    "model_list_cache_ttl_sec": 15,
    "provider_status_cache_ttl_sec": 10,
    "async_request_logging": True,
}


class SettingService:
    @staticmethod
    def get_or_create(db: Session) -> AppSetting:
        setting = db.get(AppSetting, 1)
        if setting:
            return setting
        setting = AppSetting(**DEFAULT_SETTING)
        db.add(setting)
        db.commit()
        db.refresh(setting)
        return setting

    @staticmethod
    def update(db: Session, payload: SettingUpdate) -> AppSetting:
        setting = SettingService.get_or_create(db)
        SettingService._validate_route_configuration(
            db,
            route_mode=payload.route_mode,
            default_provider_id=payload.default_provider_id,
        )
        SettingService._validate_retention_configuration(
            request_log_retention_days=payload.request_log_retention_days,
            admin_audit_log_retention_days=payload.admin_audit_log_retention_days,
        )
        for field, value in payload.model_dump().items():
            setattr(setting, field, value)
        db.commit()
        db.refresh(setting)
        return setting

    @staticmethod
    def _validate_route_configuration(
        db: Session,
        *,
        route_mode: str,
        default_provider_id: int | None,
    ) -> None:
        if route_mode == "manual" and default_provider_id is None:
            raise ValueError("manual route_mode requires default_provider_id")
        if default_provider_id is None:
            return
        provider_exists = db.scalar(
            select(Provider.id).where(Provider.id == default_provider_id)
        )
        if provider_exists is None:
            raise ValueError("default_provider_id does not exist")

    @staticmethod
    def _validate_retention_configuration(
        *,
        request_log_retention_days: int,
        admin_audit_log_retention_days: int,
    ) -> None:
        if request_log_retention_days < 0:
            raise ValueError("request_log_retention_days must be >= 0")
        if admin_audit_log_retention_days < 0:
            raise ValueError("admin_audit_log_retention_days must be >= 0")
