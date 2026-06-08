from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


IpManagementScope = Literal["external_v1", "internal_api", "user_pages", "all"]
IpRuleMatchType = Literal["exact_ip", "cidr", "range"]
IpRuleAction = Literal["allow", "record", "rate_limit", "block"]
TrustedHeaderKey = Literal["forwarded", "x_forwarded_for", "cf_connecting_ip"]


class IpManagementSettingsUpdate(BaseModel):
    enabled: bool = False
    observe_only_enabled: bool = False
    trusted_proxy_resolution_enabled: bool = False
    trusted_proxy_cidrs: list[str] = Field(default_factory=list, max_length=200)
    trusted_header_order: list[TrustedHeaderKey] = Field(default_factory=list, max_length=3)
    apply_external_v1_enabled: bool = False
    apply_internal_api_enabled: bool = False
    apply_user_pages_enabled: bool = False
    rule_engine_enabled: bool = False
    block_action_enabled: bool = False
    rate_limit_enabled: bool = False
    event_logging_enabled: bool = False
    event_sample_rate: int = Field(default=100, ge=0, le=100)
    event_retention_days: int = Field(default=30, ge=1, le=3650)
    store_raw_headers_enabled: bool = False
    ip_masking_enabled: bool = False
    fail_open_enabled: bool = True

    @field_validator("trusted_proxy_cidrs")
    @classmethod
    def normalize_cidrs(cls, value: list[str]) -> list[str]:
        return [str(item).strip() for item in value if str(item).strip()]

    @field_validator("trusted_header_order")
    @classmethod
    def dedupe_headers(cls, value: list[TrustedHeaderKey]) -> list[TrustedHeaderKey]:
        seen: set[str] = set()
        ordered: list[TrustedHeaderKey] = []
        for item in value:
            if item in seen:
                continue
            seen.add(item)
            ordered.append(item)
        return ordered


class IpAccessRulePayload(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = False
    priority: int = Field(default=100, ge=1, le=100000)
    scope: IpManagementScope = "external_v1"
    match_type: IpRuleMatchType = "cidr"
    match_value: str = Field(min_length=1, max_length=200)
    action: IpRuleAction = "record"
    rate_qps_limit: int = Field(default=0, ge=0, le=100000)
    rate_rpm_limit: int = Field(default=0, ge=0, le=1000000)
    expires_at: datetime | None = None
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("name", "match_value", "reason")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip()

    @model_validator(mode="after")
    def validate_limits(self) -> "IpAccessRulePayload":
        if self.action == "rate_limit" and self.rate_qps_limit <= 0 and self.rate_rpm_limit <= 0:
            raise ValueError("限流规则必须至少配置 QPS 或 RPM 上限")
        return self


class IpAccessRuleUpdate(IpAccessRulePayload):
    pass


class IpAccessRuleCreate(IpAccessRulePayload):
    pass


class IpResolutionTestRequest(BaseModel):
    direct_client_ip: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    trusted_proxy_cidrs: list[str] = Field(default_factory=list, max_length=200)
    trusted_header_order: list[TrustedHeaderKey] = Field(default_factory=list, max_length=3)
    trusted_proxy_resolution_enabled: bool = True


class IpRuleTestRequest(BaseModel):
    ip: str
    scope: IpManagementScope = "external_v1"
    request_path: str = "/v1/chat/completions"
    http_method: str = "POST"

    @field_validator("ip", "request_path", "http_method")
    @classmethod
    def strip_required(cls, value: str) -> str:
        return value.strip()
