from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class ModelProviderBindingBase(BaseModel):
    provider_id: int
    bound: bool = True
    enabled: bool = True
    priority: int = 100
    weight: int = 100
    price_multiplier: float = Field(default=1.0, gt=0)


class ModelProviderBindingIn(ModelProviderBindingBase):
    pass


class ModelProviderBindingOut(ModelProviderBindingBase):
    provider_name: str
    provider_enabled: bool
    provider_health_status: str
    provider_circuit_state: str | None = None
    provider_maintenance_mode_enabled: bool = False
    model_health_status: str | None = None
    model_circuit_state: str | None = None
    effective_input_price_per_1k: float | None = None
    effective_output_price_per_1k: float | None = None
    effective_cache_price_per_1k: float | None = None
    direct_input_price_per_1k: float | None = None
    direct_output_price_per_1k: float | None = None
    direct_cache_price_per_1k: float | None = None


class ModelCatalogBase(BaseModel):
    model_name: str = Field(..., min_length=1)
    display_name: str | None = None
    enabled: bool = True
    supports_stream: bool = True
    supports_vision: bool = False
    supports_tools: bool = False
    supports_chat_completions: bool = True
    supports_responses: bool = True
    context_window_tokens: int | None = Field(default=None, ge=1)
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    input_price_per_1k: float | None = Field(default=None, ge=0)
    output_price_per_1k: float | None = Field(default=None, ge=0)
    cache_price_per_1k: float | None = Field(default=None, ge=0)
    speed_label: str | None = Field(default=None, max_length=50)
    remark: str | None = None

    @field_validator("model_name")
    @classmethod
    def normalize_model_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("display_name", "speed_label", "remark")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class ModelCatalogCreate(ModelCatalogBase):
    provider_bindings: list[ModelProviderBindingIn] = Field(default_factory=list)


class ModelCatalogUpdate(BaseModel):
    display_name: str | None = None
    enabled: bool | None = None
    supports_stream: bool | None = None
    supports_vision: bool | None = None
    supports_tools: bool | None = None
    supports_chat_completions: bool | None = None
    supports_responses: bool | None = None
    context_window_tokens: int | None = Field(default=None, ge=1)
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    input_price_per_1k: float | None = Field(default=None, ge=0)
    output_price_per_1k: float | None = Field(default=None, ge=0)
    cache_price_per_1k: float | None = Field(default=None, ge=0)
    speed_label: str | None = Field(default=None, max_length=50)
    remark: str | None = None
    provider_bindings: list[ModelProviderBindingIn] | None = None

    @field_validator("display_name", "speed_label", "remark")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class ModelCatalogBatchContextWindowUpdate(BaseModel):
    model_names: list[str] = Field(..., min_length=1)
    context_window_tokens: int | None = Field(default=None, ge=1)

    @field_validator("model_names")
    @classmethod
    def normalize_model_names(cls, value: list[str]) -> list[str]:
        names = [item.strip() for item in value if isinstance(item, str) and item.strip()]
        if not names:
            raise ValueError("model_names 不能为空")
        return list(dict.fromkeys(names))


class ModelCatalogOut(ModelCatalogBase):
    provider_count: int = 0
    bound_provider_count: int = 0
    available_provider_count: int = 0
    enabled_provider_count: int = 0
    lowest_input_price_per_1k: float | None = None
    lowest_output_price_per_1k: float | None = None
    lowest_cache_price_per_1k: float | None = None
    avg_price_multiplier: float | None = None
    avg_bound_price_multiplier: float | None = None
    avg_routable_price_multiplier: float | None = None
    bound_price_multiplier_count: int = 0
    routable_price_multiplier_count: int = 0
    min_bound_price_multiplier: float | None = None
    max_bound_price_multiplier: float | None = None
    available_provider_names: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ModelCatalogSummaryOut(BaseModel):
    total: int = 0
    enabled: int = 0
    bound_providers: int = 0
    available_providers: int = 0
    enabled_providers: int = 0


class ModelCatalogPageOut(BaseModel):
    items: list[ModelCatalogOut] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
    total_pages: int = 1
    summary: ModelCatalogSummaryOut = Field(default_factory=ModelCatalogSummaryOut)


class ModelCatalogDetailOut(ModelCatalogOut):
    provider_bindings: list[ModelProviderBindingOut] = Field(default_factory=list)


class UserModelOut(BaseModel):
    model_name: str
    display_name: str | None = None
    speed_label: str | None = None
    remark: str | None = None
    supports_stream: bool = True
    supports_vision: bool = False
    supports_tools: bool = False
    supports_chat_completions: bool = True
    supports_responses: bool = True
    context_window_tokens: int | None = Field(default=None, ge=1)
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    input_price_per_1k: float | None = None
    output_price_per_1k: float | None = None
    cache_price_per_1k: float | None = None
    available_provider_names: list[str] = Field(default_factory=list)
    enabled_provider_count: int = 0
