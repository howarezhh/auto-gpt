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
    tenant_name: str | None = Field(default=None, max_length=100)
    project_name: str | None = Field(default=None, max_length=100)
    app_name: str | None = Field(default=None, max_length=100)
    environment_name: str | None = Field(default=None, max_length=100)
    enabled: bool = True
    expires_at: datetime | None = None
    token_limit_total: int | None = Field(default=None, ge=0)
    request_limit_daily: int | None = Field(default=None, ge=0)
    token_limit_daily: int | None = Field(default=None, ge=0)
    cost_limit_daily: float | None = Field(default=None, ge=0)
    qps_limit: int | None = Field(default=None, ge=0)
    rpm_limit: int | None = Field(default=None, ge=0)
    tpm_limit: int | None = Field(default=None, ge=0)
    cost_limit_total: float | None = Field(default=None, ge=0)
    balance_amount: float | None = Field(default=None, ge=0)
    route_mode: RouteMode = "failover"
    default_provider_id: int | None = None
    owner_user_id: int | None = None
    manual_allow_fallback: bool = True
    allowed_provider_ids: list[int] = Field(default_factory=list)
    allowed_model_names: list[str] = Field(default_factory=list)
    allowed_endpoint_paths: list[str] = Field(default_factory=list)
    allowed_source_ips: list[str] = Field(default_factory=list)
    preferred_provider_ids: list[int] = Field(default_factory=list)
    preferred_region_tags: list[str] = Field(default_factory=list)
    max_candidate_count: int | None = Field(default=None, ge=1, le=20)
    latency_bias: int = Field(default=1, ge=0, le=10)
    success_rate_bias: int = Field(default=1, ge=0, le=10)
    cost_bias: int = Field(default=0, ge=0, le=10)

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
        return cls._normalize_int_list(value)

    @staticmethod
    def _normalize_int_list(value: list[int]) -> list[int]:
        seen: set[int] = set()
        normalized: list[int] = []
        for item in value:
            if item in seen:
                continue
            seen.add(item)
            normalized.append(item)
        return normalized

    @staticmethod
    def _normalize_str_list(value: list[str]) -> list[str]:
        seen: set[str] = set()
        normalized: list[str] = []
        for raw in value:
            item = str(raw).strip()
            if not item or item in seen:
                continue
            seen.add(item)
            normalized.append(item)
        return normalized

    @field_validator("tenant_name", "project_name", "app_name", "environment_name")
    @classmethod
    def normalize_scope_names(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("allowed_model_names", "allowed_endpoint_paths", "allowed_source_ips", "preferred_region_tags")
    @classmethod
    def normalize_string_lists(cls, value: list[str]) -> list[str]:
        return cls._normalize_str_list(value)

    @field_validator("preferred_provider_ids")
    @classmethod
    def normalize_preferred_provider_ids(cls, value: list[int]) -> list[int]:
        return cls._normalize_int_list(value)


class ApiKeyCreate(ApiKeyBase):
    pass


class ApiKeyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    raw_api_key: str | None = Field(default=None, min_length=24, max_length=128)
    remark: str | None = None
    tenant_name: str | None = Field(default=None, max_length=100)
    project_name: str | None = Field(default=None, max_length=100)
    app_name: str | None = Field(default=None, max_length=100)
    environment_name: str | None = Field(default=None, max_length=100)
    enabled: bool | None = None
    expires_at: datetime | None = None
    token_limit_total: int | None = Field(default=None, ge=0)
    request_limit_daily: int | None = Field(default=None, ge=0)
    token_limit_daily: int | None = Field(default=None, ge=0)
    cost_limit_daily: float | None = Field(default=None, ge=0)
    qps_limit: int | None = Field(default=None, ge=0)
    rpm_limit: int | None = Field(default=None, ge=0)
    tpm_limit: int | None = Field(default=None, ge=0)
    cost_limit_total: float | None = Field(default=None, ge=0)
    balance_amount: float | None = Field(default=None, ge=0)
    route_mode: RouteMode | None = None
    default_provider_id: int | None = None
    owner_user_id: int | None = None
    manual_allow_fallback: bool | None = None
    allowed_provider_ids: list[int] | None = None
    allowed_model_names: list[str] | None = None
    allowed_endpoint_paths: list[str] | None = None
    allowed_source_ips: list[str] | None = None
    preferred_provider_ids: list[int] | None = None
    preferred_region_tags: list[str] | None = None
    max_candidate_count: int | None = Field(default=None, ge=1, le=20)
    latency_bias: int | None = Field(default=None, ge=0, le=10)
    success_rate_bias: int | None = Field(default=None, ge=0, le=10)
    cost_bias: int | None = Field(default=None, ge=0, le=10)

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

    @field_validator("tenant_name", "project_name", "app_name", "environment_name")
    @classmethod
    def normalize_scope_names(cls, value: str | None) -> str | None:
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
        return ApiKeyBase._normalize_int_list(value)

    @field_validator("allowed_model_names", "allowed_endpoint_paths", "allowed_source_ips", "preferred_region_tags")
    @classmethod
    def normalize_string_lists(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return ApiKeyBase._normalize_str_list(value)

    @field_validator("preferred_provider_ids")
    @classmethod
    def normalize_preferred_provider_ids(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        return ApiKeyBase._normalize_int_list(value)


class ApiKeyOut(BaseModel):
    id: int
    name: str
    remark: str | None
    tenant_name: str | None
    project_name: str | None
    app_name: str | None
    environment_name: str | None
    enabled: bool
    status: str
    key_prefix: str
    key_masked: str
    raw_api_key: str | None
    has_stored_raw_key: bool
    expires_at: datetime | None
    token_limit_total: int | None
    request_limit_daily: int | None
    token_limit_daily: int | None
    cost_limit_daily: float | None
    qps_limit: int | None
    rpm_limit: int | None
    tpm_limit: int | None
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
    allowed_model_names: list[str]
    allowed_endpoint_paths: list[str]
    allowed_source_ips: list[str]
    preferred_provider_ids: list[int]
    preferred_region_tags: list[str]
    max_candidate_count: int | None
    latency_bias: int
    success_rate_bias: int
    cost_bias: int
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


class ApiKeyBatchRotateItemOut(BaseModel):
    id: int
    name: str
    raw_api_key: str
    key_masked: str


class ApiKeyBatchRotateResultOut(ApiKeyBatchActionResultOut):
    items: list[ApiKeyBatchRotateItemOut] = Field(default_factory=list)


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


class ApiKeyBatchTemplateApplyIn(ApiKeyBatchActionIn):
    template_id: int = Field(..., ge=1)


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


class ApiKeyCostInsightItemOut(BaseModel):
    dimension_value: str
    total_requests: int
    total_tokens: int
    total_cost: float
    avg_latency_ms: float | None = None


class ApiKeyCostInsightResponseOut(BaseModel):
    group_by: str
    window_days: int
    items: list[ApiKeyCostInsightItemOut] = Field(default_factory=list)


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
