from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class ProviderModelConfigBase(BaseModel):
    model_name: str = Field(..., min_length=1)
    enabled: bool = True
    priority: int = 100
    weight: int = 100
    supports_stream: bool = True
    supports_vision: bool = False
    input_price_per_1k: float | None = Field(default=None, ge=0)
    output_price_per_1k: float | None = Field(default=None, ge=0)

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


class ProviderModelConfigUpdate(BaseModel):
    enabled: bool | None = None
    priority: int | None = None
    weight: int | None = None
    supports_stream: bool | None = None
    supports_vision: bool | None = None
    input_price_per_1k: float | None = Field(default=None, ge=0)
    output_price_per_1k: float | None = Field(default=None, ge=0)


class ProviderBatchConnectivityTestRequest(BaseModel):
    provider_ids: list[int] = Field(default_factory=list)


class ProviderBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    base_url: str = Field(..., min_length=1)
    api_key: str = Field(..., min_length=1)
    provider_type: str = "openai_compatible"
    enabled: bool = True
    priority: int = 100
    weight: int = 100
    timeout_ms: int = 30000
    max_retries: int = 1
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
    enabled: bool | None = None
    priority: int | None = None
    weight: int | None = None
    timeout_ms: int | None = None
    max_retries: int | None = None
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
    api_key_masked: str
    provider_type: str
    enabled: bool
    priority: int
    weight: int
    timeout_ms: int
    max_retries: int
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
    remark: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
