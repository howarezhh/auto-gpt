from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RequestLog(Base):
    __tablename__ = "request_logs"
    __table_args__ = (
        Index("ix_request_logs_created_at", "created_at"),
        Index("ix_request_logs_route_metrics", "log_type", "created_at", "provider_id", "requested_model", "success"),
        Index("ix_request_logs_api_key_created_at", "api_client_key_id", "created_at"),
        Index("ix_request_logs_user_account_created_at", "user_account_id", "created_at"),
        Index("ix_request_logs_session_id", "session_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    log_type: Mapped[str] = mapped_column(Text, nullable=False)
    provider_id: Mapped[int | None] = mapped_column(ForeignKey("providers.id"), nullable=True)
    provider_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    model_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    tenant_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    project_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    app_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    environment_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    conversation_key: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_provider_model_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    http_method: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_stream: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    has_image: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_token_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ttfb_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tps: Mapped[float | None] = mapped_column(Float, nullable=True)
    reasoning_level: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_cost: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    completion_cost: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    total_cost: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    billing_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    billing_multiplier: Mapped[float | None] = mapped_column(Float, nullable=True)
    channel_price_input_per_1k: Mapped[float | None] = mapped_column(Float, nullable=True)
    channel_price_output_per_1k: Mapped[float | None] = mapped_column(Float, nullable=True)
    api_client_balance_after: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_write_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    finish_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    upstream_request_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_body_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_body_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    retryable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    api_client_key_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    api_client_key_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_client_key_prefix: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_account_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    user_account_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_client_auth_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_client_remaining_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    api_client_remaining_requests_daily: Mapped[int | None] = mapped_column(Integer, nullable=True)
    api_client_remaining_cost_daily: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    api_client_policy_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
