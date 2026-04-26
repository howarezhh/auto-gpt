from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.schemas.api_key import RouteMode


class ApiKeyPolicyTemplateBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    remark: str | None = None
    enabled: bool = True
    route_mode: RouteMode = "failover"
    default_provider_id: int | None = None
    manual_allow_fallback: bool = True
    token_limit_total: int | None = Field(default=None, ge=0)
    cost_limit_total: float | None = Field(default=None, ge=0)
    expires_in_days: int | None = Field(default=None, ge=0)
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


class ApiKeyPolicyTemplateCreate(ApiKeyPolicyTemplateBase):
    pass


class ApiKeyPolicyTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    remark: str | None = None
    enabled: bool | None = None
    route_mode: RouteMode | None = None
    default_provider_id: int | None = None
    manual_allow_fallback: bool | None = None
    token_limit_total: int | None = Field(default=None, ge=0)
    cost_limit_total: float | None = Field(default=None, ge=0)
    expires_in_days: int | None = Field(default=None, ge=0)
    allowed_provider_ids: list[int] | None = None


class ApiKeyPolicyTemplateOut(ApiKeyPolicyTemplateBase):
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
