from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.schemas.provider import normalize_model_group


class ModelProviderBindingBase(BaseModel):
    provider_id: int
    bound: bool = True
    enabled: bool = True
    priority: int = 100
    price_multiplier: float = Field(default=1.0, gt=0)


class ModelProviderBindingIn(ModelProviderBindingBase):
    pass


class ModelProviderBindingOut(ModelProviderBindingBase):
    provider_name: str
    provider_enabled: bool
    provider_health_status: str
    provider_circuit_state: str | None = None
    provider_maintenance_mode_enabled: bool = False
    provider_model_id: int | None = None
    model_health_status: str | None = None
    model_circuit_state: str | None = None
    supports_stream: bool = False
    supports_tools: bool = False
    supports_vision: bool = False
    supports_image_generation: bool = False
    effective_input_price_per_1k: Decimal | None = None
    effective_output_price_per_1k: Decimal | None = None
    effective_cache_price_per_1k: Decimal | None = None
    effective_cache_write_price_per_1k: Decimal | None = None
    direct_input_price_per_1k: Decimal | None = None
    direct_output_price_per_1k: Decimal | None = None
    direct_cache_price_per_1k: Decimal | None = None
    direct_cache_write_price_per_1k: Decimal | None = None
    trust_status: str = "unknown"
    trust_status_label: str = "未检测"
    trust_status_reason: str | None = None
    content_integrity_status: str = "unknown"
    content_probe_last_passed_at: datetime | None = None
    content_probe_last_failed_at: datetime | None = None


class ModelCatalogBase(BaseModel):
    model_name: str = Field(..., min_length=1)
    display_name: str | None = None
    model_group: str | None = None
    enabled: bool = True
    supports_stream: bool = True
    supports_vision: bool = True
    supports_tools: bool = True
    supports_chat_completions: bool = True
    supports_responses: bool = True
    context_window_tokens: int | None = Field(default=None, ge=1)
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    pricing_mode: str = Field(default="fixed", max_length=20)
    pricing_json: dict | None = None
    input_price_per_1k: Decimal | None = Field(default=None, ge=0)
    output_price_per_1k: Decimal | None = Field(default=None, ge=0)
    cache_price_per_1k: Decimal | None = Field(default=None, ge=0)
    cache_write_price_per_1k: Decimal | None = Field(default=None, ge=0)
    source_currency: str = "USD"
    billing_currency: str = "USD"
    source_input_price_per_1k: Decimal | None = Field(default=None, ge=0)
    source_output_price_per_1k: Decimal | None = Field(default=None, ge=0)
    source_cache_price_per_1k: Decimal | None = Field(default=None, ge=0)
    source_cache_write_price_per_1k: Decimal | None = Field(default=None, ge=0)
    exchange_rate_to_billing_currency: Decimal | None = Field(default=None, ge=0)
    exchange_rate_source: str | None = None
    exchange_rate_at: datetime | None = None
    exchange_rate_version: str | None = None
    rounding_strategy: str = "ROUND_HALF_UP"
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

    @field_validator("model_group")
    @classmethod
    def normalize_group(cls, value: str | None) -> str | None:
        if value is None or str(value).strip() == "":
            return None
        return normalize_model_group(value)


class ModelCatalogCreate(ModelCatalogBase):
    provider_bindings: list[ModelProviderBindingIn] = Field(default_factory=list)


class ModelCatalogUpdate(BaseModel):
    display_name: str | None = None
    model_group: str | None = None
    enabled: bool | None = None
    supports_stream: bool | None = None
    supports_vision: bool | None = None
    supports_tools: bool | None = None
    supports_chat_completions: bool | None = None
    supports_responses: bool | None = None
    context_window_tokens: int | None = Field(default=None, ge=1)
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    pricing_mode: str | None = Field(default=None, max_length=20)
    pricing_json: dict | None = None
    input_price_per_1k: Decimal | None = Field(default=None, ge=0)
    output_price_per_1k: Decimal | None = Field(default=None, ge=0)
    cache_price_per_1k: Decimal | None = Field(default=None, ge=0)
    cache_write_price_per_1k: Decimal | None = Field(default=None, ge=0)
    source_currency: str | None = None
    billing_currency: str | None = None
    source_input_price_per_1k: Decimal | None = Field(default=None, ge=0)
    source_output_price_per_1k: Decimal | None = Field(default=None, ge=0)
    source_cache_price_per_1k: Decimal | None = Field(default=None, ge=0)
    source_cache_write_price_per_1k: Decimal | None = Field(default=None, ge=0)
    exchange_rate_to_billing_currency: Decimal | None = Field(default=None, ge=0)
    exchange_rate_source: str | None = None
    exchange_rate_at: datetime | None = None
    exchange_rate_version: str | None = None
    rounding_strategy: str | None = None
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

    @field_validator("model_group")
    @classmethod
    def normalize_group(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_model_group(value)


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


class ModelCatalogBatchImportRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=200000)
    dry_run: bool = True

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        return value.strip()


