import re
import ipaddress
import json
import socket
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

from app.utils.content_guard_config import (
    CONTENT_GUARD_MAX_SCAN_BYTES_LIMIT,
    CONTENT_GUARD_PROBE_INTERVAL_MAX_SECONDS,
    CONTENT_GUARD_PROBE_PROTOCOL_TYPES,
    CONTENT_GUARD_RULE_ACTIONS,
    CONTENT_GUARD_RULE_MATCH_TYPES,
    CONTENT_GUARD_RULE_RISK_LEVELS,
    CONTENT_GUARD_STREAM_BUFFER_MAX_BYTES_LIMIT,
    content_guard_default,
    validate_content_guard_settings,
)


ContentGuardProbeKey = Literal["fixed_answer", "pollution_rules", "json", "sse"]
ContentGuardEndpointPath = Literal["/chat/completions", "/responses"]
ContentGuardProbeProtocolType = Literal["chat_completions", "responses"]
ContentGuardRuleMatchType = Literal["keyword_any", "regex", "unexpected_url"]
ContentGuardRuleRiskLevel = Literal["low", "medium", "high"]
ContentGuardRuleAction = Literal["allow", "record", "block"]
ContentGuardHighRiskStrategy = Literal["block", "switch_provider", "record_only", "safe_error"]
ContentGuardStreamMode = Literal["pass_through_scan", "buffer_300ms", "full_buffer"]


