import re
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, field_validator


PROVIDER_PROTOCOL_TYPE_LABELS = {
    "both": "双协议",
    "chat_completions": "Chat Completions API",
    "responses": "Responses API",
    "gemini": "Gemini 原生协议",
    "claude_messages": "Claude Messages API",
}

MODEL_GROUP_LABELS = {
    "openai": "OpenAI",
    "deepseek": "DeepSeek",
    "qwen": "通义千问",
    "glm": "智谱 GLM",
    "doubao": "豆包",
    "kimi": "Kimi",
    "baichuan": "百川",
    "ernie": "文心一言",
    "hunyuan": "腾讯混元",
    "minimax": "MiniMax",
    "step": "阶跃星辰",
    "internlm": "书生浦语",
    "spark": "讯飞星火",
    "yi": "零一万物",
    "mistral": "Mistral",
    "llama": "Llama",
    "gemini": "Gemini",
    "claude": "Claude",
    "grok": "Grok",
    "cohere": "Cohere",
    "perplexity": "Perplexity",
    "unknown": "未知分组",
}

PROVIDER_TRUST_LEVEL_LABELS = {
    "official": "官方",
    "trusted": "可信",
    "standard": "标准",
    "low": "低信任",
    "blocked": "已阻断",
}

CONTENT_INTEGRITY_STATUS_LABELS = {
    "unknown": "未探测",
    "passed": "通过",
    "degraded": "降级",
    "blocked": "已隔离",
}

MODEL_TRUST_STATUS_LABELS = {
    "trusted": "可信",
    "abnormal": "异常",
    "unknown": "未检测",
}

PROVIDER_TRUST_STATUS_LABELS = {
    "trusted": "可信",
    "partially_trusted": "部分可信",
    "untrusted": "不可信",
    "unknown": "未检测",
}

MAINTENANCE_WINDOW_TIMEZONE = "Asia/Shanghai"
MAINTENANCE_WINDOW_DAILY_RE = re.compile(r"^每日\s+(\d{2}:\d{2})-(\d{2}:\d{2})\s+Asia/Shanghai$")
MAINTENANCE_WINDOW_WEEKLY_RE = re.compile(r"^每周([一二三四五六日])\s+(\d{2}:\d{2})-(\d{2}:\d{2})\s+Asia/Shanghai$")
MAINTENANCE_WINDOW_ONCE_RE = re.compile(
    r"^单次\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})-(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})\s+Asia/Shanghai$"
)
PROVIDER_NAME_RE = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9]+$")


