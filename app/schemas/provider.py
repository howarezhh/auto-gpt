from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class ProviderModelConfigBase(BaseModel):
    model_name: str = Field(..., min_length=1)
    enabled: bool = True
    priority: int = 100
    weight: int = 100
    supports_stream: bool = True
    supports_vision: bool = False
    supports_tools: bool = False
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


class ProviderBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    base_url: str = Field(..., min_length=1)
    api_key: str = Field(..., min_length=1)
    provider_type: str = "openai_compatible"
    group_name: str | None = None
    region_tag: str | None = None
    enabled: bool = True
    priority: int = 100
    weight: int = 100
    timeout_ms: int = 30000
    max_retries: int = 1
    max_active_requests: int | None = Field(default=300, ge=0)
    max_active_streams: int | None = Field(default=150, ge=0)
    max_qps: int | None = Field(default=None, ge=0)
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


class ProviderCreate(ProviderBase):
    pass


class ProviderUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    provider_type: str | None = None
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


class ProviderOut(BaseModel):
    id: int
    name: str
    base_url: str
    api_key: str
    api_key_masked: str
    provider_type: str
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
    max_error_rate: float | None
    first_token_timeout_sec: int | None
    active_requests: int = 0
    active_streams: int = 0
    current_qps: int = 0
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
    supports_vision: bool = False
    supports_tools: bool = False
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
