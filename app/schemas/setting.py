from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.utils.content_guard_config import (
    CONTENT_GUARD_MAX_SCAN_BYTES_LIMIT,
    CONTENT_GUARD_PROBE_INTERVAL_MAX_SECONDS,
    CONTENT_GUARD_STREAM_BUFFER_MAX_BYTES_LIMIT,
    content_guard_default,
    validate_content_guard_settings,
)


class SettingUpdate(BaseModel):
    route_exhausted_retry_max_wait_seconds: int = Field(default=600, ge=0, le=600)
    route_exhausted_retry_infinite_enabled: bool = False
    trusted_providers_only: bool = False
    max_candidate_count: int = Field(default=10, ge=1, le=500)
    route_candidate_expand_count: int = Field(default=5, ge=0, le=100)
    global_max_request_tokens: int = Field(default=0, ge=0)
    max_v1_request_body_bytes: int = Field(default=20971520, ge=0)
    max_v1_chat_request_body_bytes: int = Field(default=0, ge=0)
    max_v1_responses_request_body_bytes: int = Field(default=0, ge=0)
    long_output_stream_threshold_tokens: int = Field(default=8192, ge=0)
    max_non_stream_response_body_bytes: int = Field(default=20971520, ge=0)
    stream_token_capture_max_bytes: int = Field(default=1048576, ge=0)
    max_logged_metadata_bytes: int = Field(default=1024, ge=0)
    content_guard_enabled: bool = content_guard_default("content_guard_enabled")
    content_guard_precheck_auto_enabled: bool = content_guard_default("content_guard_precheck_auto_enabled")
    content_guard_block_on_high_risk: bool = content_guard_default("content_guard_block_on_high_risk")
    content_guard_probe_protocol_type: Literal["chat_completions", "responses"] = content_guard_default("content_guard_probe_protocol_type")
    content_guard_probe_interval_sec: int = Field(
        default=content_guard_default("content_guard_probe_interval_sec"),
        ge=300,
        le=CONTENT_GUARD_PROBE_INTERVAL_MAX_SECONDS,
    )
    content_guard_max_scan_bytes: int = Field(default=content_guard_default("content_guard_max_scan_bytes"), ge=1024, le=CONTENT_GUARD_MAX_SCAN_BYTES_LIMIT)
    content_guard_stream_buffer_max_bytes: int = Field(default=content_guard_default("content_guard_stream_buffer_max_bytes"), ge=1024, le=CONTENT_GUARD_STREAM_BUFFER_MAX_BYTES_LIMIT)
    content_guard_low_trust_requires_buffer: bool = content_guard_default("content_guard_low_trust_requires_buffer")
    content_guard_high_risk_strategy: Literal["switch_provider", "block", "safe_error", "record_only"] = content_guard_default("content_guard_high_risk_strategy")
    content_guard_max_detection_delay_ms: int = Field(default=content_guard_default("content_guard_max_detection_delay_ms"), ge=0, le=500)
    content_guard_stream_mode: Literal["pass_through_scan", "buffer_300ms", "full_buffer"] = content_guard_default("content_guard_stream_mode")
    content_guard_url_check_enabled: bool = content_guard_default("content_guard_url_check_enabled")
    content_guard_url_allowlist_json: str = Field(default=content_guard_default("content_guard_url_allowlist_json"), max_length=10000)
    content_guard_async_review_enabled: bool = content_guard_default("content_guard_async_review_enabled")
    content_guard_high_risk_confidence_threshold: int = Field(
        default=content_guard_default("content_guard_high_risk_confidence_threshold"),
        ge=0,
        le=100,
    )
    content_guard_enhanced_detection_enabled: bool = content_guard_default("content_guard_enhanced_detection_enabled")
    content_guard_enhanced_illegal_enabled: bool = content_guard_default("content_guard_enhanced_illegal_enabled")
    content_guard_enhanced_ad_enabled: bool = content_guard_default("content_guard_enhanced_ad_enabled")
    content_guard_enhanced_custom_enabled: bool = content_guard_default("content_guard_enhanced_custom_enabled")
    content_guard_enhanced_obfuscation_enabled: bool = content_guard_default("content_guard_enhanced_obfuscation_enabled")
    content_guard_enhanced_threshold: int = Field(default=content_guard_default("content_guard_enhanced_threshold"), ge=0, le=100)
    content_guard_enhanced_context_window_chars: int = Field(default=content_guard_default("content_guard_enhanced_context_window_chars"), ge=24, le=512)
    circuit_breaker_threshold: int = Field(default=3, ge=0)
    auto_health_check: bool = False
    health_check_interval_sec: int = Field(default=300, ge=300)
    recovery_probe_interval_sec: int = Field(default=30, ge=0)
    enable_token_logging: bool = True
    enable_payload_logging: bool = False
    enable_stream_response_persist: bool = False
    mask_sensitive_fields: bool = True
    max_logged_body_bytes: int = Field(default=16384, ge=0)
    allow_public_user_registration: bool = False
    request_log_retention_days: int = Field(default=90, ge=0)
    admin_audit_log_retention_days: int = Field(default=180, ge=0)
    request_child_log_retention_days: int = Field(default=90, ge=0)
    exception_log_retention_days: int = Field(default=180, ge=0)
    health_log_retention_days: int = Field(default=7, ge=0)
    billing_log_retention_days: int = Field(default=365, ge=0)
    background_job_log_retention_days: int = Field(default=90, ge=0)
    user_operation_log_retention_days: int = Field(default=180, ge=0)
    asset_log_retention_days: int = Field(default=180, ge=0)
    alert_event_retention_days: int = Field(default=180, ge=0)
    route_candidate_cache_ttl_sec: int = Field(default=10, ge=0, le=300)
    model_list_cache_ttl_sec: int = Field(default=15, ge=0, le=300)
    provider_status_cache_ttl_sec: int = Field(default=10, ge=0, le=300)
    async_request_logging: bool = True
    global_qps_limit: int = Field(default=20, ge=0)
    global_rpm_limit: int = Field(default=20, ge=0)
    account_qps_limit: int = Field(default=20, ge=0)
    account_rpm_limit: int = Field(default=20, ge=0)
    global_max_active_requests: int = Field(default=20, ge=0)
    global_max_active_streams: int = Field(default=10, ge=0)
    api_key_max_active_requests: int = Field(default=20, ge=0)
    api_key_max_active_streams: int = Field(default=10, ge=0)
    account_max_active_requests: int = Field(default=20, ge=0)
    account_max_active_streams: int = Field(default=10, ge=0)
    provider_max_active_requests: int = Field(default=20, ge=0)
    provider_max_active_streams: int = Field(default=10, ge=0)
    concurrency_lease_ttl_seconds: int = Field(default=900, ge=60)
    stream_connect_timeout_seconds: int = Field(default=10, ge=0)
    stream_first_token_timeout_seconds: int = Field(default=60, ge=0)
    stream_idle_timeout_seconds: int = Field(default=120, ge=0)
    stream_max_duration_seconds: int = Field(default=600, ge=0)
    responses_chat_adapter_enabled: bool = False
    responses_chat_adapter_storage_type: Literal["memory", "redis", "database", "postgresql", "postgres"] = "database"
    responses_chat_adapter_ttl_seconds: int = Field(default=86400, ge=0)
    responses_chat_adapter_model_map_json: str = ""
    responses_chat_adapter_max_tool_rounds: int = Field(default=10, ge=1, le=100)
    responses_chat_adapter_web_search_enabled: bool = False
    responses_chat_adapter_search_proxy_url: str = ""
    responses_chat_adapter_upstream_base_url: str = ""
    responses_chat_adapter_upstream_api_key: str = ""
    responses_chat_adapter_upstreams_json: str = ""

    responses_chat_adapter_context_window_tokens: int = Field(default=128000, ge=0)
    responses_chat_adapter_snapshot_max_bytes: int = Field(default=1048576, ge=0)
    responses_chat_adapter_db_cleanup_interval_seconds: int = Field(default=21600, ge=300)

    @model_validator(mode="after")
    def validate_content_guard_fields(self) -> "SettingUpdate":
        normalized = validate_content_guard_settings(self.model_dump())
        for field_name, value in normalized.items():
            if field_name.startswith("content_guard_") and hasattr(self, field_name):
                setattr(self, field_name, value)
        return self


class SettingOut(SettingUpdate):
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
