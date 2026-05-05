from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


RouteMode = Literal["manual", "failover", "weighted", "sticky"]


class SettingUpdate(BaseModel):
    route_mode: RouteMode
    default_provider_id: int | None = None
    manual_allow_fallback: bool = True
    global_timeout_ms: int = Field(default=30000, ge=0)
    global_max_retries: int = Field(default=2, ge=0)
    global_max_request_tokens: int = Field(default=0, ge=0)
    max_v1_request_body_bytes: int = Field(default=20971520, ge=0)
    max_v1_chat_request_body_bytes: int = Field(default=0, ge=0)
    max_v1_responses_request_body_bytes: int = Field(default=0, ge=0)
    long_output_stream_threshold_tokens: int = Field(default=8192, ge=0)
    max_non_stream_response_body_bytes: int = Field(default=20971520, ge=0)
    stream_token_capture_max_bytes: int = Field(default=1048576, ge=0)
    max_logged_metadata_bytes: int = Field(default=1024, ge=0)
    circuit_breaker_threshold: int = Field(default=3, ge=0)
    auto_health_check: bool = True
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
    route_candidate_cache_ttl_sec: int = Field(default=10, ge=0, le=300)
    model_list_cache_ttl_sec: int = Field(default=15, ge=0, le=300)
    provider_status_cache_ttl_sec: int = Field(default=10, ge=0, le=300)
    async_request_logging: bool = True
    global_max_active_requests: int = Field(default=1000, ge=0)
    global_max_active_streams: int = Field(default=1000, ge=0)
    api_key_max_active_requests: int = Field(default=1000, ge=0)
    api_key_max_active_streams: int = Field(default=1000, ge=0)
    account_max_active_requests: int = Field(default=1000, ge=0)
    account_max_active_streams: int = Field(default=1000, ge=0)
    provider_max_active_requests: int = Field(default=1000, ge=0)
    provider_max_active_streams: int = Field(default=1000, ge=0)
    concurrency_lease_ttl_seconds: int = Field(default=900, ge=60)
    stream_connect_timeout_seconds: int = Field(default=10, ge=0)
    stream_first_token_timeout_seconds: int = Field(default=60, ge=0)
    stream_idle_timeout_seconds: int = Field(default=120, ge=0)
    stream_max_duration_seconds: int = Field(default=600, ge=0)


class SettingOut(SettingUpdate):
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
