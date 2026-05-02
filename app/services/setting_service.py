from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.app_setting import AppSetting
from app.models.provider import Provider
from app.schemas.setting import SettingUpdate


_settings = get_settings()

DEFAULT_SETTING = {
    "id": 1,
    "route_mode": "failover",
    "default_provider_id": None,
    "manual_allow_fallback": True,
    "global_timeout_ms": 30000,
    "global_max_retries": 2,
    "global_max_request_tokens": 0,
    "circuit_breaker_threshold": 3,
    "auto_health_check": True,
    "health_check_interval_sec": 60,
    "recovery_probe_interval_sec": 30,
    "enable_token_logging": True,
    "enable_payload_logging": False,
    "enable_stream_response_persist": False,
    "mask_sensitive_fields": True,
    "max_logged_body_bytes": 16384,
    "allow_public_user_registration": False,
    "request_log_retention_days": 90,
    "admin_audit_log_retention_days": 180,
    "route_candidate_cache_ttl_sec": 10,
    "model_list_cache_ttl_sec": 15,
    "provider_status_cache_ttl_sec": 10,
    "async_request_logging": True,
    "global_max_active_requests": _settings.global_max_active_requests,
    "global_max_active_streams": _settings.global_max_active_streams,
    "api_key_max_active_requests": _settings.api_key_max_active_requests,
    "api_key_max_active_streams": _settings.api_key_max_active_streams,
    "account_max_active_requests": _settings.account_max_active_requests,
    "account_max_active_streams": _settings.account_max_active_streams,
    "provider_max_active_requests": _settings.provider_max_active_requests,
    "provider_max_active_streams": _settings.provider_max_active_streams,
    "concurrency_lease_ttl_seconds": _settings.concurrency_lease_ttl_seconds,
    "stream_connect_timeout_seconds": _settings.stream_connect_timeout_seconds,
    "stream_first_token_timeout_seconds": _settings.stream_first_token_timeout_seconds,
    "stream_idle_timeout_seconds": _settings.stream_idle_timeout_seconds,
    "stream_max_duration_seconds": _settings.stream_max_duration_seconds,
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
        SettingService._validate_stream_timeout_configuration(
            first_token_timeout_seconds=payload.stream_first_token_timeout_seconds,
            idle_timeout_seconds=payload.stream_idle_timeout_seconds,
            max_duration_seconds=payload.stream_max_duration_seconds,
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

    @staticmethod
    def _validate_stream_timeout_configuration(
        *,
        first_token_timeout_seconds: int,
        idle_timeout_seconds: int,
        max_duration_seconds: int,
    ) -> None:
        if max_duration_seconds <= 0:
            return
        positive_timeouts = [
            value
            for value in (first_token_timeout_seconds, idle_timeout_seconds)
            if value > 0
        ]
        if positive_timeouts and max_duration_seconds < max(positive_timeouts):
            raise ValueError("stream_max_duration_seconds must be >= enabled stream chunk timeouts")
