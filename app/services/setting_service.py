import json

from sqlalchemy.orm import Session
from types import SimpleNamespace

from app.config import get_settings
from app.database import SessionLocal
from app.models.app_setting import AppSetting
from app.schemas.setting import SettingUpdate
from app.services.cache_service import CacheService


_settings = get_settings()

DEFAULT_SETTING = {
    "id": 1,
    "route_exhausted_retry_max_wait_seconds": 600,
    "route_exhausted_retry_infinite_enabled": False,
    "trusted_providers_only": False,
    "max_candidate_count": 10,
    "route_candidate_expand_count": 5,
    "global_max_request_tokens": 0,
    "max_v1_request_body_bytes": 20971520,
    "max_v1_chat_request_body_bytes": 0,
    "max_v1_responses_request_body_bytes": 0,
    "long_output_stream_threshold_tokens": 8192,
    "max_non_stream_response_body_bytes": 20971520,
    "stream_token_capture_max_bytes": 1048576,
    "max_logged_metadata_bytes": 1024,
    "content_guard_enabled": True,
    "content_guard_precheck_auto_enabled": False,
    "content_guard_block_on_high_risk": True,
    "content_guard_probe_interval_sec": 3600,
    "content_guard_json_probe_enabled": False,
    "content_guard_probe_protocol_type": "chat_completions",
    "content_guard_max_scan_bytes": 16384,
    "content_guard_stream_buffer_max_bytes": 16384,
    "content_guard_low_trust_requires_buffer": True,
    "content_guard_rules_json": "",
    "content_guard_high_risk_strategy": "switch_provider",
    "content_guard_max_detection_delay_ms": 300,
    "content_guard_stream_mode": "buffer_300ms",
    "content_guard_url_check_enabled": True,
    "content_guard_url_allowlist_json": "",
    "content_guard_async_review_enabled": True,
    "content_guard_high_risk_confidence_threshold": 85,
    "content_guard_enhanced_detection_enabled": True,
    "content_guard_enhanced_illegal_enabled": True,
    "content_guard_enhanced_ad_enabled": True,
    "content_guard_enhanced_custom_enabled": True,
    "content_guard_enhanced_obfuscation_enabled": True,
    "content_guard_enhanced_threshold": 70,
    "content_guard_enhanced_context_window_chars": 96,
    "circuit_breaker_threshold": 3,
    "auto_health_check": False,
    "health_check_interval_sec": 300,
    "recovery_probe_interval_sec": 30,
    "probe_rate_limit_per_minute": 4,
    "probe_type_rate_limit_per_minute": 4,
    "enable_token_logging": True,
    "enable_payload_logging": False,
    "enable_stream_response_persist": False,
    "mask_sensitive_fields": True,
    "max_logged_body_bytes": 16384,
    "allow_public_user_registration": False,
    "request_log_retention_days": 90,
    "admin_audit_log_retention_days": 180,
    "request_child_log_retention_days": 90,
    "exception_log_retention_days": 180,
    "health_log_retention_days": 7,
    "billing_log_retention_days": 365,
    "background_job_log_retention_days": 90,
    "user_operation_log_retention_days": 180,
    "asset_log_retention_days": 180,
    "alert_event_retention_days": 180,
    "route_candidate_cache_ttl_sec": 10,
    "model_list_cache_ttl_sec": 15,
    "provider_status_cache_ttl_sec": 10,
    "async_request_logging": True,
    "global_qps_limit": _settings.global_qps_limit,
    "global_rpm_limit": _settings.global_rpm_limit,
    "account_qps_limit": _settings.account_qps_limit,
    "account_rpm_limit": _settings.account_rpm_limit,
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
    "responses_chat_adapter_enabled": _settings.responses_chat_adapter_enabled,
    "responses_chat_adapter_storage_type": _settings.responses_chat_adapter_storage_type,
    "responses_chat_adapter_ttl_seconds": _settings.responses_chat_adapter_ttl_seconds,
    "responses_chat_adapter_model_map_json": _settings.responses_chat_adapter_model_map_json,
    "responses_chat_adapter_max_tool_rounds": _settings.responses_chat_adapter_max_tool_rounds,
    "responses_chat_adapter_web_search_enabled": _settings.responses_chat_adapter_web_search_enabled,
    "responses_chat_adapter_search_proxy_url": _settings.responses_chat_adapter_search_proxy_url,
    "responses_chat_adapter_upstream_base_url": _settings.responses_chat_adapter_upstream_base_url,
    "responses_chat_adapter_upstream_api_key": _settings.responses_chat_adapter_upstream_api_key,
    "responses_chat_adapter_upstreams_json": _settings.responses_chat_adapter_upstreams_json,
    "responses_chat_adapter_context_window_tokens": _settings.responses_chat_adapter_context_window_tokens,
    "responses_chat_adapter_snapshot_max_bytes": _settings.responses_chat_adapter_snapshot_max_bytes,
    "responses_chat_adapter_db_cleanup_interval_seconds": _settings.responses_chat_adapter_db_cleanup_interval_seconds,
}


