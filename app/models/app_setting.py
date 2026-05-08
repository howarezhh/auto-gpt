from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AppSetting(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    route_mode: Mapped[str] = mapped_column(Text, nullable=False, default="failover")
    default_provider_id: Mapped[int | None] = mapped_column(ForeignKey("providers.id"), nullable=True)
    manual_allow_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    global_timeout_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=30000)
    global_max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    global_max_request_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_v1_request_body_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=20971520)
    max_v1_chat_request_body_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_v1_responses_request_body_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    long_output_stream_threshold_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=8192)
    max_non_stream_response_body_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=20971520)
    stream_token_capture_max_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=1048576)
    max_logged_metadata_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=1024)
    circuit_breaker_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    auto_health_check: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    health_check_interval_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    recovery_probe_interval_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    enable_token_logging: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    enable_payload_logging: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    enable_stream_response_persist: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    mask_sensitive_fields: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    max_logged_body_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=16384)
    allow_public_user_registration: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    request_log_retention_days: Mapped[int] = mapped_column(Integer, nullable=False, default=90)
    admin_audit_log_retention_days: Mapped[int] = mapped_column(Integer, nullable=False, default=180)
    route_candidate_cache_ttl_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    model_list_cache_ttl_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=15)
    provider_status_cache_ttl_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    async_request_logging: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    global_max_active_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)
    global_max_active_streams: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)
    api_key_max_active_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)
    api_key_max_active_streams: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)
    account_max_active_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)
    account_max_active_streams: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)
    provider_max_active_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)
    provider_max_active_streams: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)
    concurrency_lease_ttl_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=900)
    stream_connect_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    stream_first_token_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    stream_idle_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=120)
    stream_max_duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=600)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )
