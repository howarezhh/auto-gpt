from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


RouteMode = Literal["manual", "failover", "weighted", "sticky"]


class ApiKeyProviderOut(BaseModel):
    id: int
    name: str
    enabled: bool
    health_status: str


class ApiKeyBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    raw_api_key: str | None = Field(default=None, min_length=24, max_length=128)
    remark: str | None = None
    enabled: bool = True
    expires_at: datetime | None = None
    token_limit_total: int | None = Field(default=None, ge=0)
    cost_limit_total: float | None = Field(default=None, ge=0)
    balance_amount: float | None = Field(default=None, ge=0)
    route_mode: RouteMode = "failover"
    default_provider_id: int | None = None
    owner_user_id: int | None = None
    manual_allow_fallback: bool = True
    allowed_provider_ids: list[int] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("remark")
    @classmethod
    def normalize_remark(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("raw_api_key")
    @classmethod
    def normalize_raw_api_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("allowed_provider_ids")
    @classmethod
    def normalize_allowed_provider_ids(cls, value: list[int]) -> list[int]:
        seen: set[int] = set()
        normalized: list[int] = []
        for item in value:
            if item in seen:
                continue
            seen.add(item)
            normalized.append(item)
        return normalized


class ApiKeyCreate(ApiKeyBase):
    pass


class ApiKeyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    raw_api_key: str | None = Field(default=None, min_length=24, max_length=128)
    remark: str | None = None
    enabled: bool | None = None
    expires_at: datetime | None = None
    token_limit_total: int | None = Field(default=None, ge=0)
    cost_limit_total: float | None = Field(default=None, ge=0)
    balance_amount: float | None = Field(default=None, ge=0)
    route_mode: RouteMode | None = None
    default_provider_id: int | None = None
    owner_user_id: int | None = None
    manual_allow_fallback: bool | None = None
    allowed_provider_ids: list[int] | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip()

    @field_validator("remark")
    @classmethod
    def normalize_remark(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("raw_api_key")
    @classmethod
    def normalize_raw_api_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("allowed_provider_ids")
    @classmethod
    def normalize_allowed_provider_ids(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        seen: set[int] = set()
        normalized: list[int] = []
        for item in value:
            if item in seen:
                continue
            seen.add(item)
            normalized.append(item)
        return normalized


class ApiKeyOut(BaseModel):
    id: int
    name: str
    remark: str | None
    enabled: bool
    status: str
    key_prefix: str
    key_masked: str
    raw_api_key: str | None
    has_stored_raw_key: bool
    expires_at: datetime | None
    token_limit_total: int | None
    prompt_tokens_used: int
    completion_tokens_used: int
    total_tokens_used: int
    remaining_tokens: int | None
    cost_limit_total: float | None
    total_cost_used: float
    balance_amount: float | None
    total_recharge_amount: float
    remaining_cost_quota: float | None
    route_mode: RouteMode
    default_provider_id: int | None
    owner_user_id: int | None
    owner_user_name: str | None
    manual_allow_fallback: bool
    allowed_provider_ids: list[int]
    allowed_providers: list[ApiKeyProviderOut]
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ApiKeyRecentUsageOut(BaseModel):
    recent_window_hours: int
    recent_requests: int
    recent_failed_requests: int
    recent_prompt_tokens: int
    recent_completion_tokens: int
    recent_total_tokens: int
    recent_total_cost: float = 0


class ApiKeyDetailOut(ApiKeyOut):
    recent_usage: ApiKeyRecentUsageOut


class ApiKeyCreateResponse(ApiKeyOut):
    raw_api_key: str


class ApiKeyStatsOut(BaseModel):
    api_client_key_id: int
    total_requests: int
    success_requests: int
    failed_requests: int
    avg_latency_ms: float | None
    recent_window_hours: int
    recent_requests: int
    recent_failed_requests: int
    recent_prompt_tokens: int
    recent_completion_tokens: int
    recent_total_tokens: int
    recent_total_cost: float = 0


class ApiKeySummaryOut(BaseModel):
    total_keys: int
    enabled_keys: int
    disabled_keys: int
    expired_keys: int
    quota_exhausted_keys: int
    unbound_keys: int
    total_requests: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    total_cost_used: float = 0
    total_balance_amount: float = 0
    total_recharge_amount: float = 0


class ApiKeyListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[ApiKeyOut]


class ApiKeyBatchActionIn(BaseModel):
    api_key_ids: list[int] = Field(default_factory=list)

    @field_validator("api_key_ids")
    @classmethod
    def normalize_api_key_ids(cls, value: list[int]) -> list[int]:
        seen: set[int] = set()
        normalized: list[int] = []
        for item in value:
            if item in seen:
                continue
            seen.add(item)
            normalized.append(item)
        if not normalized:
            raise ValueError("至少选择一个 API Key")
        return normalized


class ApiKeyBatchActionResultOut(BaseModel):
    requested_count: int
    affected_count: int
    api_key_ids: list[int] = Field(default_factory=list)


class ApiKeyBatchProviderUpdateIn(ApiKeyBatchActionIn):
    route_mode: RouteMode
    default_provider_id: int | None = None
    manual_allow_fallback: bool = True
    allowed_provider_ids: list[int] = Field(default_factory=list)

    @field_validator("allowed_provider_ids")
    @classmethod
    def normalize_batch_allowed_provider_ids(cls, value: list[int]) -> list[int]:
        seen: set[int] = set()
        normalized: list[int] = []
        for item in value:
            if item in seen:
                continue
            seen.add(item)
            normalized.append(item)
        return normalized


class ApiKeyModelDistributionItemOut(BaseModel):
    model_name: str
    total_requests: int
    failed_requests: int
    total_tokens: int
    total_cost: float = 0
    last_requested_at: datetime | None


class ApiKeyRecentErrorOut(BaseModel):
    id: int
    created_at: datetime
    request_path: str | None
    model_name: str | None
    provider_name: str | None
    status_code: int | None
    api_client_auth_result: str | None
    message: str | None


class ApiKeyAnalyticsOut(BaseModel):
    api_client_key_id: int
    model_distribution: list[ApiKeyModelDistributionItemOut]
    recent_errors: list[ApiKeyRecentErrorOut]


class ApiKeyBillingRecordOut(BaseModel):
    id: int
    api_client_key_id: int
    request_log_id: int | None
    record_type: str
    amount: float
    balance_after: float | None
    provider_id: int | None
    provider_name: str | None
    model_name: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    unit_input_price_per_1k: float | None
    unit_output_price_per_1k: float | None
    remark: str | None
    created_at: datetime


class ApiKeyBillingSummaryOut(BaseModel):
    api_client_key_id: int
    balance_amount: float | None
    total_cost_used: float
    total_recharge_amount: float
    cost_limit_total: float | None
    remaining_cost_quota: float | None
    recent_billed_cost: float = 0
    total_billing_records: int = 0
    items: list[ApiKeyBillingRecordOut] = Field(default_factory=list)


class ApiKeyBalanceAdjustmentIn(BaseModel):
    amount: float
    remark: str | None = None

    @field_validator("remark")
    @classmethod
    def normalize_adjustment_remark(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None
