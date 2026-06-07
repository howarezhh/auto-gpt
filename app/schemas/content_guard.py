import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator


ContentGuardProbeKey = Literal["fixed_answer", "json", "sse", "refusal", "tools"]
ContentGuardEndpointPath = Literal["/chat/completions", "/responses"]
ContentGuardRuleMatchType = Literal["keyword_any", "regex", "unexpected_url"]
ContentGuardRuleRiskLevel = Literal["low", "medium", "high"]
ContentGuardRuleAction = Literal["allow", "record", "block"]
ContentGuardHighRiskStrategy = Literal["block", "switch_provider", "record_only", "safe_error"]
ContentGuardStreamMode = Literal["pass_through_scan", "buffer_300ms", "full_buffer"]


class ContentGuardSettingsUpdate(BaseModel):
    content_guard_enabled: bool = True
    content_guard_block_on_high_risk: bool = True
    content_guard_probe_interval_sec: int = Field(default=3600, ge=300)
    content_guard_max_scan_bytes: int = Field(default=16384, ge=1024)
    content_guard_stream_buffer_max_bytes: int = Field(default=16384, ge=1024)
    content_guard_low_trust_requires_buffer: bool = True
    content_guard_high_risk_strategy: ContentGuardHighRiskStrategy = "switch_provider"
    content_guard_max_detection_delay_ms: int = Field(default=300, ge=0, le=3000)
    content_guard_stream_mode: ContentGuardStreamMode = "buffer_300ms"
    content_guard_url_check_enabled: bool = True
    content_guard_url_allowlist_json: str = Field(default="", max_length=10000)
    content_guard_async_review_enabled: bool = True
    content_guard_high_risk_confidence_threshold: int = Field(default=85, ge=0, le=100)


class ContentGuardRulePayload(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=80)
    category: str = Field(..., min_length=1, max_length=64)
    enabled: bool = True
    match_type: ContentGuardRuleMatchType = "keyword_any"
    patterns: list[str] = Field(default_factory=list, max_length=50)
    risk_level: ContentGuardRuleRiskLevel = "medium"
    action: ContentGuardRuleAction = "record"
    score_delta: int = Field(default=-8, ge=-100, le=0)
    confidence: float = Field(default=0.7, ge=0, le=1)
    reason: str = Field(default="", max_length=200)

    @field_validator("id", "category")
    @classmethod
    def normalize_identifier(cls, value: str) -> str:
        normalized = re.sub(r"[^a-z0-9_]+", "_", value.strip().lower().replace("-", "_")).strip("_")
        if not normalized:
            raise ValueError("标识不能为空")
        return normalized

    @field_validator("name", "reason")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("patterns")
    @classmethod
    def normalize_patterns(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in value or []:
            text = str(item).strip()
            if text and text not in normalized:
                normalized.append(text[:500])
        if not normalized:
            raise ValueError("规则至少需要一个匹配项")
        return normalized


class ContentGuardRulesUpdate(BaseModel):
    rules: list[ContentGuardRulePayload] = Field(default_factory=list, max_length=200)

    @field_validator("rules")
    @classmethod
    def validate_unique_rule_ids(cls, value: list[ContentGuardRulePayload]) -> list[ContentGuardRulePayload]:
        seen: set[str] = set()
        for item in value:
            if item.id in seen:
                raise ValueError(f"规则标识重复：{item.id}")
            seen.add(item.id)
        return value


class ContentGuardTextInspectRequest(BaseModel):
    text: str = Field(..., min_length=1)
    endpoint_path: ContentGuardEndpointPath | None = None
    request_payload: dict | None = None
    max_scan_bytes: int = Field(default=16384, ge=1024)


class ContentGuardExternalTarget(BaseModel):
    base_url: str = Field(..., min_length=1)
    api_key: str = Field(..., min_length=1)
    model_name: str = Field(..., min_length=1)
    endpoint_path: ContentGuardEndpointPath = "/responses"

    @field_validator("base_url")
    @classmethod
    def normalize_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        if not normalized:
            raise ValueError("接口地址不能为空")
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("接口地址必须以 http:// 或 https:// 开头")
        return normalized

    @field_validator("api_key", "model_name")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("必填项不能为空")
        return normalized


class ContentGuardRunRequest(BaseModel):
    target_type: Literal["internal", "external"] = "internal"
    provider_id: int | None = None
    provider_model_id: int | None = None
    external: ContentGuardExternalTarget | None = None
    probe_keys: list[ContentGuardProbeKey] = Field(default_factory=lambda: ["fixed_answer", "json", "refusal"])
    persist_internal_result: bool = True

    @field_validator("probe_keys")
    @classmethod
    def normalize_probe_keys(cls, value: list[ContentGuardProbeKey]) -> list[ContentGuardProbeKey]:
        ordered: list[ContentGuardProbeKey] = []
        for item in value or []:
            if item not in ordered:
                ordered.append(item)
        return ordered or ["fixed_answer", "json", "refusal"]
