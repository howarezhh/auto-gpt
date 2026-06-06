from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


PROVIDER_PROTOCOL_TYPE_LABELS = {
    "both": "双协议",
    "chat_completions": "Chat Completions API",
    "responses": "Responses API",
}


def normalize_provider_protocol_type(value: str | None) -> str:
    raw = str(value or "").strip()
    normalized = raw.lower().replace("-", "_").replace(" ", "_")
    compact = raw.lower().replace(" ", "").replace("_", "").replace("-", "")
    aliases = {
        "": "both",
        "both": "both",
        "all": "both",
        "dual": "both",
        "dualprotocol": "both",
        "chatandresponses": "both",
        "chatresponses": "both",
        "都支持": "both",
        "双协议": "both",
        "全部": "both",
        "全协议": "both",
        "chat": "chat_completions",
        "chatcompletion": "chat_completions",
        "chatcompletions": "chat_completions",
        "chatcompletionapi": "chat_completions",
        "chatcompletionsapi": "chat_completions",
        "聊天": "chat_completions",
        "对话": "chat_completions",
        "responses": "responses",
        "response": "responses",
        "responsesapi": "responses",
        "响应": "responses",
        "响应式": "responses",
    }
    if normalized in PROVIDER_PROTOCOL_TYPE_LABELS:
        return normalized
    if compact in aliases:
        return aliases[compact]
    raise ValueError("协议仅支持 双协议、Chat Completions API 或 Responses API")


def format_provider_protocol_label(value: str | None) -> str:
    return PROVIDER_PROTOCOL_TYPE_LABELS.get(normalize_provider_protocol_type(value), PROVIDER_PROTOCOL_TYPE_LABELS["both"])


class ProviderModelConfigBase(BaseModel):
    model_name: str = Field(..., min_length=1)
    enabled: bool = True
    priority: int = 100
    weight: int = 100
    supports_stream: bool = True
    supports_vision: bool = True
    supports_tools: bool = True
    supports_chat_completions: bool = True
    supports_responses: bool = True
    context_window_tokens: int | None = Field(default=None, ge=1)
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    price_multiplier: float = Field(default=1.0, gt=0)
    input_price_per_1k: float | None = Field(default=None, ge=0)
    output_price_per_1k: float | None = Field(default=None, ge=0)
    cache_price_per_1k: float | None = Field(default=None, ge=0)

    @field_validator("model_name")
    @classmethod
    def normalize_model_name(cls, value: str) -> str:
        return value.strip()


class ProviderModelConfigInput(ProviderModelConfigBase):
    pass


class ProviderModelConfigOut(ProviderModelConfigBase):
    id: int
    supports_image_generation: bool = False
    health_status: str
    circuit_state: str
    circuit_opened_at: datetime | None
    last_check_at: datetime | None
    last_latency_ms: int | None
    failure_count: int
    success_count: int
    last_error: str | None
    recent_request_count: int = 0
    success_rate: float | None = None
    avg_first_token_latency_ms: float | None = None
    stability_score: float | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ProviderModelMountProviderOut(BaseModel):
    id: int
    name: str
    base_url: str
    protocol_type: str = "both"
    protocol_label: str = "双协议"
    group_name: str | None = None
    region_tag: str | None = None
    enabled: bool
    health_status: str


class ProviderModelMountOut(BaseModel):
    provider: ProviderModelMountProviderOut
    model: ProviderModelConfigOut


class ProviderModelMountListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    total_pages: int
    items: list[ProviderModelMountOut]


class ProviderModelConfigUpdate(BaseModel):
    enabled: bool | None = None
    priority: int | None = None
    weight: int | None = None
    supports_stream: bool | None = None
    supports_vision: bool | None = None
    supports_tools: bool | None = None
    supports_chat_completions: bool | None = None
    supports_responses: bool | None = None
    context_window_tokens: int | None = Field(default=None, ge=1)
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    price_multiplier: float | None = Field(default=None, gt=0)
    input_price_per_1k: float | None = Field(default=None, ge=0)
    output_price_per_1k: float | None = Field(default=None, ge=0)
    cache_price_per_1k: float | None = Field(default=None, ge=0)


class ProviderBatchConnectivityTestRequest(BaseModel):
    provider_ids: list[int] = Field(default_factory=list)


class ProviderBatchImportRequest(BaseModel):
    content: str = Field(..., min_length=1)
    dry_run: bool = True
    skip_duplicates: bool = True

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        return value.strip()


