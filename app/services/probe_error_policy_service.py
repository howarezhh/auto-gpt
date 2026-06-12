from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProbeErrorPolicy:
    error_code: str
    error_type: str
    error_category: str
    handling_strategy: str
    retryable: bool
    recoverable: bool
    update_allowed: bool
    state_update_policy: str
    frontend_severity: str
    frontend_label: str
    frontend_message: str
    log_level: str

    def to_result_fields(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code,
            "error_type": self.error_type,
            "error_category": self.error_category,
            "handling_strategy": self.handling_strategy,
            "retryable": self.retryable,
            "recoverable": self.recoverable,
            "update_allowed": self.update_allowed,
            "state_update_policy": self.state_update_policy,
            "frontend_severity": self.frontend_severity,
            "frontend_label": self.frontend_label,
            "frontend_message": self.frontend_message,
            "log_level": self.log_level,
        }


class ProbeErrorPolicyService:
    """统一探针错误分类、前端展示和状态沉淀策略。"""

    _POLICIES: dict[str, ProbeErrorPolicy] = {
        "probe_rate_limited": ProbeErrorPolicy(
            error_code="probe_rate_limited",
            error_type="rate_limit",
            error_category="probe_control",
            handling_strategy="等待限频窗口结束后再执行探针；本次结果不写入健康、协议或内容可信沉淀状态。",
            retryable=True,
            recoverable=True,
            update_allowed=False,
            state_update_policy="skip_all_state_updates",
            frontend_severity="warning",
            frontend_label="探针限频",
            frontend_message="探针触发频率限制，等待窗口结束后可重试。",
            log_level="warning",
        ),
        "provider_maintenance_mode": ProbeErrorPolicy(
            error_code="provider_maintenance_mode",
            error_type="maintenance",
            error_category="probe_control",
            handling_strategy="维护模式下跳过自动探针；等待维护结束后再检测，不写入健康、协议或内容可信沉淀状态。",
            retryable=True,
            recoverable=True,
            update_allowed=False,
            state_update_policy="skip_all_state_updates",
            frontend_severity="info",
            frontend_label="维护跳过",
            frontend_message="提供商处于维护模式，本次自动探针已跳过。",
            log_level="info",
        ),
        "probe_config_invalid": ProbeErrorPolicy(
            error_code="probe_config_invalid",
            error_type="configuration",
            error_category="local_validation",
            handling_strategy="提示修正探针配置或表单参数；不发起上游请求，不写入沉淀状态。",
            retryable=False,
            recoverable=False,
            update_allowed=False,
            state_update_policy="skip_all_state_updates",
            frontend_severity="error",
            frontend_label="配置错误",
            frontend_message="探针配置不完整或不合法，请修正后重新检测。",
            log_level="warning",
        ),
        "probe_target_disabled_or_missing": ProbeErrorPolicy(
            error_code="probe_target_disabled_or_missing",
            error_type="target_unavailable",
            error_category="local_validation",
            handling_strategy="提示启用或重新选择提供商/模型；不重试，不写入沉淀状态。",
            retryable=False,
            recoverable=False,
            update_allowed=False,
            state_update_policy="skip_all_state_updates",
            frontend_severity="error",
            frontend_label="目标不可用",
            frontend_message="提供商或模型不存在、停用或不属于当前提供商。",
            log_level="warning",
        ),
        "probe_capability_skipped": ProbeErrorPolicy(
            error_code="probe_capability_skipped",
            error_type="capability_skipped",
            error_category="local_validation",
            handling_strategy="按能力声明跳过可选探针；必需探针缺失时标记为必需能力缺失，不归因为上游失败。",
            retryable=False,
            recoverable=False,
            update_allowed=False,
            state_update_policy="skip_all_state_updates",
            frontend_severity="info",
            frontend_label="能力跳过",
            frontend_message="当前能力或协议未启用，探针已按配置跳过。",
            log_level="info",
        ),
        "upstream_auth_error": ProbeErrorPolicy(
            error_code="upstream_auth_error",
            error_type="auth",
            error_category="upstream_rejected",
            handling_strategy="提示检查提供商密钥、权限或模型授权；不做等待重试，不把协议或内容可信沉淀为不支持。",
            retryable=False,
            recoverable=False,
            update_allowed=False,
            state_update_policy="health_may_fail_skip_protocol_and_trust",
            frontend_severity="error",
            frontend_label="鉴权失败",
            frontend_message="上游拒绝鉴权或权限不足，请检查密钥与模型授权。",
            log_level="warning",
        ),
        "upstream_quota_or_billing": ProbeErrorPolicy(
            error_code="upstream_quota_or_billing",
            error_type="quota_or_billing",
            error_category="upstream_rejected",
            handling_strategy="提示充值、开通订阅或等待额度恢复；可恢复但不写协议不支持或内容可信失败。",
            retryable=True,
            recoverable=True,
            update_allowed=False,
            state_update_policy="health_may_fail_skip_protocol_and_trust",
            frontend_severity="warning",
            frontend_label="额度不足",
            frontend_message="上游额度、余额或订阅状态不足，恢复后可重新检测。",
            log_level="warning",
        ),
        "upstream_rate_limited": ProbeErrorPolicy(
            error_code="upstream_rate_limited",
            error_type="rate_limit",
            error_category="upstream_transient",
            handling_strategy="等待上游限流窗口结束后重试；不写能力不支持或内容可信失败。",
            retryable=True,
            recoverable=True,
            update_allowed=False,
            state_update_policy="health_may_fail_skip_protocol_and_trust",
            frontend_severity="warning",
            frontend_label="上游限流",
            frontend_message="上游返回限流，稍后可重试。",
            log_level="warning",
        ),
        "upstream_timeout": ProbeErrorPolicy(
            error_code="upstream_timeout",
            error_type="timeout",
            error_category="upstream_transient",
            handling_strategy="按探针类型执行一次性重试或提示稍后重试；不沉淀协议不支持或内容可信失败。",
            retryable=True,
            recoverable=True,
            update_allowed=False,
            state_update_policy="health_may_fail_skip_protocol_and_trust",
            frontend_severity="warning",
            frontend_label="上游超时",
            frontend_message="连接、读取或首包等待超时，可能是上游临时不可用。",
            log_level="warning",
        ),
        "upstream_network_error": ProbeErrorPolicy(
            error_code="upstream_network_error",
            error_type="network",
            error_category="upstream_transient",
            handling_strategy="提示检查网络、DNS、TLS 或上游地址；不写协议不支持或内容可信失败。",
            retryable=True,
            recoverable=True,
            update_allowed=False,
            state_update_policy="health_may_fail_skip_protocol_and_trust",
            frontend_severity="warning",
            frontend_label="网络异常",
            frontend_message="探针连接上游失败，可能是网络、DNS、TLS 或地址异常。",
            log_level="warning",
        ),
        "upstream_5xx_unavailable": ProbeErrorPolicy(
            error_code="upstream_5xx_unavailable",
            error_type="upstream_unavailable",
            error_category="upstream_transient",
            handling_strategy="视为上游临时故障，可稍后重试；不写协议不支持或内容可信失败。",
            retryable=True,
            recoverable=True,
            update_allowed=False,
            state_update_policy="health_may_fail_skip_protocol_and_trust",
            frontend_severity="warning",
            frontend_label="上游故障",
            frontend_message="上游返回 5xx 或服务不可用，稍后可重试。",
            log_level="warning",
        ),
        "endpoint_explicit_unsupported": ProbeErrorPolicy(
            error_code="endpoint_explicit_unsupported",
            error_type="endpoint_unsupported",
            error_category="capability_or_protocol",
            handling_strategy="仅当错误明确指向端点或协议不支持时，协议检测可沉淀 unsupported；普通健康探针只显示能力不支持。",
            retryable=False,
            recoverable=False,
            update_allowed=True,
            state_update_policy="protocol_detection_may_mark_unsupported",
            frontend_severity="error",
            frontend_label="端点不支持",
            frontend_message="上游明确表示该端点、协议或路径不支持。",
            log_level="info",
        ),
        "model_or_capability_not_supported": ProbeErrorPolicy(
            error_code="model_or_capability_not_supported",
            error_type="capability_unsupported",
            error_category="capability_or_protocol",
            handling_strategy="按探针类型沉淀能力不支持或模型异常；不做等待重试。",
            retryable=False,
            recoverable=False,
            update_allowed=True,
            state_update_policy="capability_probe_may_mark_unsupported",
            frontend_severity="error",
            frontend_label="能力不支持",
            frontend_message="模型不存在或该模型不支持当前能力。",
            log_level="info",
        ),
        "invalid_upstream_response": ProbeErrorPolicy(
            error_code="invalid_upstream_response",
            error_type="invalid_response",
            error_category="upstream_response",
            handling_strategy="记录上游响应摘要用于排查；协议状态保持未知，除非响应明确说明不支持。",
            retryable=False,
            recoverable=False,
            update_allowed=False,
            state_update_policy="keep_protocol_unknown_skip_trust_failure",
            frontend_severity="error",
            frontend_label="响应异常",
            frontend_message="上游返回结构不符合预期，需查看原始响应摘要。",
            log_level="warning",
        ),
        "transport_decompression_error": ProbeErrorPolicy(
            error_code="transport_decompression_error",
            error_type="transport",
            error_category="upstream_response",
            handling_strategy="记录压缩头异常并继续使用 Accept-Encoding: identity；不写内容可信失败或协议不支持。",
            retryable=True,
            recoverable=True,
            update_allowed=False,
            state_update_policy="skip_protocol_and_trust_state",
            frontend_severity="warning",
            frontend_label="压缩异常",
            frontend_message="上游响应压缩头与实际内容不一致，本次不判定内容污染。",
            log_level="warning",
        ),
        "stream_empty_or_invalid_sse": ProbeErrorPolicy(
            error_code="stream_empty_or_invalid_sse",
            error_type="stream_protocol",
            error_category="upstream_response",
            handling_strategy="记录 SSE 事件、控制行或空流问题；按手动结果展示，不直接写内容可信失败。",
            retryable=False,
            recoverable=False,
            update_allowed=False,
            state_update_policy="manual_review_required",
            frontend_severity="error",
            frontend_label="SSE 异常",
            frontend_message="流式响应为空、SSE 结构异常或终止符后存在额外内容。",
            log_level="warning",
        ),
        "content_integrity_violation": ProbeErrorPolicy(
            error_code="content_integrity_violation",
            error_type="content_integrity",
            error_category="content_guard",
            handling_strategy="内容防护探针可写入可信/内容完整性状态；普通健康探针不得用健康字段替代内容风险。",
            retryable=False,
            recoverable=False,
            update_allowed=True,
            state_update_policy="content_guard_may_update_trust_state",
            frontend_severity="error",
            frontend_label="内容风险",
            frontend_message="探针发现内容污染、固定答案不一致或内容完整性异常。",
            log_level="warning",
        ),
        "probe_internal_exception": ProbeErrorPolicy(
            error_code="probe_internal_exception",
            error_type="internal",
            error_category="system_error",
            handling_strategy="记录内部异常并提示排查系统代码或配置；不更新上游沉淀状态。",
            retryable=False,
            recoverable=False,
            update_allowed=False,
            state_update_policy="skip_all_state_updates",
            frontend_severity="error",
            frontend_label="内部异常",
            frontend_message="探针执行时出现系统内部异常。",
            log_level="error",
        ),
        "client_cancelled": ProbeErrorPolicy(
            error_code="client_cancelled",
            error_type="client_cancelled",
            error_category="client",
            handling_strategy="客户端取消不计入上游失败，不更新健康、协议或内容可信沉淀状态。",
            retryable=False,
            recoverable=True,
            update_allowed=False,
            state_update_policy="skip_all_state_updates",
            frontend_severity="info",
            frontend_label="客户端取消",
            frontend_message="客户端已取消探针请求，本次不计入上游失败。",
            log_level="info",
        ),
        "probe_failed": ProbeErrorPolicy(
            error_code="probe_failed",
            error_type="unknown",
            error_category="unknown",
            handling_strategy="保留原始错误摘要并人工确认；默认不写协议或内容可信沉淀状态。",
            retryable=True,
            recoverable=True,
            update_allowed=False,
            state_update_policy="manual_review_required",
            frontend_severity="warning",
            frontend_label="探针失败",
            frontend_message="探针失败但原因未能明确分类，请查看详情。",
            log_level="warning",
        ),
    }

    _AUTH_HINTS = ("invalid api key", "invalid_api_key", "unauthorized", "permission_denied", "forbidden", "无权限", "鉴权", "认证", "权限")
    _QUOTA_HINTS = ("insufficient_quota", "insufficient balance", "quota", "billing", "paid_model_required", "subscription", "余额", "额度", "欠费", "订阅")
    _RATE_LIMIT_HINTS = ("rate limit", "rate_limit", "rate_limit_exceeded", "too many requests", "限流", "频率")
    _TIMEOUT_HINTS = ("timeout", "timed out", "first token", "idle timeout", "超时")
    _NETWORK_HINTS = ("connection", "connect error", "network", "dns", "tls", "ssl", "reset", "refused", "unreachable", "网络", "连接")
    _UNSUPPORTED_ENDPOINT_HINTS = ("not found", "404", "method not allowed", "cannot post", "unknown endpoint", "no route", "unsupported endpoint", "端点", "路径")
    _UNSUPPORTED_MODEL_HINTS = ("model_not_found", "model not found", "does not support", "unsupported", "not implemented", "不支持", "模型不存在")
    _INVALID_RESPONSE_HINTS = ("invalid json", "bad_response_status_code", "schema", "malformed", "非 json", "结构", "格式")
    _DECOMPRESSION_HINTS = ("incorrect header check", "decompress", "content-encoding", "gzip")
    _SSE_CATEGORY_HINTS = ("invalid_sse_stream", "sse_tail_pollution", "sse_fixed_answer_mismatch", "sse_probe_exception")
    _CONTENT_CATEGORY_HINTS = (
        "content_probe_",
        "pollution_probe_",
        "combined_text_probe_",
        "fixed_answer_",
        "vision_probe_",
        "sse_fixed_answer_mismatch",
        "sse_tail_pollution",
        "content_trust_",
    )

    @classmethod
    def policy_for_code(cls, error_code: str | None) -> ProbeErrorPolicy:
        return cls._POLICIES.get(str(error_code or "").strip()) or cls._POLICIES["probe_failed"]

    @classmethod
    def classify_result(cls, result: dict[str, Any], *, probe_kind: str = "health") -> dict[str, Any]:
        if not isinstance(result, dict):
            return {}
        if result.get("success") is True:
            return {
                "retryable": False,
                "recoverable": True,
                "update_allowed": bool(result.get("update_allowed", True)),
                "state_update_policy": "success_may_update_state",
            }
        guard = result.get("content_guard") if isinstance(result.get("content_guard"), dict) else {}
        existing_code = result.get("error_code")
        category = (
            result.get("category")
            or guard.get("content_guard_categories_json")
            or guard.get("categories")
        )
        policy = cls.classify(
            status_code=result.get("status_code"),
            message=result.get("message") or result.get("support_label"),
            detail=result.get("error_detail"),
            support_mode=result.get("support_mode"),
            category=category,
            existing_error_code=existing_code,
            retryable=result.get("retryable"),
            probe_kind=probe_kind,
        )
        return policy.to_result_fields()

    @classmethod
    def classify(
        cls,
        *,
        status_code: int | None = None,
        message: Any = None,
        detail: Any = None,
        exception: Exception | None = None,
        support_mode: str | None = None,
        category: Any = None,
        existing_error_code: str | None = None,
        retryable: bool | None = None,
        probe_kind: str = "health",
    ) -> ProbeErrorPolicy:
        code = cls._select_code(
            status_code=status_code,
            message=message,
            detail=detail,
            exception=exception,
            support_mode=support_mode,
            category=category,
            existing_error_code=existing_error_code,
        )
        policy = cls.policy_for_code(code)
        if retryable is None or retryable == policy.retryable:
            return policy
        return ProbeErrorPolicy(
            error_code=policy.error_code,
            error_type=policy.error_type,
            error_category=policy.error_category,
            handling_strategy=policy.handling_strategy,
            retryable=bool(retryable),
            recoverable=policy.recoverable or bool(retryable),
            update_allowed=policy.update_allowed,
            state_update_policy=policy.state_update_policy,
            frontend_severity=policy.frontend_severity,
            frontend_label=policy.frontend_label,
            frontend_message=policy.frontend_message,
            log_level=policy.log_level,
        )

    @classmethod
    def annotate_result(cls, result: dict[str, Any], *, probe_kind: str = "health") -> dict[str, Any]:
        if not isinstance(result, dict):
            return result
        fields = cls.classify_result(result, probe_kind=probe_kind)
        for key, value in fields.items():
            if key in {"retryable", "update_allowed"}:
                result[key] = value
            else:
                result.setdefault(key, value)
        if not result.get("log_fields"):
            result["log_fields"] = cls.log_fields(result)
        return result

    @classmethod
    def log_fields(cls, result: dict[str, Any]) -> dict[str, Any]:
        return {
            "error_code": result.get("error_code"),
            "error_type": result.get("error_type"),
            "error_category": result.get("error_category"),
            "handling_strategy": result.get("handling_strategy"),
            "retryable": result.get("retryable"),
            "recoverable": result.get("recoverable"),
            "update_allowed": result.get("update_allowed"),
            "state_update_policy": result.get("state_update_policy"),
            "frontend_label": result.get("frontend_label"),
            "status_code": result.get("status_code"),
            "support_mode": result.get("support_mode"),
        }

    @classmethod
    def _select_code(
        cls,
        *,
        status_code: int | None,
        message: Any,
        detail: Any,
        exception: Exception | None,
        support_mode: str | None,
        category: Any,
        existing_error_code: str | None,
    ) -> str:
        existing = str(existing_error_code or "").strip()
        if existing in cls._POLICIES and existing != "probe_failed":
            return existing
        mode = str(support_mode or "").strip()
        if mode == "probe_rate_limited":
            return "probe_rate_limited"
        if mode == "provider_maintenance_mode":
            return "provider_maintenance_mode"
        if mode in {"skipped", "required_missing"}:
            return "probe_capability_skipped"
        if mode == "timeout":
            return "upstream_timeout"
        text = cls._normalize_text(message, detail, exception, category)
        category_text = cls._normalize_text(category)
        if any(hint in text for hint in cls._DECOMPRESSION_HINTS):
            return "transport_decompression_error"
        if any(hint in category_text for hint in cls._SSE_CATEGORY_HINTS):
            return "stream_empty_or_invalid_sse"
        if any(hint in category_text for hint in cls._CONTENT_CATEGORY_HINTS):
            if any(hint in category_text for hint in ("exception", "timeout", "request_failed", "http_failure", "transport")):
                pass
            else:
                return "content_integrity_violation"
        if status_code is not None:
            try:
                status_value = int(status_code)
            except (TypeError, ValueError):
                status_value = None
            if status_value in {401, 403}:
                return "upstream_auth_error"
            if status_value == 408:
                return "upstream_timeout"
            if status_value == 429:
                return "upstream_rate_limited"
            if status_value in {404, 405}:
                return "endpoint_explicit_unsupported"
            if status_value is not None and status_value >= 500:
                return "upstream_5xx_unavailable"
        if any(hint in text for hint in cls._AUTH_HINTS):
            return "upstream_auth_error"
        if any(hint in text for hint in cls._QUOTA_HINTS):
            return "upstream_quota_or_billing"
        if any(hint in text for hint in cls._RATE_LIMIT_HINTS):
            return "upstream_rate_limited"
        if any(hint in text for hint in cls._TIMEOUT_HINTS):
            return "upstream_timeout"
        if any(hint in text for hint in cls._NETWORK_HINTS):
            return "upstream_network_error"
        if any(hint in text for hint in cls._UNSUPPORTED_ENDPOINT_HINTS):
            return "endpoint_explicit_unsupported"
        if any(hint in text for hint in cls._UNSUPPORTED_MODEL_HINTS):
            return "model_or_capability_not_supported"
        if any(hint in text for hint in cls._INVALID_RESPONSE_HINTS):
            return "invalid_upstream_response"
        if isinstance(exception, (ValueError, TypeError, AttributeError, KeyError)):
            return "probe_internal_exception"
        if mode == "unsupported":
            return "model_or_capability_not_supported"
        return "probe_failed"

    @staticmethod
    def _normalize_text(*values: Any) -> str:
        parts: list[str] = []
        for value in values:
            if value is None:
                continue
            if isinstance(value, (list, tuple, set)):
                parts.extend(ProbeErrorPolicyService._normalize_text(item) for item in value)
                continue
            if isinstance(value, dict):
                try:
                    parts.append(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
                except Exception:
                    parts.append(str(value))
                continue
            parts.append(str(value))
        return " ".join(part for part in parts if part).strip().lower()
