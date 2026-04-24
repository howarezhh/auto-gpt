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
    resolved_provider_model_id: int | None
    request_path: str | None
    is_stream: bool
    has_image: bool
    success: bool
    status_code: int | None
    latency_ms: int | None
    first_token_latency_ms: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    finish_reason: str | None
    upstream_request_id: str | None
    request_body_json: str | None
    response_body_json: str | None
    response_text: str | None
    message: str | None
    api_client_key_id: int | None
    api_client_key_name: str | None
    api_client_key_prefix: str | None
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


class LogListResponse(BaseModel):
    total: int
    items: list[RequestLogOut]
    summary: LogSummaryOut | None = None


class MetricItem(BaseModel):
    provider_id: int | None
    provider_name: str | None
    requested_model: str | None
    total_requests: int
    failed_requests: int
    failure_rate: float
    avg_latency_ms: float | None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class MetricListResponse(BaseModel):
    window_minutes: int
    items: list[MetricItem]
