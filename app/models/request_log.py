from app.utils.timezone import now_beijing
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, Numeric, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.utils.decimal_utils import (
    DB_MONEY_PRECISION,
    DB_MONEY_SCALE,
    DB_MULTIPLIER_PRECISION,
    DB_MULTIPLIER_SCALE,
    DB_PRICE_PRECISION,
    DB_PRICE_SCALE,
)


class RequestLog(Base):
    __tablename__ = "request_logs"
    __table_args__ = (
        Index("ix_request_logs_created_at", "created_at"),
        Index("ix_request_logs_route_metrics", "log_type", "created_at", "provider_id", "model_name", "success"),
        Index("ix_request_logs_recent_provider_model_health", "created_at", "provider_id", "resolved_provider_model_id", "log_type", "success"),
        Index(
            "ix_request_logs_token_finalize_pending",
            "created_at",
            "id",
            postgresql_where=text(
                "api_client_key_id IS NOT NULL "
                "AND billable = true "
                "AND log_type IN ('chat','responses','embeddings') "
                "AND request_path IS NOT NULL "
                "AND request_path <> '/v1/models' "
                "AND request_path NOT LIKE '/v1/models/%' "
                "AND (billing_finalized_at IS NULL OR billing_status = 'pending_tokens') "
                "AND (token_finalize_attempt_count IS NULL OR token_finalize_attempt_count < 3)"
            ),
        ),
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
    billable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    billable_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_token_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ttfb_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tps: Mapped[float | None] = mapped_column(Float, nullable=True)
    reasoning_level: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_reasoning_effort: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_cost: Mapped[Decimal | None] = mapped_column(Numeric(DB_MONEY_PRECISION, DB_MONEY_SCALE), nullable=True)
    completion_cost: Mapped[Decimal | None] = mapped_column(Numeric(DB_MONEY_PRECISION, DB_MONEY_SCALE), nullable=True)
    total_cost: Mapped[Decimal | None] = mapped_column(Numeric(DB_MONEY_PRECISION, DB_MONEY_SCALE), nullable=True)
    billing_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    billing_finalized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    billing_event_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    billing_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    billing_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    pricing_tier_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    pricing_tier_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_finalize_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token_finalize_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    billing_multiplier: Mapped[Decimal | None] = mapped_column(Numeric(DB_MULTIPLIER_PRECISION, DB_MULTIPLIER_SCALE), nullable=True)
    channel_price_input_per_1k: Mapped[Decimal | None] = mapped_column(Numeric(DB_PRICE_PRECISION, DB_PRICE_SCALE), nullable=True)
    channel_price_output_per_1k: Mapped[Decimal | None] = mapped_column(Numeric(DB_PRICE_PRECISION, DB_PRICE_SCALE), nullable=True)
    channel_price_cache_per_1k: Mapped[Decimal | None] = mapped_column(Numeric(DB_PRICE_PRECISION, DB_PRICE_SCALE), nullable=True)
    channel_price_cache_write_per_1k: Mapped[Decimal | None] = mapped_column(Numeric(DB_PRICE_PRECISION, DB_PRICE_SCALE), nullable=True)
    source_currency: Mapped[str | None] = mapped_column(Text, nullable=True)
    billing_currency: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_price_input_per_1k: Mapped[Decimal | None] = mapped_column(Numeric(DB_PRICE_PRECISION, DB_PRICE_SCALE), nullable=True)
    source_price_output_per_1k: Mapped[Decimal | None] = mapped_column(Numeric(DB_PRICE_PRECISION, DB_PRICE_SCALE), nullable=True)
    source_price_cache_per_1k: Mapped[Decimal | None] = mapped_column(Numeric(DB_PRICE_PRECISION, DB_PRICE_SCALE), nullable=True)
    source_price_cache_write_per_1k: Mapped[Decimal | None] = mapped_column(Numeric(DB_PRICE_PRECISION, DB_PRICE_SCALE), nullable=True)
    source_prompt_cost: Mapped[Decimal | None] = mapped_column(Numeric(DB_MONEY_PRECISION, DB_MONEY_SCALE), nullable=True)
    source_completion_cost: Mapped[Decimal | None] = mapped_column(Numeric(DB_MONEY_PRECISION, DB_MONEY_SCALE), nullable=True)
    source_total_cost: Mapped[Decimal | None] = mapped_column(Numeric(DB_MONEY_PRECISION, DB_MONEY_SCALE), nullable=True)
    exchange_rate_to_billing_currency: Mapped[Decimal | None] = mapped_column(Numeric(DB_PRICE_PRECISION, DB_PRICE_SCALE), nullable=True)
    exchange_rate_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    exchange_rate_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    exchange_rate_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    rounding_strategy: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_client_balance_after: Mapped[Decimal | None] = mapped_column(Numeric(DB_MONEY_PRECISION, DB_MONEY_SCALE), nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_write_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_audio_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_audio_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    accepted_prediction_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rejected_prediction_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    upstream_usage_missing: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    usage_details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    finish_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    upstream_request_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_body_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_body_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    retryable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    content_guard_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_guard_risk_level: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_guard_categories_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_guard_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_guard_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_guard_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_guard_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_guard_buffer_wait_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_guard_retry_provider_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_guard_final_strategy: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_guard_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    content_guard_score_delta: Mapped[int | None] = mapped_column(Integer, nullable=True)
    api_client_key_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    api_client_key_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_client_key_prefix: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_account_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    user_account_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_client_auth_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_client_policy_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=now_beijing)
