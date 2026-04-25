from datetime import datetime
from typing import Literal

from pydantic import BaseModel


RouteMode = Literal["manual", "failover", "weighted", "sticky"]


class SettingUpdate(BaseModel):
    route_mode: RouteMode
    default_provider_id: int | None = None
    manual_allow_fallback: bool = True
    global_timeout_ms: int = 30000
    global_max_retries: int = 2
    circuit_breaker_threshold: int = 3
    auto_health_check: bool = True
    health_check_interval_sec: int = 60
    recovery_probe_interval_sec: int = 30
    enable_token_logging: bool = True
    enable_payload_logging: bool = True
    enable_stream_response_persist: bool = True
    mask_sensitive_fields: bool = True
    max_logged_body_bytes: int = 16384
    allow_public_user_registration: bool = False


class SettingOut(SettingUpdate):
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
