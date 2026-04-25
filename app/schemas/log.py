from datetime import datetime

from pydantic import BaseModel


class RequestLogOut(BaseModel):
    id: int
    log_type: str
    provider_id: int | None
    provider_name: str | None
    model_name: str | None
    requested_model: str | None
    request_id: str | None
    conversation_key: str | None
    session_id: str | None
    resolved_provider_model_id: int | None
    request_path: str | None
    http_method: str | None
    is_stream: bool
    has_image: bool
    success: bool
    status_code: int | None
    latency_ms: int | None
    first_token_latency_ms: int | None
    ttfb_ms: int | None
    duration_ms: int | None
    tps: float | None
    reasoning_level: str | None
    attempt_count: int | None
    prompt_cost: float | None
    completion_cost: float | None
    total_cost: float | None
    billing_status: str | None
    billing_multiplier: float | None
    channel_price_input_per_1k: float | None
    channel_price_output_per_1k: float | None
    api_client_balance_after: float | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    finish_reason: str | None
    upstream_request_id: str | None
    request_body_json: str | None
    response_body_json: str | None
    response_text: str | None
    message: str | None
    api_client_key_id: int | None
    api_client_key_name: str | None
    api_client_key_prefix: str | None
    user_account_id: int | None
    user_account_name: str | None
    api_client_auth_result: str | None
    api_client_remaining_tokens: int | None
    api_client_policy_snapshot_json: str | None
    trace_json: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class LogSummaryOut(BaseModel):
    total_requests: int = 0
    success_requests: int = 0
    failed_requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    matched_api_keys: int = 0


class LogFilterOptionOut(BaseModel):
    value: str
    label: str


class LogFilterOptionsResponse(BaseModel):
    providers: list[LogFilterOptionOut]
    model_names: list[LogFilterOptionOut]
    api_client_key_ids: list[LogFilterOptionOut]
    api_client_key_queries: list[LogFilterOptionOut]
    users: list[LogFilterOptionOut]


class LogListResponse(BaseModel):
    total: int
    items: list[RequestLogOut]
    summary: LogSummaryOut | None = None


class MetricItem(BaseModel):
    provider_id: int | None
    provider_name: str | None
    requested_model: str | None
    total_requests: int
    success_requests: int
    failed_requests: int
    failure_rate: float
    avg_latency_ms: float | None
    avg_ttfb_ms: float | None = None
    avg_duration_ms: float | None = None
    stream_requests: int = 0
    image_requests: int = 0
    unique_users: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class MetricListResponse(BaseModel):
    window_minutes: int
    items: list[MetricItem]


class MetricTimeSeriesItem(BaseModel):
    bucket_start: datetime
    total_requests: int
    success_requests: int
    failed_requests: int
    stream_requests: int = 0
    image_requests: int = 0
    avg_latency_ms: float | None = None
    avg_ttfb_ms: float | None = None
    total_tokens: int = 0


class MetricTimeSeriesResponse(BaseModel):
    window_minutes: int
    bucket_minutes: int
    items: list[MetricTimeSeriesItem]