class SettingService:
    """负责读取、初始化和校验全局应用设置。"""

    RUNTIME_CACHE_KEY = "runtime-settings:app"
    RUNTIME_CACHE_TTL_SECONDS = 5

    @staticmethod
    def get_or_create(db: Session) -> AppSetting:
        """读取系统设置，不存在时按默认值初始化。"""
        setting = db.get(AppSetting, 1)
        if setting:
            # 兼容旧数据，把过低的健康检查间隔自动拉回最小安全值。
            changed = False
            if setting.health_check_interval_sec < 300:
                setting.health_check_interval_sec = 300
                changed = True
            normalized_guard_delay_ms = min(500, max(0, int(setting.content_guard_max_detection_delay_ms or 300)))
            if setting.content_guard_max_detection_delay_ms != normalized_guard_delay_ms:
                setting.content_guard_max_detection_delay_ms = normalized_guard_delay_ms
                changed = True
            if changed:
                db.commit()
                db.refresh(setting)
                SettingService.invalidate_runtime_cache()
            return setting
        setting = AppSetting(**DEFAULT_SETTING)
        db.add(setting)
        db.commit()
        db.refresh(setting)
        SettingService.invalidate_runtime_cache()
        return setting

    @staticmethod
    def get_cached() -> SimpleNamespace:
        """读取请求热路径使用的设置快照，避免每个请求重复打开数据库会话。"""
        cached = CacheService.get(SettingService.RUNTIME_CACHE_KEY)
        if isinstance(cached, dict):
            return SimpleNamespace(**cached)
        db = SessionLocal()
        try:
            setting = SettingService.get_or_create(db)
            payload = SettingService._to_runtime_payload(setting)
        finally:
            db.close()
        CacheService.set(
            SettingService.RUNTIME_CACHE_KEY,
            payload,
            ttl_seconds=SettingService.RUNTIME_CACHE_TTL_SECONDS,
        )
        return SimpleNamespace(**payload)

    @staticmethod
    def update(db: Session, payload: SettingUpdate) -> AppSetting:
        """更新系统设置，并在落库前执行关键约束校验。"""
        setting = SettingService.get_or_create(db)
        SettingService._validate_retention_configuration(
            payload=payload,
        )
        SettingService._validate_stream_timeout_configuration(
            first_token_timeout_seconds=payload.stream_first_token_timeout_seconds,
            idle_timeout_seconds=payload.stream_idle_timeout_seconds,
            max_duration_seconds=payload.stream_max_duration_seconds,
        )
        SettingService._validate_responses_chat_adapter_configuration(payload)
        explicit_fields = payload.model_fields_set
        for field, value in payload.model_dump().items():
            if field.startswith("content_guard_") and field not in explicit_fields:
                continue
            setattr(setting, field, value)
        db.commit()
        db.refresh(setting)
        SettingService.invalidate_runtime_cache()
        CacheService.invalidate_prefix("providers-runtime")
        CacheService.invalidate_prefix("route-candidates")
        CacheService.invalidate_prefix("v1-models")
        return setting

    @staticmethod
    def invalidate_runtime_cache() -> None:
        CacheService.invalidate_prefix("runtime-settings")

    @staticmethod
    def _to_runtime_payload(setting: AppSetting) -> dict:
        payload = {
            column.name: getattr(setting, column.name)
            for column in AppSetting.__table__.columns
            if column.name not in {"created_at", "updated_at"}
        }
        payload["content_guard_max_detection_delay_ms"] = min(
            500,
            max(0, int(payload.get("content_guard_max_detection_delay_ms") or 300)),
        )
        return payload

    def _validate_retention_configuration(
        *,
        payload: SettingUpdate,
    ) -> None:
        """校验日志保留时间不能为负数。"""
        retention_fields = [
            "request_log_retention_days",
            "admin_audit_log_retention_days",
            "request_child_log_retention_days",
            "exception_log_retention_days",
            "health_log_retention_days",
            "billing_log_retention_days",
            "background_job_log_retention_days",
            "user_operation_log_retention_days",
            "asset_log_retention_days",
            "alert_event_retention_days",
        ]
        for field_name in retention_fields:
            if int(getattr(payload, field_name, 0) or 0) < 0:
                raise ValueError(f"{field_name} must be >= 0")

    @staticmethod
    def _validate_stream_timeout_configuration(
        *,
        first_token_timeout_seconds: int,
        idle_timeout_seconds: int,
        max_duration_seconds: int,
    ) -> None:
        """校验流式超时配置之间的相对关系。"""
        if max_duration_seconds <= 0:
            return
        positive_timeouts = [
            value
            for value in (first_token_timeout_seconds, idle_timeout_seconds)
            if value > 0
        ]
        if positive_timeouts and max_duration_seconds < max(positive_timeouts):
            raise ValueError("stream_max_duration_seconds must be >= enabled stream chunk timeouts")

    @staticmethod
    def _validate_responses_chat_adapter_configuration(payload: SettingUpdate) -> None:
        for field_name, expected_types in (
            ("responses_chat_adapter_model_map_json", (dict,)),
            ("responses_chat_adapter_upstreams_json", (dict, list)),
        ):
            raw_value = getattr(payload, field_name, "")
            if not raw_value:
                continue
            try:
                parsed = json.loads(raw_value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{field_name} must be valid JSON") from exc
            if not isinstance(parsed, expected_types):
                raise ValueError(f"{field_name} must be a JSON object" if expected_types == (dict,) else f"{field_name} must be a JSON object or array")