class ProviderBatchImportItemOut(BaseModel):
    index: int
    name: str | None = None
    base_url: str | None = None
    model_count: int = 0
    valid: bool = False
    skipped: bool = False
    created: bool = False
    errors: list[str] = Field(default_factory=list)
    provider: dict[str, Any] | None = None


class ProviderBatchImportResponse(BaseModel):
    total: int
    valid_count: int
    created_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    dry_run: bool
    template: str | None = None
    items: list[ProviderBatchImportItemOut]


class ProviderBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    base_url: str = Field(..., min_length=1)
    api_key: str = Field(..., min_length=1)
    provider_type: str = "openai_compatible"
    protocol_type: str = "both"
    group_name: str | None = None
    region_tag: str | None = None
    enabled: bool = True
    priority: int = 100
    weight: int = 100
    timeout_ms: int = 30000
    max_retries: int = 1
    max_active_requests: int | None = Field(default=20, ge=0)
    max_active_streams: int | None = Field(default=10, ge=0)
    max_qps: int | None = Field(default=20, ge=0)
    max_rpm: int | None = Field(default=20, ge=0)
    max_error_rate: float | None = Field(default=80.0, ge=0, le=100)
    first_token_timeout_sec: int | None = Field(default=60, ge=1)
    maintenance_window: str | None = None
    maintenance_mode_enabled: bool = False
    auto_circuit_break_enabled: bool = True
    auto_recover_enabled: bool = True
    circuit_breaker_threshold_override: int | None = Field(default=None, ge=0)
    recovery_probe_interval_sec_override: int | None = Field(default=None, ge=0)
    models: list[str] = Field(default_factory=list)
    model_configs: list[ProviderModelConfigInput] = Field(default_factory=list)
    remark: str | None = None

    @field_validator("models")
    @classmethod
    def normalize_models(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item and item.strip()]

    @field_validator("model_configs")
    @classmethod
    def normalize_model_configs(cls, value: list[ProviderModelConfigInput]) -> list[ProviderModelConfigInput]:
        seen: set[str] = set()
        normalized: list[ProviderModelConfigInput] = []
        for item in value:
            if item.model_name in seen:
                continue
            seen.add(item.model_name)
            normalized.append(item)
        return normalized

    @field_validator("protocol_type")
    @classmethod
    def normalize_protocol_type(cls, value: str | None) -> str:
        return normalize_provider_protocol_type(value)


class ProviderCreate(ProviderBase):
    pass


class ProviderUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    provider_type: str | None = None
    protocol_type: str | None = None
    group_name: str | None = None
    region_tag: str | None = None
    enabled: bool | None = None
    priority: int | None = None
    weight: int | None = None
    timeout_ms: int | None = None
    max_retries: int | None = None
    max_active_requests: int | None = Field(default=None, ge=0)
    max_active_streams: int | None = Field(default=None, ge=0)
    max_qps: int | None = Field(default=None, ge=0)
    max_rpm: int | None = Field(default=None, ge=0)
    max_error_rate: float | None = Field(default=None, ge=0, le=100)
    first_token_timeout_sec: int | None = Field(default=None, ge=1)
    maintenance_window: str | None = None
    maintenance_mode_enabled: bool | None = None
    auto_circuit_break_enabled: bool | None = None
    auto_recover_enabled: bool | None = None
    circuit_breaker_threshold_override: int | None = Field(default=None, ge=0)
    recovery_probe_interval_sec_override: int | None = Field(default=None, ge=0)
    models: list[str] | None = None
    model_configs: list[ProviderModelConfigInput] | None = None
    remark: str | None = None

    @field_validator("models")
    @classmethod
    def normalize_models(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return [item.strip() for item in value if item and item.strip()]

    @field_validator("model_configs")
    @classmethod
    def normalize_model_configs(cls, value: list[ProviderModelConfigInput] | None) -> list[ProviderModelConfigInput] | None:
        if value is None:
            return None
        seen: set[str] = set()
        normalized: list[ProviderModelConfigInput] = []
        for item in value:
            if item.model_name in seen:
                continue
            seen.add(item.model_name)
            normalized.append(item)
        return normalized

    @field_validator("protocol_type")
    @classmethod
    def normalize_protocol_type(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_provider_protocol_type(value)


class ProviderOut(BaseModel):
    id: int
    name: str
    base_url: str
    api_key: str
    api_key_masked: str
    provider_type: str
    protocol_type: str
    protocol_label: str
    group_name: str | None
    region_tag: str | None
    enabled: bool
    priority: int
    weight: int
    timeout_ms: int
    max_retries: int
    max_active_requests: int | None
    max_active_streams: int | None
    max_qps: int | None
    max_rpm: int | None
    max_error_rate: float | None
    first_token_timeout_sec: int | None
    active_requests: int = 0
    active_streams: int = 0
    current_qps: int = 0
    current_rpm: int = 0
    maintenance_window: str | None
    maintenance_mode_enabled: bool
    auto_circuit_break_enabled: bool
    auto_recover_enabled: bool
    circuit_breaker_threshold_override: int | None
    recovery_probe_interval_sec_override: int | None
    models: list[str]
    model_configs: list[ProviderModelConfigOut]
    health_status: str
    last_check_at: datetime | None
    last_latency_ms: int | None
    failure_count: int
    success_count: int
    circuit_state: str
    recent_request_count: int = 0
    success_rate: float | None = None
    avg_first_token_latency_ms: float | None = None
    stability_score: float | None = None
    best_input_price_per_1k: float | None = None
    best_output_price_per_1k: float | None = None
    credential_rotated_at: datetime | None = None
    credential_hint: str | None = None
    remark: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ProviderTelemetryCardOut(BaseModel):
    id: str
    label: str
    value: int | float


class ProviderPageContentOut(BaseModel):
    providers: list[ProviderOut]
    summary: dict[str, int | float]
    telemetry_cards: list[ProviderTelemetryCardOut]


class ProviderOptionOut(BaseModel):
    id: int
    name: str
    group_name: str | None = None
    region_tag: str | None = None
    enabled: bool
    health_status: str
    protocol_type: str = "both"
    protocol_label: str = "双协议"
    models: list[str] = Field(default_factory=list)


class ProviderPlaygroundModelOut(BaseModel):
    id: int
    model_name: str
    enabled: bool
    supports_stream: bool = True
    supports_vision: bool = True
    supports_tools: bool = True
    supports_image_generation: bool = False
    supports_chat_completions: bool = True
    supports_responses: bool = True


class ProviderPlaygroundOut(BaseModel):
    id: int
    name: str
    group_name: str | None = None
    region_tag: str | None = None
    enabled: bool
    health_status: str
    protocol_type: str = "both"
    protocol_label: str = "双协议"
    models: list[str] = Field(default_factory=list)
    model_configs: list[ProviderPlaygroundModelOut] = Field(default_factory=list)


class ProviderSummaryOut(BaseModel):
    id: int
    name: str
    group_name: str | None = None
    region_tag: str | None = None
    enabled: bool
    priority: int
    weight: int
    health_status: str
    protocol_type: str = "both"
    protocol_label: str = "双协议"
    circuit_state: str
    last_latency_ms: int | None = None
    models: list[str] = Field(default_factory=list)


class ProviderCredentialRotateIn(BaseModel):
    api_key: str = Field(..., min_length=1)
    credential_hint: str | None = None

    @field_validator("api_key")
    @classmethod
    def normalize_api_key(cls, value: str) -> str:
        return value.strip()

    @field_validator("credential_hint")
    @classmethod
    def normalize_credential_hint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class ProviderDiscoverModelsIn(BaseModel):
    provider_id: int | None = None
    base_url: str | None = None
    api_key: str | None = None
    provider_type: str | None = None
    timeout_ms: int | None = Field(default=None, ge=1000)
    existing_model_names: list[str] = Field(default_factory=list)

    @field_validator("base_url", "api_key", "provider_type")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("existing_model_names")
    @classmethod
    def normalize_existing_model_names(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        normalized: list[str] = []
        for item in value:
            current = (item or "").strip()
            if not current or current in seen:
                continue
            seen.add(current)
            normalized.append(current)
        return normalized


class ProviderDiscoveredModelOut(BaseModel):
    model_name: str
    supports_stream: bool = True
    supports_vision: bool = True
    supports_tools: bool = True
    supports_image_generation: bool = False
    supports_chat_completions: bool = True
    supports_responses: bool = True
    context_window_tokens: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    already_configured: bool = False


class ProviderDiscoverModelsResponse(BaseModel):
    provider_name: str | None = None
    source_base_url: str
    total_models: int
    items: list[ProviderDiscoveredModelOut]


class ProviderAvailabilityPointOut(BaseModel):
    bucket_start: datetime
    total_requests: int
    success_requests: int
    failed_requests: int
    success_rate: float
    avg_latency_ms: float | None = None


class ProviderAvailabilityResponse(BaseModel):
    provider_id: int
    provider_name: str
    window_hours: int
    bucket_minutes: int
    items: list[ProviderAvailabilityPointOut]