def normalize_provider_name(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("提供商名称不能为空")
    if not PROVIDER_NAME_RE.fullmatch(text):
        raise ValueError("提供商名称只能包含中文、英文字母或数字，不能包含标点符号或空格")
    return text


def _validate_maintenance_time_range(start: str, end: str) -> None:
    try:
        datetime.strptime(start, "%H:%M")
        datetime.strptime(end, "%H:%M")
    except ValueError as exc:
        raise ValueError("维护窗口时间必须使用 HH:MM 格式") from exc
    if start == end:
        raise ValueError("维护窗口开始时间和结束时间不能相同")


def normalize_provider_maintenance_window(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    daily = MAINTENANCE_WINDOW_DAILY_RE.match(text)
    if daily:
        start, end = daily.groups()
        _validate_maintenance_time_range(start, end)
        return f"每日 {start}-{end} {MAINTENANCE_WINDOW_TIMEZONE}"
    weekly = MAINTENANCE_WINDOW_WEEKLY_RE.match(text)
    if weekly:
        weekday, start, end = weekly.groups()
        _validate_maintenance_time_range(start, end)
        return f"每周{weekday} {start}-{end} {MAINTENANCE_WINDOW_TIMEZONE}"
    once = MAINTENANCE_WINDOW_ONCE_RE.match(text)
    if once:
        start_text, end_text = once.groups()
        try:
            start_at = datetime.strptime(start_text, "%Y-%m-%d %H:%M")
            end_at = datetime.strptime(end_text, "%Y-%m-%d %H:%M")
        except ValueError as exc:
            raise ValueError("单次维护窗口必须使用 YYYY-MM-DD HH:MM 格式") from exc
        if end_at <= start_at:
            raise ValueError("单次维护窗口结束时间必须晚于开始时间")
        return f"单次 {start_text}-{end_text} {MAINTENANCE_WINDOW_TIMEZONE}"
    raise ValueError("维护窗口仅支持北京时间：每日 HH:MM-HH:MM Asia/Shanghai、每周日 HH:MM-HH:MM Asia/Shanghai、单次 YYYY-MM-DD HH:MM-YYYY-MM-DD HH:MM Asia/Shanghai")


def normalize_provider_trust_level(value: str | None) -> str:
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "": "standard",
        "official": "official",
        "trusted": "trusted",
        "standard": "standard",
        "low": "low",
        "blocked": "blocked",
        "官方": "official",
        "可信": "trusted",
        "标准": "standard",
        "低信任": "low",
        "已阻断": "blocked",
        "阻断": "blocked",
    }
    if raw in aliases:
        return aliases[raw]
    raise ValueError("信任等级仅支持 官方、可信、标准、低信任、已阻断")


def normalize_content_integrity_status(value: str | None) -> str:
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "": "unknown",
        "unknown": "unknown",
        "passed": "passed",
        "pass": "passed",
        "degraded": "degraded",
        "blocked": "blocked",
        "未探测": "unknown",
        "通过": "passed",
        "降级": "degraded",
        "已隔离": "blocked",
        "隔离": "blocked",
    }
    if raw in aliases:
        return aliases[raw]
    raise ValueError("内容完整性状态仅支持 未探测、通过、降级、已隔离")


def normalize_provider_protocol_type(value: str | None) -> str:
    raw = str(value or "").strip()
    normalized = raw.lower().replace("-", "_").replace(" ", "_")
    compact = raw.lower().replace(" ", "").replace("_", "").replace("-", "")
    aliases = {
        "": "both",
        "both": "both",
        "all": "both",
        "dual": "both",
        "dualprotocol": "both",
        "chatandresponses": "both",
        "chatresponses": "both",
        "都支持": "both",
        "双协议": "both",
        "全部": "both",
        "全协议": "both",
        "chat": "chat_completions",
        "chatcompletion": "chat_completions",
        "chatcompletions": "chat_completions",
        "chatcompletionapi": "chat_completions",
        "chatcompletionsapi": "chat_completions",
        "聊天": "chat_completions",
        "对话": "chat_completions",
        "responses": "responses",
        "response": "responses",
        "responsesapi": "responses",
        "响应": "responses",
        "响应式": "responses",
        "gemini": "gemini",
        "google": "gemini",
        "googleai": "gemini",
        "googleaiapi": "gemini",
        "geminiapi": "gemini",
        "gemini原生协议": "gemini",
        "claude": "claude_messages",
        "anthropic": "claude_messages",
        "anthropicapi": "claude_messages",
        "claudemessages": "claude_messages",
        "claudemessagesapi": "claude_messages",
        "claudeapi": "claude_messages",
        "claude原生协议": "claude_messages",
    }
    if normalized in PROVIDER_PROTOCOL_TYPE_LABELS:
        return normalized
    if compact in aliases:
        return aliases[compact]
    raise ValueError("协议仅支持 双协议、Chat Completions API、Responses API、Gemini 原生协议或 Claude Messages API")


