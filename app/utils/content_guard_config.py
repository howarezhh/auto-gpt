from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse


CONTENT_GUARD_RULE_SCHEMA_VERSION = 1

CONTENT_GUARD_HIGH_RISK_STRATEGIES = ("switch_provider", "block", "safe_error", "record_only")
CONTENT_GUARD_STREAM_MODES = ("pass_through_scan", "buffer_300ms", "full_buffer")
CONTENT_GUARD_RULE_MATCH_TYPES = ("keyword_any", "regex", "unexpected_url")
CONTENT_GUARD_RULE_RISK_LEVELS = ("low", "medium", "high")
CONTENT_GUARD_RULE_ACTIONS = ("allow", "record", "block")

CONTENT_GUARD_SETTING_DEFAULTS: dict[str, Any] = {
    "content_guard_enabled": True,
    "content_guard_precheck_auto_enabled": False,
    "content_guard_block_on_high_risk": True,
    "content_guard_probe_interval_sec": 3600,
    "content_guard_max_scan_bytes": 16384,
    "content_guard_stream_buffer_max_bytes": 16384,
    "content_guard_low_trust_requires_buffer": True,
    "content_guard_rules_json": "",
    "content_guard_high_risk_strategy": "switch_provider",
    "content_guard_max_detection_delay_ms": 300,
    "content_guard_stream_mode": "buffer_300ms",
    "content_guard_url_check_enabled": True,
    "content_guard_url_allowlist_json": "",
    "content_guard_async_review_enabled": True,
    "content_guard_high_risk_confidence_threshold": 85,
}

CONTENT_GUARD_SETTING_GROUPS: dict[str, tuple[str, ...]] = {
    "runtime": (
        "content_guard_enabled",
        "content_guard_block_on_high_risk",
        "content_guard_max_scan_bytes",
        "content_guard_stream_buffer_max_bytes",
        "content_guard_low_trust_requires_buffer",
        "content_guard_high_risk_strategy",
        "content_guard_max_detection_delay_ms",
        "content_guard_stream_mode",
        "content_guard_url_check_enabled",
        "content_guard_url_allowlist_json",
        "content_guard_async_review_enabled",
        "content_guard_high_risk_confidence_threshold",
    ),
    "precheck": (
        "content_guard_enabled",
        "content_guard_precheck_auto_enabled",
        "content_guard_probe_interval_sec",
    ),
    "rules": ("content_guard_rules_json",),
}

CONTENT_GUARD_SETTING_DESCRIPTIONS: dict[str, str] = {
    "content_guard_enabled": "内容防护治理总开关；关闭后运行时检测、自动预检和自动处置同时停用。",
    "content_guard_precheck_auto_enabled": "内容防护预先防护自动检测开关；仅在总开关启用时生效，不依赖健康检查总开关。",
    "content_guard_block_on_high_risk": "高风险命中是否允许阻断、切换提供商或安全错误；record_only 策略下必须关闭。",
    "content_guard_high_risk_strategy": "高风险运行时处置策略。",
    "content_guard_stream_mode": "流式检测模式。",
}


def content_guard_default(field_name: str, fallback: Any = None) -> Any:
    return CONTENT_GUARD_SETTING_DEFAULTS.get(field_name, fallback)


def normalize_content_guard_settings(values: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(values)
    for field_name, default_value in CONTENT_GUARD_SETTING_DEFAULTS.items():
        if field_name not in normalized or normalized[field_name] is None:
            normalized[field_name] = default_value

    normalized["content_guard_probe_interval_sec"] = max(
        300,
        int(normalized.get("content_guard_probe_interval_sec") or content_guard_default("content_guard_probe_interval_sec")),
    )
    normalized["content_guard_max_scan_bytes"] = max(
        1024,
        int(normalized.get("content_guard_max_scan_bytes") or content_guard_default("content_guard_max_scan_bytes")),
    )
    normalized["content_guard_stream_buffer_max_bytes"] = max(
        1024,
        int(normalized.get("content_guard_stream_buffer_max_bytes") or content_guard_default("content_guard_stream_buffer_max_bytes")),
    )
    normalized["content_guard_max_detection_delay_ms"] = min(
        500,
        max(0, int(normalized.get("content_guard_max_detection_delay_ms") or content_guard_default("content_guard_max_detection_delay_ms"))),
    )
    normalized["content_guard_high_risk_confidence_threshold"] = min(
        100,
        max(0, int(normalized.get("content_guard_high_risk_confidence_threshold") or content_guard_default("content_guard_high_risk_confidence_threshold"))),
    )
    return normalized


def validate_content_guard_settings(values: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_content_guard_settings(values)
    strategy = str(normalized.get("content_guard_high_risk_strategy") or "")
    if strategy not in CONTENT_GUARD_HIGH_RISK_STRATEGIES:
        raise ValueError("content_guard_high_risk_strategy 无效")
    stream_mode = str(normalized.get("content_guard_stream_mode") or "")
    if stream_mode not in CONTENT_GUARD_STREAM_MODES:
        raise ValueError("content_guard_stream_mode 无效")
    if not bool(normalized.get("content_guard_enabled")) and bool(normalized.get("content_guard_precheck_auto_enabled")):
        raise ValueError("内容防护总开关关闭时不能启用自动预检")
    if strategy == "record_only" and bool(normalized.get("content_guard_block_on_high_risk")):
        raise ValueError("record_only 策略下必须关闭高风险阻断开关")
    if strategy in {"switch_provider", "block", "safe_error"} and not bool(normalized.get("content_guard_block_on_high_risk")):
        raise ValueError("阻断类高风险策略必须启用高风险阻断开关")
    if stream_mode == "full_buffer" and int(normalized["content_guard_stream_buffer_max_bytes"]) < int(normalized["content_guard_max_scan_bytes"]):
        raise ValueError("full_buffer 模式下流式缓冲上限不能小于最大扫描字节数")
    validate_content_guard_url_allowlist(normalized.get("content_guard_url_allowlist_json", ""))
    return normalized


def validate_content_guard_url_allowlist(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError("URL 白名单 JSON 格式无效") from exc
            if not isinstance(parsed, list):
                raise ValueError("URL 白名单 JSON 必须是字符串数组")
            items = parsed
        elif stripped.startswith("{"):
            raise ValueError("URL 白名单仅支持 JSON 字符串数组或按行填写域名")
        else:
            items = [item.strip() for item in stripped.replace(",", "\n").splitlines()]
    elif isinstance(value, list):
        items = value
    else:
        raise ValueError("URL 白名单仅支持字符串或字符串数组")

    domains: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise ValueError("URL 白名单只能包含字符串")
        domain = _normalize_domain(item)
        if not domain:
            raise ValueError(f"URL 白名单域名无效：{item}")
        if domain not in domains:
            domains.append(domain)
    return domains


def _normalize_domain(value: str) -> str:
    domain = value.strip().lower().rstrip(".")
    if "://" in domain:
        domain = urlparse(domain).hostname or ""
    domain = domain.removeprefix("www.").strip().lower().rstrip(".")
    if not domain or "." not in domain or len(domain) > 253:
        return ""
    return domain