class ModelCatalogBatchImportItemOut(BaseModel):
    index: int
    model_name: str | None = None
    display_name: str | None = None
    valid: bool = False
    created: bool = False
    errors: list[str] = Field(default_factory=list)
    model: dict[str, Any] | None = None


class ModelCatalogBatchImportResponse(BaseModel):
    total: int
    valid_count: int
    created_count: int = 0
    failed_count: int = 0
    dry_run: bool
    template: str | None = None
    items: list[ModelCatalogBatchImportItemOut]


class ModelCatalogOut(ModelCatalogBase):
    supports_image_generation: bool = False
    provider_count: int = 0
    bound_provider_count: int = 0
    available_provider_count: int = 0
    enabled_provider_count: int = 0
    health_status: str = "unhealthy"
    health_reason: str | None = None
    healthy_provider_count: int = 0
    unhealthy_provider_count: int = 0
    lowest_input_price_per_1k: Decimal | None = None
    lowest_output_price_per_1k: Decimal | None = None
    lowest_cache_price_per_1k: Decimal | None = None
    lowest_cache_write_price_per_1k: Decimal | None = None
    avg_price_multiplier: float | None = None
    avg_bound_price_multiplier: float | None = None
    avg_routable_price_multiplier: float | None = None
    bound_price_multiplier_count: int = 0
    routable_price_multiplier_count: int = 0
    min_bound_price_multiplier: float | None = None
    max_bound_price_multiplier: float | None = None
    available_provider_names: list[str] = Field(default_factory=list)
    provider_bindings: list[ModelProviderBindingOut] = Field(default_factory=list)
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


class ModelCatalogOptionOut(BaseModel):
    model_name: str
    display_name: str | None = None
    model_group: str = "unknown"
    model_group_label: str = "未知分组"
    enabled: bool = True
    supports_stream: bool = True
    supports_vision: bool = True
    supports_tools: bool = True
    supports_image_generation: bool = False
    supports_chat_completions: bool = True
    supports_responses: bool = True
    context_window_tokens: int | None = Field(default=None, ge=1)
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    bound_provider_count: int = 0
    available_provider_count: int = 0
    enabled_provider_count: int = 0
    provider_bindings: list[ModelProviderBindingOut] = Field(default_factory=list)


class UserModelOut(BaseModel):
    model_name: str
    display_name: str | None = None
    model_group: str = "unknown"
    model_group_label: str = "未知分组"
    speed_label: str | None = None
    remark: str | None = None
    supports_stream: bool = True
    supports_vision: bool = True
    supports_tools: bool = True
    supports_image_generation: bool = False
    supports_chat_completions: bool = True
    supports_responses: bool = True
    context_window_tokens: int | None = Field(default=None, ge=1)
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    pricing_mode: str = "fixed"
    pricing_json: dict | None = None
    input_price_per_1k: Decimal | None = None
    output_price_per_1k: Decimal | None = None
    cache_price_per_1k: Decimal | None = None
    cache_write_price_per_1k: Decimal | None = None
    source_currency: str | None = None
    billing_currency: str | None = None
    source_input_price_per_1k: Decimal | None = None
    source_output_price_per_1k: Decimal | None = None
    source_cache_price_per_1k: Decimal | None = None
    source_cache_write_price_per_1k: Decimal | None = None
    exchange_rate_to_billing_currency: Decimal | None = None
    exchange_rate_source: str | None = None
    exchange_rate_at: datetime | None = None
    exchange_rate_version: str | None = None
    available_provider_names: list[str] = Field(default_factory=list)
    enabled_provider_count: int = 0