class ContentGuardSettingsUpdate(BaseModel):
    content_guard_enabled: bool = content_guard_default("content_guard_enabled")
    content_guard_precheck_auto_enabled: bool = content_guard_default("content_guard_precheck_auto_enabled")
    content_guard_block_on_high_risk: bool = content_guard_default("content_guard_block_on_high_risk")
    content_guard_json_probe_enabled: bool = content_guard_default("content_guard_json_probe_enabled")
    content_guard_probe_protocol_type: ContentGuardProbeProtocolType = content_guard_default("content_guard_probe_protocol_type")
    content_guard_probe_interval_sec: int = Field(
        default=content_guard_default("content_guard_probe_interval_sec"),
        ge=300,
        le=CONTENT_GUARD_PROBE_INTERVAL_MAX_SECONDS,
    )
    content_guard_max_scan_bytes: int = Field(default=content_guard_default("content_guard_max_scan_bytes"), ge=1024, le=CONTENT_GUARD_MAX_SCAN_BYTES_LIMIT)
    content_guard_stream_buffer_max_bytes: int = Field(default=content_guard_default("content_guard_stream_buffer_max_bytes"), ge=1024, le=CONTENT_GUARD_STREAM_BUFFER_MAX_BYTES_LIMIT)
    content_guard_low_trust_requires_buffer: bool = content_guard_default("content_guard_low_trust_requires_buffer")
    content_guard_high_risk_strategy: ContentGuardHighRiskStrategy = content_guard_default("content_guard_high_risk_strategy")
    content_guard_max_detection_delay_ms: int = Field(default=content_guard_default("content_guard_max_detection_delay_ms"), ge=0, le=500)
    content_guard_stream_mode: ContentGuardStreamMode = content_guard_default("content_guard_stream_mode")
    content_guard_url_check_enabled: bool = content_guard_default("content_guard_url_check_enabled")
    content_guard_url_allowlist_json: str = Field(default=content_guard_default("content_guard_url_allowlist_json"), max_length=10000)
    content_guard_async_review_enabled: bool = content_guard_default("content_guard_async_review_enabled")
    content_guard_high_risk_confidence_threshold: int = Field(
        default=content_guard_default("content_guard_high_risk_confidence_threshold"),
        ge=0,
        le=100,
    )
    content_guard_enhanced_detection_enabled: bool = content_guard_default("content_guard_enhanced_detection_enabled")
    content_guard_enhanced_illegal_enabled: bool = content_guard_default("content_guard_enhanced_illegal_enabled")
    content_guard_enhanced_ad_enabled: bool = content_guard_default("content_guard_enhanced_ad_enabled")
    content_guard_enhanced_custom_enabled: bool = content_guard_default("content_guard_enhanced_custom_enabled")
    content_guard_enhanced_obfuscation_enabled: bool = content_guard_default("content_guard_enhanced_obfuscation_enabled")
    content_guard_enhanced_threshold: int = Field(
        default=content_guard_default("content_guard_enhanced_threshold"),
        ge=0,
        le=100,
    )
    content_guard_enhanced_context_window_chars: int = Field(
        default=content_guard_default("content_guard_enhanced_context_window_chars"),
        ge=24,
        le=512,
    )

    @model_validator(mode="after")
    def validate_cross_fields(self) -> "ContentGuardSettingsUpdate":
        validate_content_guard_settings(self.model_dump())
        return self

    @field_validator("content_guard_probe_protocol_type")
    @classmethod
    def validate_probe_protocol_type(cls, value: str) -> str:
        if value not in CONTENT_GUARD_PROBE_PROTOCOL_TYPES:
            raise ValueError("探针协议无效")
        return value

    @field_validator("content_guard_url_allowlist_json")
    @classmethod
    def validate_url_allowlist_json(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            return "[]"
        try:
            parsed = json.loads(normalized)
        except json.JSONDecodeError as exc:
            raise ValueError("URL 白名单必须是 JSON 字符串数组") from exc
        if not isinstance(parsed, list):
            raise ValueError("URL 白名单必须是 JSON 字符串数组")
        cleaned: list[str] = []
        for item in parsed:
            if not isinstance(item, str):
                raise ValueError("URL 白名单只允许字符串域名")
            domain = item.strip().lower().rstrip(".")
            if "://" in domain:
                parsed_domain = urlparse(domain).hostname or ""
                domain = parsed_domain.strip().lower().rstrip(".")
            domain = domain.removeprefix("www.")
            if not domain or len(domain) > 253 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+", domain):
                raise ValueError(f"URL 白名单域名无效：{item}")
            if domain not in cleaned:
                cleaned.append(domain)
        return json.dumps(cleaned, ensure_ascii=False)


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

    @field_validator("match_type")
    @classmethod
    def validate_match_type(cls, value: str) -> str:
        if value not in CONTENT_GUARD_RULE_MATCH_TYPES:
            raise ValueError("规则匹配类型无效")
        return value

    @field_validator("risk_level")
    @classmethod
    def validate_risk_level(cls, value: str) -> str:
        if value not in CONTENT_GUARD_RULE_RISK_LEVELS:
            raise ValueError("规则风险等级无效")
        return value

    @field_validator("action")
    @classmethod
    def validate_action(cls, value: str) -> str:
        if value not in CONTENT_GUARD_RULE_ACTIONS:
            raise ValueError("规则动作无效")
        return value

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

    @model_validator(mode="after")
    def validate_rule_semantics(self) -> "ContentGuardRulePayload":
        if self.action == "allow" and self.score_delta != 0:
            raise ValueError("放行动作的扣分必须为 0")
        if self.action == "block" and self.risk_level != "high":
            raise ValueError("阻断动作必须使用高风险等级")
        if self.match_type == "regex":
            for pattern in self.patterns:
                try:
                    from app.services.content_guard_rule_service import ContentGuardRuleService

                    ContentGuardRuleService.validate_regex_pattern(pattern)
                    re.compile(pattern)
                except (re.error, ValueError) as exc:
                    raise ValueError(f"正则规则无效：{pattern}") from exc
        return self


class ContentGuardRulesUpdate(BaseModel):
    rules: list[ContentGuardRulePayload] = Field(default_factory=list, min_length=1, max_length=200)

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
    text: str = Field(..., min_length=1, max_length=CONTENT_GUARD_MAX_SCAN_BYTES_LIMIT)
    endpoint_path: ContentGuardEndpointPath | None = None
    request_payload: dict | None = None
    max_scan_bytes: int = Field(default=16384, ge=1024, le=CONTENT_GUARD_MAX_SCAN_BYTES_LIMIT)

    @field_validator("request_payload")
    @classmethod
    def validate_request_payload_size(cls, value: dict | None) -> dict | None:
        if value is None:
            return None
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > CONTENT_GUARD_MAX_SCAN_BYTES_LIMIT:
            raise ValueError("请求上下文不能超过内容防护最大扫描字节数")

        def walk(item: Any, *, depth: int = 0) -> int:
            if depth > 12:
                raise ValueError("请求上下文嵌套层级不能超过 12 层")
            if isinstance(item, dict):
                total = 1
                for nested in item.values():
                    total += walk(nested, depth=depth + 1)
                    if total > 2000:
                        raise ValueError("请求上下文结构节点不能超过 2000 个")
                return total
            if isinstance(item, list):
                total = 1
                for nested in item:
                    total += walk(nested, depth=depth + 1)
                    if total > 2000:
                        raise ValueError("请求上下文结构节点不能超过 2000 个")
                return total
            return 1

        walk(value)
        return value


class ContentGuardExternalTarget(BaseModel):
    base_url: str = Field(..., min_length=1, max_length=512)
    api_key: str = Field(..., min_length=1, max_length=4096)
    model_name: str = Field(..., min_length=1, max_length=256)
    endpoint_path: ContentGuardEndpointPath = "/chat/completions"

    @field_validator("base_url")
    @classmethod
    def normalize_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        if not normalized:
            raise ValueError("接口地址不能为空")
        if len(normalized.encode("utf-8")) > 512:
            raise ValueError("外部提供商接口地址不能超过 512 字节")
        parsed = urlparse(normalized)
        if parsed.scheme != "https":
            raise ValueError("外部提供商接口地址必须使用 https://")
        if not parsed.hostname:
            raise ValueError("接口地址必须包含有效主机名")
        if parsed.username or parsed.password:
            raise ValueError("外部提供商接口地址禁止包含用户名或密码")
        if parsed.query or parsed.fragment:
            raise ValueError("外部提供商接口地址禁止包含查询参数或片段")
        hostname = parsed.hostname.lower().strip(".")
        if hostname in {"169.254.169.254", "metadata.google.internal"} or hostname.endswith(".internal"):
            raise ValueError("外部提供商接口地址禁止使用云元数据或内部主机名")
        if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".localhost"):
            raise ValueError("外部提供商接口地址禁止使用 localhost")
        try:
            host_ip = ipaddress.ip_address(hostname)
        except ValueError:
            host_ip = None
        if host_ip is not None and (
            host_ip.is_private
            or host_ip.is_loopback
            or host_ip.is_link_local
            or host_ip.is_multicast
            or host_ip.is_reserved
            or host_ip.is_unspecified
        ):
            raise ValueError("外部提供商接口地址禁止使用内网、回环、链路本地或保留地址")
        if host_ip is None:
            try:
                resolved_hosts = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
            except socket.gaierror as exc:
                raise ValueError("外部提供商接口地址主机名无法解析") from exc
            for resolved in resolved_hosts:
                address = resolved[4][0]
                try:
                    resolved_ip = ipaddress.ip_address(address)
                except ValueError:
                    continue
                if (
                    resolved_ip.is_private
                    or resolved_ip.is_loopback
                    or resolved_ip.is_link_local
                    or resolved_ip.is_multicast
                    or resolved_ip.is_reserved
                    or resolved_ip.is_unspecified
                ):
                    raise ValueError("外部提供商接口地址解析到内网、回环、链路本地或保留地址")
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
    probe_keys: list[ContentGuardProbeKey] = Field(
        default_factory=lambda: ["fixed_answer", "pollution_rules", "sse"],
        max_length=4,
    )
    persist_internal_result: bool = True

    @field_validator("probe_keys")
    @classmethod
    def normalize_probe_keys(cls, value: list[ContentGuardProbeKey]) -> list[ContentGuardProbeKey]:
        ordered: list[ContentGuardProbeKey] = []
        for item in value or []:
            if item not in ordered:
                ordered.append(item)
        return ordered or ["fixed_answer", "pollution_rules", "sse"]
