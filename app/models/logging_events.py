from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.utils.decimal_utils import DB_MONEY_PRECISION, DB_MONEY_SCALE


class RequestAuthEvent(Base):
    __tablename__ = "request_auth_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    request_log_id: Mapped[int | None] = mapped_column(ForeignKey("request_logs.id", ondelete="CASCADE"), nullable=True, index=True)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    auth_result: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    api_client_key_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    api_client_key_prefix: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_account_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    remaining_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    remaining_requests_daily: Mapped[int | None] = mapped_column(Integer, nullable=True)
    remaining_cost_daily: Mapped[float | None] = mapped_column(Float, nullable=True)
    policy_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class RequestValidationEvent(Base):
    __tablename__ = "request_validation_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    request_log_id: Mapped[int | None] = mapped_column(ForeignKey("request_logs.id", ondelete="CASCADE"), nullable=True, index=True)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    validation_stage: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    limit_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_body_summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    safe_detail_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class RequestModelPermissionEvent(Base):
    __tablename__ = "request_model_permission_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    request_log_id: Mapped[int | None] = mapped_column(ForeignKey("request_logs.id", ondelete="CASCADE"), nullable=True, index=True)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    requested_model: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    resolved_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    endpoint_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    required_capabilities_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    permission_result: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    reason_details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class RequestRouteDecisionEvent(Base):
    __tablename__ = "request_route_decision_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    request_log_id: Mapped[int | None] = mapped_column(ForeignKey("request_logs.id", ondelete="CASCADE"), nullable=True, index=True)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    route_round: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    route_policy: Mapped[str | None] = mapped_column(Text, nullable=True)
    candidate_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    selected_provider_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    selected_provider_model_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sticky_hit: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    excluded_summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    diagnostics_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_wait_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class RequestProviderAttemptEvent(Base):
    __tablename__ = "request_provider_attempt_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    request_log_id: Mapped[int | None] = mapped_column(ForeignKey("request_logs.id", ondelete="CASCADE"), nullable=True, index=True)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    attempt_index: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    provider_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    provider_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_model_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    endpoint_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    protocol_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    capacity_lease_acquired: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    result: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    upstream_request_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    retryable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class RequestUpstreamResponseEvent(Base):
    __tablename__ = "request_upstream_response_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    request_log_id: Mapped[int | None] = mapped_column(ForeignKey("request_logs.id", ondelete="CASCADE"), nullable=True, index=True)
    provider_attempt_event_id: Mapped[int | None] = mapped_column(ForeignKey("request_provider_attempt_events.id", ondelete="SET NULL"), nullable=True, index=True)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    upstream_request_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    finish_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    usage_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_text_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_body_truncated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class RequestStreamEvent(Base):
    __tablename__ = "request_stream_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    request_log_id: Mapped[int | None] = mapped_column(ForeignKey("request_logs.id", ondelete="CASCADE"), nullable=True, index=True)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    stream_result: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    first_token_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ttfb_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    captured_text_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sse_error_sent: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    done_sent: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    disconnect_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class RequestErrorResponseEvent(Base):
    __tablename__ = "request_error_response_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    request_log_id: Mapped[int | None] = mapped_column(ForeignKey("request_logs.id", ondelete="CASCADE"), nullable=True, index=True)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    public_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    retryable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    recoverable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    required_endpoint: Mapped[str | None] = mapped_column(Text, nullable=True)
    missing_capability: Mapped[str | None] = mapped_column(Text, nullable=True)
    diagnostic_sample_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class RequestContentGuardEvent(Base):
    __tablename__ = "request_content_guard_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    request_log_id: Mapped[int | None] = mapped_column(ForeignKey("request_logs.id", ondelete="CASCADE"), nullable=True, index=True)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    provider_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    provider_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_model_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    model_name: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    requested_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_path: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    is_stream: Mapped[bool | None] = mapped_column(Boolean, nullable=True, index=True)
    guard_stage: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    guard_result: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    risk_level: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    matched_categories_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    matched_rules_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    action: Mapped[str | None] = mapped_column(Text, nullable=True)
    excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_status_after: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_delta: Mapped[int | None] = mapped_column(Integer, nullable=True)
    diagnostics_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class RequestBillingEvent(Base):
    __tablename__ = "request_billing_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    request_log_id: Mapped[int | None] = mapped_column(ForeignKey("request_logs.id", ondelete="CASCADE"), nullable=True, index=True)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    billing_event_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    billing_stage: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    token_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_write_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    completion_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    balance_before: Mapped[float | None] = mapped_column(Float, nullable=True)
    balance_after: Mapped[float | None] = mapped_column(Float, nullable=True)
    attempt_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class ExceptionEvent(Base):
    __tablename__ = "exception_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    event_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True, index=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False, default="exception")
    event_name: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    correlation_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    module: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    severity: Mapped[str] = mapped_column(Text, nullable=False, default="danger", index=True)
    actor_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[str] = mapped_column(Text, nullable=False, default="failed", index=True)
    exception_type: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    handler_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_path: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    method: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_external_v1: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    stack_hash: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    stack_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class HealthCheckRun(Base):
    __tablename__ = "health_check_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    run_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True, index=True)
    trigger_type: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    scope_type: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    scope_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    phase_keys_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_probes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success_probes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_probes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    overall_result: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class HealthProbeEvent(Base):
    __tablename__ = "health_probe_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    run_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    provider_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    provider_model_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    model_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    probe_type: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    endpoint_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    protocol_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    capability_result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_guard_result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class TokenFinalizeEvent(Base):
    __tablename__ = "token_finalize_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    request_log_id: Mapped[int | None] = mapped_column(ForeignKey("request_logs.id", ondelete="SET NULL"), nullable=True, index=True)
    queue_source: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    usage_before_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    usage_after_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    enable_usage_fill: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    result: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class BillingProcessEvent(Base):
    __tablename__ = "billing_process_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    request_log_id: Mapped[int | None] = mapped_column(ForeignKey("request_logs.id", ondelete="SET NULL"), nullable=True, index=True)
    api_client_key_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    user_account_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    pricing_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    pricing_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    balance_delta: Mapped[float | None] = mapped_column(Float, nullable=True)
    balance_after: Mapped[float | None] = mapped_column(Float, nullable=True)
    billing_status: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    billing_record_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class BackgroundJobEvent(Base):
    __tablename__ = "background_job_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    job_run_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    job_name: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    trigger_type: Mapped[str] = mapped_column(Text, nullable=False, default="scheduler", index=True)
    lock_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    lock_status: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    processed_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failed_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class UserOperationAuditLog(Base):
    __tablename__ = "user_operation_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_account_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    username: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    action: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    entity_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    entity_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    detail_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    result: Mapped[str] = mapped_column(Text, nullable=False, default="success", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class AssetEvent(Base):
    __tablename__ = "asset_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    asset_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    asset_event_type: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    actor_type: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    actor_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sha256_prefix: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256_hex: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_scope: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    result: Mapped[str] = mapped_column(Text, nullable=False, default="success", index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    request_log_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, index=True)