def normalize_native_endpoint_path(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.lower().startswith(("http://", "https://")):
        raise ValueError("原生接口路径只填写路径部分，例如 /models/{model}:{action}，域名请填写在 Base URL")
    return text if text.startswith("/") else f"/{text}"


def format_provider_protocol_label(value: str | None) -> str:
    return PROVIDER_PROTOCOL_TYPE_LABELS.get(normalize_provider_protocol_type(value), PROVIDER_PROTOCOL_TYPE_LABELS["both"])


def protocol_type_from_supports(*, supports_chat_completions: bool, supports_responses: bool) -> str:
    if supports_chat_completions and supports_responses:
        return "both"
    if supports_chat_completions:
        return "chat_completions"
    return "responses"


def supports_from_protocol_type(value: str | None) -> tuple[bool, bool]:
    normalized = normalize_provider_protocol_type(value or "responses")
    return normalized in {"both", "chat_completions"}, normalized in {"both", "responses"}


def normalize_model_group(value: str | None) -> str:
    raw = str(value or "").strip()
    normalized = raw.lower().replace(" ", "").replace("_", "").replace("-", "")
    aliases = {
        "": "unknown",
        "unknown": "unknown",
        "未知": "unknown",
        "openai": "openai",
        "gpt": "openai",
        "deepseek": "deepseek",
        "qwen": "qwen",
        "通义千问": "qwen",
        "tongyi": "qwen",
        "glm": "glm",
        "zhipu": "glm",
        "智谱": "glm",
        "doubao": "doubao",
        "豆包": "doubao",
        "bytedance": "doubao",
        "kimi": "kimi",
        "moonshot": "kimi",
        "月之暗面": "kimi",
        "baichuan": "baichuan",
        "百川": "baichuan",
        "ernie": "ernie",
        "wenxin": "ernie",
        "文心": "ernie",
        "hunyuan": "hunyuan",
        "混元": "hunyuan",
        "minimax": "minimax",
        "abab": "minimax",
        "step": "step",
        "阶跃": "step",
        "internlm": "internlm",
        "书生": "internlm",
        "spark": "spark",
        "讯飞星火": "spark",
        "yi": "yi",
        "零一万物": "yi",
        "mistral": "mistral",
        "llama": "llama",
        "meta": "llama",
        "gemini": "gemini",
        "google": "gemini",
        "claude": "claude",
        "anthropic": "claude",
        "grok": "grok",
        "xai": "grok",
        "cohere": "cohere",
        "command": "cohere",
        "perplexity": "perplexity",
        "sonar": "perplexity",
    }
    if raw in MODEL_GROUP_LABELS:
        return raw
    if normalized in aliases:
        return aliases[normalized]
    raise ValueError("模型分组仅支持国内外主流大模型品牌名")


class ProviderModelConfigBase(BaseModel):
    model_name: str = Field(..., min_length=1)
    upstream_model_name: str | None = None
    model_group: str | None = None
    enabled: bool = True
    priority: int = 100
    protocol_type: str = "responses"
    native_endpoint_path: str | None = None
    supports_stream: bool = True
    supports_vision: bool = True
    supports_tools: bool = True
    supports_image_generation: bool = False
    supports_chat_completions: bool = False
    supports_responses: bool = True
    context_window_tokens: int | None = Field(default=None, ge=1)
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    max_active_requests: int | None = Field(default=None, ge=0)
    max_active_streams: int | None = Field(default=None, ge=0)
    max_qps: int | None = Field(default=None, ge=0)
    max_rpm: int | None = Field(default=None, ge=0)
    price_multiplier: float = Field(default=1.0, gt=0)
    input_price_per_1k: Decimal | None = Field(default=None, ge=0)
    output_price_per_1k: Decimal | None = Field(default=None, ge=0)
    cache_price_per_1k: Decimal | None = Field(default=None, ge=0)
    cache_write_price_per_1k: Decimal | None = Field(default=None, ge=0)

    @field_validator("model_name")
    @classmethod
    def normalize_model_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("upstream_model_name")
    @classmethod
    def normalize_upstream_model_name(cls, value: str | None) -> str | None:
        text = str(value or "").strip()
        return text or None

    @field_validator("protocol_type")
    @classmethod
    def normalize_protocol_type(cls, value: str | None) -> str:
        return normalize_provider_protocol_type(value or "responses")

    @field_validator("native_endpoint_path")
    @classmethod
    def normalize_native_path(cls, value: str | None) -> str | None:
        return normalize_native_endpoint_path(value)

    @field_validator("model_group")
    @classmethod
    def normalize_group(cls, value: str | None) -> str | None:
        if value is None or str(value).strip() == "":
            return None
        return normalize_model_group(value)


class ProviderModelConfigInput(ProviderModelConfigBase):
    pass


class ProviderModelConfigOut(ProviderModelConfigBase):
    id: int
    model_group: str = "unknown"
    model_group_label: str = "未知分组"
    protocol_type: str = "responses"
    protocol_label: str = "Responses API"
    health_status: str
    db_health: str = "unknown"
    runtime_health: str | None = None
    effective_health: str = "unknown"
    db_availability: str = "unknown"
    runtime_availability: str | None = None
    effective_availability: str = "unknown"
    availability_status: str = "unknown"
    route_availability_status: str = "normal"
    route_availability_status_label: str = "正常"
    route_availability_reasons: list[str] = Field(default_factory=list)
    route_availability_reason_text: str | None = None
    state_source: str = "db"
    health_state_updated_at: datetime | str | None = None
    availability_state_updated_at: datetime | str | None = None
    circuit_state: str
    circuit_opened_at: datetime | None
    last_check_at: datetime | None
    last_latency_ms: int | None
    failure_count: int
    success_count: int
    last_error: str | None
    content_integrity_status: str = "unknown"
    content_integrity_status_label: str = "未探测"
    content_probe_last_passed_at: datetime | None = None
    content_probe_last_failed_at: datetime | None = None
    content_probe_failure_count: int = 0
    content_probe_results: list[dict[str, Any]] = Field(default_factory=list)
    trust_status: str = "unknown"
    trust_status_label: str = "未检测"
    trust_status_reason: str | None = None
    recent_request_count: int = 0
    success_rate: float | None = None
    avg_first_token_latency_ms: float | None = None
    stability_score: float | None = None
    quality_window_minutes: int = 60
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ProviderModelMountProviderOut(BaseModel):
    id: int
    name: str
    base_url: str
    protocol_type: str = "both"
    protocol_label: str = "双协议"
    native_endpoint_path: str | None = None
    group_name: str | None = None
    region_tag: str | None = None
    enabled: bool
    health_status: str
    db_health: str = "unknown"
    runtime_health: str | None = None
    effective_health: str = "unknown"
    db_availability: str = "unknown"
    runtime_availability: str | None = None
    effective_availability: str = "unknown"
    availability_status: str = "unknown"
    state_source: str = "db"
    health_state_updated_at: datetime | str | None = None
    availability_state_updated_at: datetime | str | None = None
    trust_status: str = "unknown"
    trust_status_label: str = "未检测"
    trust_status_reason: str | None = None


class ProviderModelMountOut(BaseModel):
    provider: ProviderModelMountProviderOut
    model: ProviderModelConfigOut


class ProviderModelMountListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    total_pages: int
    quality_window_hours: int = 1
    items: list[ProviderModelMountOut]


class ProviderModelConfigUpdate(BaseModel):
    model_name: str | None = None
    upstream_model_name: str | None = None
    model_group: str | None = None
    enabled: bool | None = None
    priority: int | None = None
    protocol_type: str | None = None
    native_endpoint_path: str | None = None
    supports_stream: bool | None = None
    supports_vision: bool | None = None
    supports_tools: bool | None = None
    supports_image_generation: bool | None = None
    supports_chat_completions: bool | None = None
    supports_responses: bool | None = None
    context_window_tokens: int | None = Field(default=None, ge=1)
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    max_active_requests: int | None = Field(default=None, ge=0)
    max_active_streams: int | None = Field(default=None, ge=0)
    max_qps: int | None = Field(default=None, ge=0)
    max_rpm: int | None = Field(default=None, ge=0)
    price_multiplier: float | None = Field(default=None, gt=0)
    input_price_per_1k: Decimal | None = Field(default=None, ge=0)
    output_price_per_1k: Decimal | None = Field(default=None, ge=0)
    cache_price_per_1k: Decimal | None = Field(default=None, ge=0)
    cache_write_price_per_1k: Decimal | None = Field(default=None, ge=0)
    content_integrity_status: str | None = None

    @field_validator("protocol_type")
    @classmethod
    def normalize_protocol_type(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_provider_protocol_type(value)

    @field_validator("upstream_model_name")
    @classmethod
    def normalize_upstream_model_name(cls, value: str | None) -> str | None:
        text = str(value or "").strip()
        return text or None

    @field_validator("model_name")
    @classmethod
    def normalize_model_name(cls, value: str | None) -> str | None:
        text = str(value or "").strip()
        return text or None

    @field_validator("native_endpoint_path")
    @classmethod
    def normalize_native_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_native_endpoint_path(value)

    @field_validator("model_group")
    @classmethod
    def normalize_group(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_model_group(value)

    @field_validator("content_integrity_status")
    @classmethod
    def normalize_integrity_status(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_content_integrity_status(value)


class ProviderBatchConnectivityTestRequest(BaseModel):
    provider_ids: list[int] = Field(default_factory=list)


class ProviderEndpointProtocolDetectionRequest(BaseModel):
    provider_ids: list[int] = Field(default_factory=list)


class ProviderModelEndpointProtocolDetectionTarget(BaseModel):
    provider_id: int = Field(..., ge=1)
    provider_model_id: int = Field(..., ge=1)


class ProviderModelEndpointProtocolDetectionRequest(BaseModel):
    targets: list[ProviderModelEndpointProtocolDetectionTarget] = Field(default_factory=list)


class ProviderBatchImportRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=200000)
    dry_run: bool = True
    skip_duplicates: bool = True

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        return value.strip()


class ProviderBatchImportItemOut(BaseModel):
    index: int
    name: str | None = None
    base_url: str | None = None
    model_count: int = 0
    valid: bool = False
    skipped: bool = False
    created: bool = False
    errors: list[str] = Field(default_factory=list)
    provider: dict[str, Any] | None = None


class ProviderBatchImportResponse(BaseModel):
    total: int
    valid_count: int
    created_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    dry_run: bool
    template: str | None = None
    items: list[ProviderBatchImportItemOut]


class ProviderModelBatchImportRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=200000)
    dry_run: bool = True
    skip_missing_providers: bool = True

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        return value.strip()


class ProviderModelBatchImportItemOut(BaseModel):
    index: int
    provider_name: str | None = None
    model_name: str | None = None
    upstream_model_name: str | None = None
    valid: bool = False
    skipped: bool = False
    created: bool = False
    updated: bool = False
    errors: list[str] = Field(default_factory=list)
    model: ProviderModelConfigOut | None = None


class ProviderModelBatchImportResponse(BaseModel):
    total: int
    valid_count: int
    created_count: int = 0
    updated_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    dry_run: bool
    template: str | None = None
    items: list[ProviderModelBatchImportItemOut]


class ProviderBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    base_url: str = Field(..., min_length=1)
    api_key: str = Field(..., min_length=1)
    provider_type: str = "openai_compatible"
    protocol_type: str = "both"
    native_endpoint_path: str | None = None
    group_name: str | None = None
    region_tag: str | None = None
    enabled: bool = True
    priority: int = 100
    timeout_ms: int = 30000
    max_retries: int = 2
    max_active_requests: int | None = Field(default=20, ge=0)
    max_active_streams: int | None = Field(default=10, ge=0)
    max_qps: int | None = Field(default=20, ge=0)
    max_rpm: int | None = Field(default=20, ge=0)
    first_token_timeout_sec: int | None = Field(default=60, ge=1)
    maintenance_window: str | None = None
    maintenance_mode_enabled: bool = False
    auto_circuit_break_enabled: bool = True
    auto_recover_enabled: bool = True
    circuit_breaker_threshold_override: int | None = Field(default=None, ge=0)
    recovery_probe_interval_sec_override: int | None = Field(default=None, ge=0)
    trust_level: str = "standard"
    content_integrity_status: str = "unknown"
    content_integrity_score: int = Field(default=80, ge=0, le=100)
    content_guard_enabled: bool = True
    buffer_stream_for_guard: bool = True
    models: list[str] = Field(default_factory=list)
    model_configs: list[ProviderModelConfigInput] = Field(default_factory=list)
    remark: str | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return normalize_provider_name(value)

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

    @field_validator("protocol_type")
    @classmethod
    def normalize_protocol_type(cls, value: str | None) -> str:
        return normalize_provider_protocol_type(value)

    @field_validator("native_endpoint_path")
    @classmethod
    def normalize_native_path(cls, value: str | None) -> str | None:
        return normalize_native_endpoint_path(value)

    @field_validator("maintenance_window")
    @classmethod
    def normalize_maintenance_window(cls, value: str | None) -> str | None:
        return normalize_provider_maintenance_window(value)

    @field_validator("trust_level")
    @classmethod
    def normalize_trust_level(cls, value: str | None) -> str:
        return normalize_provider_trust_level(value)

    @field_validator("content_integrity_status")
    @classmethod
    def normalize_integrity_status(cls, value: str | None) -> str:
        return normalize_content_integrity_status(value)


class ProviderCreate(ProviderBase):
    pass


class ProviderUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    provider_type: str | None = None
    protocol_type: str | None = None
    native_endpoint_path: str | None = None
    group_name: str | None = None
    region_tag: str | None = None
    enabled: bool | None = None
    priority: int | None = None
    timeout_ms: int | None = None
    max_retries: int | None = None
    max_active_requests: int | None = Field(default=None, ge=0)
    max_active_streams: int | None = Field(default=None, ge=0)
    max_qps: int | None = Field(default=None, ge=0)
    max_rpm: int | None = Field(default=None, ge=0)
    first_token_timeout_sec: int | None = Field(default=None, ge=1)
    maintenance_window: str | None = None
    maintenance_mode_enabled: bool | None = None
    auto_circuit_break_enabled: bool | None = None
    auto_recover_enabled: bool | None = None
    circuit_breaker_threshold_override: int | None = Field(default=None, ge=0)
    recovery_probe_interval_sec_override: int | None = Field(default=None, ge=0)
    trust_level: str | None = None
    content_integrity_status: str | None = None
    content_integrity_score: int | None = Field(default=None, ge=0, le=100)
    content_guard_enabled: bool | None = None
    buffer_stream_for_guard: bool | None = None
    models: list[str] | None = None
    model_configs: list[ProviderModelConfigInput] | None = None
    remark: str | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_provider_name(value)

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

    @field_validator("protocol_type")
    @classmethod
    def normalize_protocol_type(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_provider_protocol_type(value)

    @field_validator("native_endpoint_path")
    @classmethod
    def normalize_native_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_native_endpoint_path(value)

    @field_validator("maintenance_window")
    @classmethod
    def normalize_maintenance_window(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_provider_maintenance_window(value)

    @field_validator("trust_level")
    @classmethod
    def normalize_trust_level(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_provider_trust_level(value)

    @field_validator("content_integrity_status")
    @classmethod
    def normalize_integrity_status(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_content_integrity_status(value)


class ProviderBatchGovernanceRequest(BaseModel):
    provider_ids: list[int] = Field(..., min_length=1)
    action: str

    @field_validator("provider_ids")
    @classmethod
    def normalize_provider_ids(cls, value: list[int]) -> list[int]:
        normalized = [int(item) for item in value if int(item) > 0]
        if not normalized:
            raise ValueError("请先选择要操作的提供商")
        return list(dict.fromkeys(normalized))

    @field_validator("action")
    @classmethod
    def normalize_action(cls, value: str) -> str:
        normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        allowed = {"enable", "disable", "mark_available", "mark_trusted"}
        if normalized not in allowed:
            raise ValueError("批量治理动作仅支持 enable、disable、mark_available、mark_trusted")
        return normalized


class ProviderOut(BaseModel):
    id: int
    name: str
    base_url: str
    api_key: str
    api_key_masked: str
    provider_type: str
    protocol_type: str
    protocol_label: str
    native_endpoint_path: str | None = None
    group_name: str | None
    region_tag: str | None
    enabled: bool
    priority: int
    timeout_ms: int
    max_retries: int
    max_active_requests: int | None
    max_active_streams: int | None
    max_qps: int | None
    max_rpm: int | None
    first_token_timeout_sec: int | None
    active_requests: int = 0
    active_streams: int = 0
    current_qps: int = 0
    current_rpm: int = 0
    maintenance_window: str | None
    maintenance_mode_enabled: bool
    auto_circuit_break_enabled: bool
    auto_recover_enabled: bool
    circuit_breaker_threshold_override: int | None
    recovery_probe_interval_sec_override: int | None
    trust_level: str
    trust_level_label: str = "标准"
    content_integrity_status: str
    content_integrity_status_label: str = "未探测"
    content_integrity_score: int
    computed_trust_status: str = "unknown"
    computed_trust_status_label: str = "未检测"
    computed_trust_status_reason: str | None = None
    content_violation_count: int
    last_content_violation_at: datetime | None
    recent_content_guard_events: list[dict[str, Any]] = Field(default_factory=list)
    content_guard_enabled: bool
    buffer_stream_for_guard: bool
    models: list[str]
    model_configs: list[ProviderModelConfigOut]
    model_config_count: int = 0
    model_configs_truncated: bool = False
    health_status: str
    db_health: str = "unknown"
    runtime_health: str | None = None
    effective_health: str = "unknown"
    db_availability: str = "unknown"
    runtime_availability: str | None = None
    effective_availability: str = "unknown"
    availability_status: str = "unknown"
    state_source: str = "db"
    health_state_updated_at: datetime | str | None = None
    availability_state_updated_at: datetime | str | None = None
    last_check_at: datetime | None
    last_latency_ms: int | None
    failure_count: int
    success_count: int
    circuit_state: str
    recent_request_count: int = 0
    success_rate: float | None = None
    avg_first_token_latency_ms: float | None = None
    stability_score: float | None = None
    quality_window_minutes: int = 60
    best_input_price_per_1k: float | None = None
    best_output_price_per_1k: float | None = None
    credential_rotated_at: datetime | None = None
    credential_hint: str | None = None
    remark: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ProviderBatchGovernanceResponse(BaseModel):
    action: str
    requested_count: int
    updated_count: int
    skipped_count: int = 0
    missing_ids: list[int] = Field(default_factory=list)
    items: list[ProviderOut] = Field(default_factory=list)


class ProviderTelemetryCardOut(BaseModel):
    id: str
    label: str
    value: int | float


class ProviderPageContentOut(BaseModel):
    providers: list[ProviderOut]
    summary: dict[str, int | float]
    telemetry_cards: list[ProviderTelemetryCardOut]


class ProviderListResponse(BaseModel):
    items: list[ProviderOut]
    total: int
    page: int
    page_size: int
    total_pages: int
    filter_options: dict[str, list[str]] = Field(default_factory=dict)


class ProviderOptionOut(BaseModel):
    id: int
    name: str
    group_name: str | None = None
    region_tag: str | None = None
    enabled: bool
    health_status: str
    db_health: str = "unknown"
    runtime_health: str | None = None
    effective_health: str = "unknown"
    db_availability: str = "unknown"
    runtime_availability: str | None = None
    effective_availability: str = "unknown"
    availability_status: str = "unknown"
    state_source: str = "db"
    health_state_updated_at: datetime | str | None = None
    availability_state_updated_at: datetime | str | None = None
    protocol_type: str = "both"
    protocol_label: str = "双协议"
    native_endpoint_path: str | None = None
    models: list[str] = Field(default_factory=list)


class ProviderPlaygroundModelOut(BaseModel):
    id: int
    model_name: str
    enabled: bool
    supports_stream: bool = True
    supports_vision: bool = True
    supports_tools: bool = True
    supports_image_generation: bool = False
    supports_chat_completions: bool = True
    supports_responses: bool = True


class ProviderPlaygroundOut(BaseModel):
    id: int
    name: str
    group_name: str | None = None
    region_tag: str | None = None
    enabled: bool
    health_status: str
    db_health: str = "unknown"
    runtime_health: str | None = None
    effective_health: str = "unknown"
    db_availability: str = "unknown"
    runtime_availability: str | None = None
    effective_availability: str = "unknown"
    availability_status: str = "unknown"
    state_source: str = "db"
    health_state_updated_at: datetime | str | None = None
    availability_state_updated_at: datetime | str | None = None
    protocol_type: str = "both"
    protocol_label: str = "双协议"
    native_endpoint_path: str | None = None
    models: list[str] = Field(default_factory=list)
    model_configs: list[ProviderPlaygroundModelOut] = Field(default_factory=list)


class ProviderSummaryOut(BaseModel):
    id: int
    name: str
    group_name: str | None = None
    region_tag: str | None = None
    enabled: bool
    priority: int
    health_status: str
    db_health: str = "unknown"
    runtime_health: str | None = None
    effective_health: str = "unknown"
    db_availability: str = "unknown"
    runtime_availability: str | None = None
    effective_availability: str = "unknown"
    availability_status: str = "unknown"
    state_source: str = "db"
    health_state_updated_at: datetime | str | None = None
    availability_state_updated_at: datetime | str | None = None
    protocol_type: str = "both"
    protocol_label: str = "双协议"
    native_endpoint_path: str | None = None
    circuit_state: str
    last_latency_ms: int | None = None
    models: list[str] = Field(default_factory=list)


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


class ProviderCredentialAuth(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)

    @field_validator("username")
    @classmethod
    def normalize_username_for_credential_api(cls, value: str) -> str:
        return value.strip()


class ProviderCredentialExternalItem(BaseModel):
    id: int
    name: str
    base_url: str
    enabled: bool
    group_name: str | None = None
    region_tag: str | None = None
    api_key: str
    masked_api_key: str
    credential_hint: str | None = None
    credential_rotated_at: datetime | None = None
    updated_at: datetime


class ProviderCredentialListRequest(ProviderCredentialAuth):
    provider_ids: list[int] = Field(default_factory=list)
    provider_names: list[str] = Field(default_factory=list)

    @field_validator("provider_ids")
    @classmethod
    def normalize_provider_ids(cls, value: list[int]) -> list[int]:
        seen: set[int] = set()
        normalized: list[int] = []
        for item in value:
            provider_id = int(item)
            if provider_id <= 0 or provider_id in seen:
                continue
            seen.add(provider_id)
            normalized.append(provider_id)
        return normalized

    @field_validator("provider_names")
    @classmethod
    def normalize_provider_names(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        normalized: list[str] = []
        for item in value:
            name = str(item or "").strip()
            key = name.lower()
            if not name or key in seen:
                continue
            seen.add(key)
            normalized.append(name)
        return normalized


class ProviderCredentialListResponse(BaseModel):
    total: int
    providers: list[ProviderCredentialExternalItem]


class ProviderCredentialUpdateItem(BaseModel):
    provider_id: int | None = Field(default=None, ge=1)
    provider_name: str | None = Field(default=None, min_length=1)
    api_key: str = Field(..., min_length=1)
    credential_hint: str | None = None

    @field_validator("provider_name", "credential_hint")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("api_key")
    @classmethod
    def normalize_required_api_key(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("提供商密钥不能为空")
        return normalized


class ProviderCredentialUpdateRequest(ProviderCredentialAuth, ProviderCredentialUpdateItem):
    pass


class ProviderCredentialBatchUpdateRequest(ProviderCredentialAuth):
    items: list[ProviderCredentialUpdateItem] = Field(..., min_length=1, max_length=100)


class ProviderCredentialUpdateResult(BaseModel):
    success: bool
    provider_id: int | None = None
    provider_name: str | None = None
    message: str
    provider: ProviderCredentialExternalItem | None = None


class ProviderCredentialBatchUpdateResponse(BaseModel):
    total: int
    success_count: int
    failed_count: int
    results: list[ProviderCredentialUpdateResult]


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
    model_group: str = "unknown"
    model_group_label: str = "未知分组"
    supports_stream: bool = True
    supports_vision: bool = True
    supports_tools: bool = True
    supports_image_generation: bool = False
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
