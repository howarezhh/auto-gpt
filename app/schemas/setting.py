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
    circuit_breaker_threshold: int = Field(default=3, ge=0)
    auto_health_check: bool = True
    health_check_interval_sec: int = Field(default=60, ge=0)
    recovery_probe_interval_sec: int = Field(default=30, ge=0)
    enable_token_logging: bool = True
    enable_payload_logging: bool = True
    enable_stream_response_persist: bool = True
    mask_sensitive_fields: bool = True
    max_logged_body_bytes: int = Field(default=16384, ge=0)
    allow_public_user_registration: bool = False
    request_log_retention_days: int = Field(default=90, ge=0)
    admin_audit_log_retention_days: int = Field(default=180, ge=0)
    route_candidate_cache_ttl_sec: int = Field(default=10, ge=0, le=300)
    model_list_cache_ttl_sec: int = Field(default=15, ge=0, le=300)
    provider_status_cache_ttl_sec: int = Field(default=10, ge=0, le=300)
    async_request_logging: bool = True


class SettingOut(SettingUpdate):
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
