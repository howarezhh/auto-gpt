from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.services.content_guard_service import ContentGuardResult, ContentGuardService
from app.services.native_protocol_adapter import NativeProtocolAdapter
from app.services.probe_error_policy_service import ProbeErrorPolicyService
from app.services.probe_rate_limit_service import ProbeRateLimitService
from app.services.provider_service import ProviderService
from app.services.setting_service import SettingService
from app.utils.json_utils import dumps_json, safeJsonParse


def _proxy_service():
    from app.services.proxy_service import ProxyService

    return ProxyService


def _stream_timeout_policy_cls():
    from app.services.proxy_service import StreamTimeoutPolicy

    return StreamTimeoutPolicy


class ContentGuardProbeService:
    """内容完整性探针、检测结果与内容完整性状态维护。"""

    FIXED_ANSWER = "AOTU_CONTENT_GUARD_OK"
    FIXED_ANSWER_JSON_FIELDS = frozenset({"marker", "answer", "text", "content", "output"})
    JSON_EXPECTED = {"status": "ok", "marker": "AOTU_CONTENT_GUARD_OK"}
    VISION_EXPECTED = "红色"
    VISION_PROBE_IMAGE_DATA_URL = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP4z8AARAwQCgAf7gP9i18U1AAAAABJRU5ErkJggg=="
    )
    PROBE_PHASE_KEYS = frozenset(
        {
            "content_fixed_answer",
            "content_pollution_rules",
            "content_json",
            "content_sse",
            "content_vision",
        }
    )
    TRUST_PROBE_KEYS = frozenset({"content_fixed_answer", "content_pollution_rules", "content_sse"})
    RESULT_PASS = ContentGuardService.RESULT_PASS
    STREAM_CONNECT_TIMEOUT_SECONDS = 4
    STREAM_FIRST_TOKEN_TIMEOUT_SECONDS = 4
    PROBE_FAILURE_WINDOW_SECONDS = 600
    SSE_PROBE_MAX_CHUNKS = 64
    SSE_PROBE_MAX_BYTES = 65536
    SSE_PROBE_MAX_DURATION_SECONDS = 12
    SSE_PROBE_IDLE_TIMEOUT_SECONDS = 4
    POLLUTION_PROBE_TIMEOUT_SECONDS = 10
    POLLUTION_PROBE_MAX_SCENARIOS = 4
    COMBINED_TEXT_PROBE_TIMEOUT_SECONDS = 12
    RAW_PROVIDER_RESPONSE_MAX_CHARS = 20000
    DETECTION_TRAFFIC_TYPE = "content_guard_probe"

    @staticmethod
    def provider_probe_timeout_seconds(provider: Provider | Any, fallback_seconds: float) -> float:
        timeout_ms = getattr(provider, "timeout_ms", None)
        try:
            timeout_seconds = float(timeout_ms or 0) / 1000.0
        except (TypeError, ValueError):
            timeout_seconds = 0.0
        return max(1.0, timeout_seconds) if timeout_seconds > 0 else float(fallback_seconds)

    @staticmethod
    def provider_stream_connect_timeout_seconds(provider: Provider | Any) -> float:
        return ContentGuardProbeService.provider_probe_timeout_seconds(
            provider,
            ContentGuardProbeService.STREAM_CONNECT_TIMEOUT_SECONDS,
        )

    @staticmethod
    def provider_stream_first_token_timeout_seconds(provider: Provider | Any) -> float:
        first_token_timeout = getattr(provider, "first_token_timeout_sec", None)
        try:
            first_token_timeout_seconds = float(first_token_timeout or 0)
        except (TypeError, ValueError):
            first_token_timeout_seconds = 0.0
        if first_token_timeout_seconds > 0:
            return max(1.0, first_token_timeout_seconds)
        return float(ContentGuardProbeService.STREAM_FIRST_TOKEN_TIMEOUT_SECONDS)

    @staticmethod
    def content_probe_endpoint_path(provider: Provider, provider_model: ProviderModel) -> str | None:
        native_protocol = ProviderService.provider_or_model_native_protocol(provider, provider_model)
        if native_protocol:
            return f"/native/{native_protocol}"
        chat_supported = ProviderService.provider_supports_chat_completions(provider) and bool(getattr(provider_model, "supports_chat_completions", False))
        responses_supported = ProviderService.provider_supports_responses(provider) and bool(getattr(provider_model, "supports_responses", False))
        probe_protocol_type = ContentGuardProbeService._content_guard_probe_protocol_type()

        if probe_protocol_type == "responses":
            if responses_supported:
                return "/responses"
            if chat_supported:
                return "/chat/completions"
            return None
        if chat_supported:
            return "/chat/completions"
        if responses_supported:
            return "/responses"
        return None

    @staticmethod
    def _content_guard_probe_protocol_type() -> str:
        try:
            value = str(getattr(SettingService.get_cached(), "content_guard_probe_protocol_type", "") or "")
        except Exception:
            value = ""
        if value in {"chat_completions", "responses"}:
            return value
        return "chat_completions"

    @staticmethod
    def _native_protocol_from_probe_endpoint(endpoint_path: str) -> str | None:
        text = str(endpoint_path or "").strip()
        if not text.startswith("/native/"):
            return None
        protocol_type = text.removeprefix("/native/")
        return protocol_type if protocol_type in NativeProtocolAdapter.NATIVE_PROTOCOLS else None

    @staticmethod
    def build_health_phase_specs(
        provider: Provider,
        *,
        should_test_endpoint: Callable[[ProviderModel, str], bool],
        include_json_probe: bool = False,
    ) -> list[dict[str, Any]]:
        """构造健康检测可复用的内容完整性阶段，具体探针仍由内容防护模块负责。"""
        phases = [
            {
                "key": "content_fixed_answer",
                "label": "固定答案完整性探针",
                "targets": lambda model: bool(
                    should_test_endpoint(model, "native") or should_test_endpoint(model, "chat") or should_test_endpoint(model, "responses")
                ),
                "probes": [
                    {
                        "key": "content_fixed_answer",
                        "probe": lambda model: ContentGuardProbeService.probe_fixed_answer(
                            provider,
                            model,
                            endpoint_path=ContentGuardProbeService.content_probe_endpoint_path(provider, model) or "/chat/completions",
                        ),
                    }
                ],
            },
            {
                "key": "content_pollution_rules",
                "label": "外链广告识别探针",
                "targets": lambda model: bool(
                    should_test_endpoint(model, "native") or should_test_endpoint(model, "chat") or should_test_endpoint(model, "responses")
                ),
                "probes": [
                    {
                        "key": "content_pollution_rules",
                        "probe": lambda model: ContentGuardProbeService.probe_pollution_rules(
                            provider,
                            model,
                            endpoint_path=ContentGuardProbeService.content_probe_endpoint_path(provider, model) or "/chat/completions",
                        ),
                    }
                ],
            },
            {
                "key": "content_sse",
                "label": "流式污染检测探针",
                "targets": lambda model: bool(
                    model.supports_stream
                    and (should_test_endpoint(model, "native") or should_test_endpoint(model, "chat") or should_test_endpoint(model, "responses"))
                ),
                "probes": [
                    {
                        "key": "content_sse",
                        "probe": lambda model: ContentGuardProbeService.probe_sse(
                            provider,
                            model,
                            endpoint_path=ContentGuardProbeService.content_probe_endpoint_path(provider, model) or "/chat/completions",
                        ),
                    }
                ],
            },
        ]
        if include_json_probe:
            phases.insert(
                1,
                {
                    "key": "content_json",
                    "label": "严格 JSON 完整性探针",
                    "targets": lambda model: bool(
                        should_test_endpoint(model, "chat") or should_test_endpoint(model, "responses")
                        or should_test_endpoint(model, "native")
                    ),
                    "probes": [
                        {
                            "key": "content_json",
                            "probe": lambda model: ContentGuardProbeService.probe_json(
                                provider,
                                model,
                                endpoint_path=ContentGuardProbeService.content_probe_endpoint_path(provider, model) or "/chat/completions",
                            ),
                        }
                    ],
                },
            )
        return phases

    @staticmethod
    async def send_content_probe_json(
        provider: Provider,
        provider_model: ProviderModel,
        *,
        endpoint_path: str,
        payload: dict[str, Any],
        endpoint_label: str,
    ) -> tuple[dict[str, Any] | None, int, int | None, list[dict[str, Any]], dict[str, Any] | None]:
        ProxyService = _proxy_service()
        started = time.perf_counter()
        limit_result = await ProbeRateLimitService.claim(
            provider,
            provider_model,
            probe_type=f"content_guard_json_{endpoint_path.strip('/').replace('/', '_') or 'endpoint'}",
        )
        if not limit_result.allowed:
            return None, 0, 429, [], ContentGuardProbeService.mark_detection_result(
                ProbeRateLimitService.rate_limited_probe_result(
                    limit_result,
                    endpoint_path=endpoint_path,
                    endpoint_label=endpoint_label,
                    support_label="内容防护探针已限频",
                )
            )
        setting = await ProxyService._get_setting_async()
        try:
            native_protocol = ContentGuardProbeService._native_protocol_from_probe_endpoint(endpoint_path)
            if native_protocol:
                from app.services.proxy_service import PreparedUpstreamRequest

                native_model_name = ProviderService.provider_model_upstream_model_name(provider_model)
                native_path = NativeProtocolAdapter.request_path(
                    native_protocol,
                    native_model_name,
                    endpoint_path_template=(
                        getattr(provider_model, "native_endpoint_path", None)
                        or getattr(provider, "native_endpoint_path", None)
                    ),
                )
                prepared = PreparedUpstreamRequest(
                    request_path=native_path,
                    request_payload=payload,
                    public_endpoint_path=endpoint_path,
                    upstream_protocol_type=native_protocol,
                    response_model_override=native_model_name,
                )
                response, _ = await ProxyService._send_prepared_json(
                    provider,
                    prepared=prepared,
                    headers={"Accept-Encoding": "identity"},
                    requested_payload={"model": native_model_name},
                    setting=setting,
                )
                fallback_trace = [
                    {
                        "result": "native_content_guard_probe",
                        "protocol_type": native_protocol,
                        "endpoint": native_path,
                        "is_detection_traffic": True,
                    }
                ]
            else:
                response, _, fallback_trace = await ProxyService._forward_json_with_endpoint_fallback(
                    provider,
                    provider_model,
                    endpoint_path,
                    payload,
                    started=started,
                    setting=setting,
                    extra_headers={"Accept-Encoding": "identity"},
                )
            return (
                response,
                int((time.perf_counter() - started) * 1000),
                200,
                ContentGuardProbeService.mark_detection_trace(
                    fallback_trace,
                    endpoint_path=endpoint_path,
                    endpoint_label=endpoint_label,
                ),
                None,
            )
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            ProxyService = _proxy_service()
            error_body = await ProxyService._extract_response_error(exc.response)
            detail = ProxyService._normalize_error_detail(error_body)
            message = ProxyService._error_message_for_log(detail)
            guard_result = ContentGuardProbeService.review_result(
                message or f"{endpoint_label} HTTP 请求失败",
                category="content_probe_http_failure",
            )
            return None, int((time.perf_counter() - started) * 1000), status_code, [], ContentGuardProbeService.mark_detection_result({
                "endpoint_path": endpoint_path,
                "endpoint_label": endpoint_label,
                "success": False,
                "native_success": False,
                "adapted_success": False,
                "support_mode": "probe_failed",
                "support_label": f"{endpoint_label} 请求失败",
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "status_code": status_code,
                "message": message,
                "trace": [],
                "retryable": status_code in {408, 429, 500, 502, 503, 504},
                "error_detail": detail,
                "raw_provider_response": {
                    "endpoint_path": endpoint_path,
                    "endpoint_label": endpoint_label,
                    "status_code": status_code,
                    "body": ContentGuardProbeService.compact_raw_provider_value(error_body),
                    "normalized_error": ContentGuardProbeService.compact_raw_provider_value(detail),
                },
                "content_guard": ContentGuardProbeService.serialize_guard_result(guard_result),
            })
        except Exception as exc:
            status_code = getattr(exc, "status_code", None)
            detail = getattr(exc, "detail", None)
            message = ProxyService._error_message_for_log(detail) if detail is not None else str(exc)
            decompression_error = ContentGuardProbeService._is_transport_decompression_error(message)
            guard_result = ContentGuardProbeService.review_result(
                (
                    f"上游响应压缩头与实际内容不一致：{message}；探针已请求 Accept-Encoding: identity，"
                    "本次不作为内容可信失败写入。"
                )
                if decompression_error
                else (message or f"{endpoint_label} 网络请求失败"),
                category="content_probe_transport_decompression_error" if decompression_error else "content_probe_request_failure",
            )
            return None, int((time.perf_counter() - started) * 1000), status_code, [], ContentGuardProbeService.mark_detection_result({
                "endpoint_path": endpoint_path,
                "endpoint_label": endpoint_label,
                "success": False,
                "native_success": False,
                "adapted_success": False,
                "support_mode": "probe_failed",
                "support_label": "上游压缩响应异常，未判定内容污染" if decompression_error else f"{endpoint_label} 请求失败",
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "status_code": status_code,
                "message": guard_result.reason,
                "trace": [],
                "retryable": bool(decompression_error),
                "raw_provider_response": {
                    "endpoint_path": endpoint_path,
                    "endpoint_label": endpoint_label,
                    "status_code": status_code,
                    "error": message,
                },
                "content_guard": ContentGuardProbeService.serialize_guard_result(guard_result),
            })

    @staticmethod
    def detection_trace_marker(*, endpoint_path: str, endpoint_label: str) -> dict[str, Any]:
        return {
            "result": "content_guard_detection_traffic",
            "endpoint": endpoint_path,
            "endpoint_label": endpoint_label,
            "traffic_type": ContentGuardProbeService.DETECTION_TRAFFIC_TYPE,
            "is_detection_traffic": True,
        }

    @staticmethod
    def mark_detection_trace(
        trace: list[dict[str, Any]] | None,
        *,
        endpoint_path: str,
        endpoint_label: str,
    ) -> list[dict[str, Any]]:
        marked_trace: list[dict[str, Any]] = []
        for item in trace or []:
            if not isinstance(item, dict):
                continue
            marked = dict(item)
            marked.setdefault("traffic_type", ContentGuardProbeService.DETECTION_TRAFFIC_TYPE)
            marked.setdefault("is_detection_traffic", True)
            marked_trace.append(marked)
        if not any(item.get("result") == "content_guard_detection_traffic" for item in marked_trace):
            marked_trace.append(
                ContentGuardProbeService.detection_trace_marker(endpoint_path=endpoint_path, endpoint_label=endpoint_label)
            )
        return marked_trace

    @staticmethod
    def mark_detection_result(result: dict[str, Any]) -> dict[str, Any]:
        result["traffic_type"] = ContentGuardProbeService.DETECTION_TRAFFIC_TYPE
        result["is_detection_traffic"] = True
        ProbeErrorPolicyService.annotate_result(result, probe_kind="content_guard")
        trace = result.get("trace") if isinstance(result.get("trace"), list) else []
        result["trace"] = ContentGuardProbeService.mark_detection_trace(
            trace,
            endpoint_path=str(result.get("endpoint_path") or ""),
            endpoint_label=str(result.get("endpoint_label") or ""),
        )
        return result

    @staticmethod
    def compact_raw_provider_value(value: Any, *, max_chars: int | None = None) -> Any:
        limit = max(1024, int(max_chars or ContentGuardProbeService.RAW_PROVIDER_RESPONSE_MAX_CHARS))
        try:
            text = dumps_json(value)
        except Exception:
            text = str(value)
        if len(text) <= limit:
            return value
        return {
            "truncated": True,
            "preview": text[:limit],
            "original_chars": len(text),
        }

    @staticmethod
    def _safe_probe_reason_text(value: Any, *, max_chars: int = 500) -> str:
        if value is None:
            return ""
        if isinstance(value, dict):
            error = value.get("error")
            if isinstance(error, dict) and error.get("message"):
                text = str(error.get("message") or "")
            elif value.get("message"):
                text = str(value.get("message") or "")
            else:
                try:
                    text = dumps_json(value)
                except Exception:
                    text = str(value)
        else:
            text = str(value)
        text = re.sub(r"(?i)(authorization|api[-_ ]?key|token|password|secret)(['\"\s:=]+)[^,'\"\s}]+", r"\1\2***", text)
        text = re.sub(r"(?i)bearer\s+[a-z0-9._\-]+", "Bearer ***", text)
        text = " ".join(text.split())
        if len(text) > max_chars:
            return f"{text[:max_chars]}..."
        return text

    @staticmethod
    def content_probe_failure_reason(error_result: dict[str, Any] | None, *, fallback: str) -> str:
        if not isinstance(error_result, dict):
            return fallback
        generic_messages = {
            "",
            "Upstream request failed",
            "upstream request failed",
            "文本内容完整性组合探针请求失败",
            "外链广告识别组合探针请求失败",
        }
        candidates: list[str] = []
        raw = error_result.get("raw_provider_response")
        if isinstance(error_result.get("error_detail"), (dict, str)):
            candidates.append(ContentGuardProbeService._safe_probe_reason_text(error_result.get("error_detail")))
        if isinstance(raw, dict):
            for key in ("normalized_error", "body", "error", "message"):
                candidates.append(ContentGuardProbeService._safe_probe_reason_text(raw.get(key)))
        candidates.append(ContentGuardProbeService._safe_probe_reason_text(error_result.get("message")))
        candidates.append(ContentGuardProbeService._safe_probe_reason_text(error_result.get("reason")))
        for candidate in candidates:
            if candidate and candidate not in generic_messages:
                return candidate
        status_code = error_result.get("status_code")
        if status_code is None and isinstance(raw, dict):
            status_code = raw.get("status_code")
        code = error_result.get("error_code") or error_result.get("support_mode")
        if isinstance(error_result.get("error_detail"), dict):
            code = error_result["error_detail"].get("code") or code
        parts = [fallback]
        if status_code is not None:
            parts.append(f"状态码 {status_code}")
        if code:
            parts.append(f"错误码 {code}")
        return "；".join(parts)

    @staticmethod
    def attach_raw_provider_response(
        result: dict[str, Any],
        *,
        response: Any = None,
        output_text: str | None = None,
        stream_events: list[str] | None = None,
        stream_chunks: list[str] | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        raw: dict[str, Any] = {
            "endpoint_path": result.get("endpoint_path"),
            "endpoint_label": result.get("endpoint_label"),
            "status_code": result.get("status_code"),
        }
        if response is not None:
            raw["body"] = ContentGuardProbeService.compact_raw_provider_value(response)
        if output_text is not None:
            raw["output_text"] = ContentGuardProbeService.compact_raw_provider_value(output_text)
        if stream_events is not None:
            raw["stream_events"] = ContentGuardProbeService.compact_raw_provider_value(stream_events)
        if stream_chunks is not None:
            raw["stream_chunks"] = ContentGuardProbeService.compact_raw_provider_value(stream_chunks)
        if note:
            raw["note"] = note
        if any(key in raw for key in ("body", "output_text", "stream_events", "stream_chunks", "note")):
            result["raw_provider_response"] = raw
        return result

    @staticmethod
    def serialize_guard_result(guard_result: ContentGuardResult) -> dict[str, Any]:
        return {
            **guard_result.to_log_kwargs(),
            "matched_rules": list(guard_result.matched_rules or []),
        }

    @staticmethod
    def probe_success(
        *,
        endpoint_path: str,
        endpoint_label: str,
        support_label: str,
        latency_ms: int,
        status_code: int | None,
        message: str,
        trace: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        trace_items = list(trace or [])
        adapted_success = any(
            isinstance(item, dict)
            and (
                bool(item.get("adapted_success"))
                or item.get("fallback_from")
                or item.get("fallback_to")
                or item.get("adapted_endpoint")
                or item.get("required_endpoint")
            )
            for item in trace_items
        )
        guard_result = ContentGuardResult(
            result=ContentGuardService.RESULT_PASS,
            risk_level="low",
            reason=f"{endpoint_label} 通过",
            action="allow",
        )
        return ContentGuardProbeService.mark_detection_result({
            "endpoint_path": endpoint_path,
            "endpoint_label": endpoint_label,
            "success": True,
            "native_success": not adapted_success,
            "adapted_success": adapted_success,
            "support_mode": "adapted" if adapted_success else "native",
            "support_label": support_label,
            "latency_ms": latency_ms,
            "status_code": status_code,
            "message": message,
            "trace": trace_items,
            "retryable": False,
            "content_guard": ContentGuardProbeService.serialize_guard_result(guard_result),
        })

    @staticmethod
    def summarize_probe_result(endpoint_result: dict[str, Any]) -> dict[str, Any]:
        content_guard = endpoint_result.get("content_guard")
        if not isinstance(content_guard, dict):
            content_guard = {}
        return {
            "phase_key": endpoint_result.get("capability_key") or endpoint_result.get("endpoint_label"),
            "probe_key": endpoint_result.get("probe_key"),
            "endpoint_label": endpoint_result.get("endpoint_label"),
            "endpoint_path": endpoint_result.get("endpoint_path"),
            "support_mode": endpoint_result.get("support_mode"),
            "support_label": endpoint_result.get("support_label"),
            "required_probe": bool(endpoint_result.get("required_probe")),
            "required_missing": bool(endpoint_result.get("required_missing")),
            "success": bool(endpoint_result.get("success")),
            "latency_ms": int(endpoint_result.get("latency_ms") or 0),
            "status_code": endpoint_result.get("status_code"),
            "content_guard_result": content_guard.get("content_guard_result"),
            "content_guard_risk_level": content_guard.get("content_guard_risk_level"),
            "content_guard_categories_json": content_guard.get("content_guard_categories_json"),
            "content_guard_reason": content_guard.get("content_guard_reason"),
            "content_guard_action": content_guard.get("content_guard_action"),
            "content_guard_excerpt": content_guard.get("content_guard_excerpt"),
            "message": endpoint_result.get("message"),
        }

    @staticmethod
    def review_result(reason: str, *, category: str, excerpt: str | None = None) -> ContentGuardResult:
        matched_rule = {
            "id": category,
            "name": reason[:80] or category,
            "category": category,
            "enabled": True,
            "match_type": "probe_review",
            "patterns": [],
            "risk_level": "medium",
            "action": "record",
            "score_delta": -10,
            "confidence": 0.7,
            "reason": reason,
        }
        return ContentGuardResult(
            result=ContentGuardService.RESULT_REVIEW,
            risk_level="medium",
            categories=[category],
            reason=reason,
            action="record",
            excerpt=excerpt,
            score_delta=-10,
            matched_rules=[matched_rule],
        )

    @staticmethod
    def _content_guard_probe_setting_int(name: str, default: int, *, min_value: int = 0, max_value: int | None = None) -> int:
        try:
            setting = SettingService.get_cached()
            value = int(getattr(setting, name, default) or default)
        except Exception:
            value = default
        value = max(min_value, value)
        if max_value is not None:
            value = min(max_value, value)
        return value

    @staticmethod
    def fixed_answer_prompt() -> str:
        return (
            "固定答案完整性检测。你只能输出下一行固定字符串，不能输出其它任何字符。\n"
            f"{ContentGuardProbeService.FIXED_ANSWER}\n"
            "严格要求：\n"
            "1. 输出内容必须与固定字符串逐字符完全一致。\n"
            "2. 不要 Markdown、JSON、引号、标点、空格、换行、解释、前缀或后缀。\n"
            "3. 不要复述规则，不要输出多余文本。"
        )

    @staticmethod
    def build_fixed_answer_payload(provider_model: ProviderModel, *, endpoint_path: str, stream: bool = False) -> dict[str, Any]:
        prompt = ContentGuardProbeService.fixed_answer_prompt()
        native_protocol = ContentGuardProbeService._native_protocol_from_probe_endpoint(endpoint_path)
        if native_protocol:
            model_name = ProviderService.provider_model_upstream_model_name(provider_model)
            return NativeProtocolAdapter.native_text_payload(
                native_protocol,
                model_name=model_name,
                prompt=prompt,
                max_tokens=16,
                stream=stream,
            )
        if endpoint_path == "/chat/completions":
            payload: dict[str, Any] = {
                "model": provider_model.model_name,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 16,
            }
            if stream:
                payload["stream"] = True
            return payload
        payload = {
            "model": provider_model.model_name,
            "input": prompt,
            "temperature": 0,
            "max_output_tokens": 16,
        }
        if stream:
            payload["stream"] = True
        return payload

    @staticmethod
    def build_pollution_probe_payload(provider_model: ProviderModel, *, endpoint_path: str, prompt: str) -> dict[str, Any]:
        native_protocol = ContentGuardProbeService._native_protocol_from_probe_endpoint(endpoint_path)
        if native_protocol:
            model_name = ProviderService.provider_model_upstream_model_name(provider_model)
            return NativeProtocolAdapter.native_text_payload(
                native_protocol,
                model_name=model_name,
                prompt=prompt,
                max_tokens=512,
            )
        if endpoint_path == "/chat/completions":
            return {
                "model": provider_model.model_name,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 512,
            }
        return {
            "model": provider_model.model_name,
            "input": prompt,
            "temperature": 0,
            "max_output_tokens": 512,
        }

    @staticmethod
    def pollution_probe_scenarios() -> list[dict[str, Any]]:
        return [
            {
                "key": "long_context_answer",
                "label": "长上下文回答污染",
                "prompt": (
                    "请用中文分 4 点说明大语言模型代理系统的请求路由流程，每点不超过 25 个字。"
                    "禁止附加外部链接、短链接、优惠码、邀请码、推广语、赞助信息、联系方式或社群引流。"
                ),
            },
            {
                "key": "tool_context_answer",
                "label": "工具上下文回答污染",
                "prompt": (
                    "假设工具返回了健康检查状态 healthy，请用一句中文解释这个状态。"
                    "禁止添加工具调用以外的购买链接、开户链接、客服联系方式、社群邀请或推广内容。"
                ),
            },
            {
                "key": "markdown_answer",
                "label": "Markdown 回答污染",
                "prompt": (
                    "请用 Markdown 列表写出 3 条 API Key 保管建议。"
                    "禁止输出任何外部 URL、二维码、折扣码、课程广告、联系方式、社群链接或赞助语。"
                ),
            },
            {
                "key": "citation_answer",
                "label": "引用格式回答污染",
                "prompt": (
                    "请用中文解释什么是内容完整性检测，并在句末使用“参考：内部策略”作为固定引用。"
                    "禁止添加真实网址、短链、推广引用、广告尾巴、联系方式或社区邀请。"
                ),
            },
        ]

    @staticmethod
    def build_combined_pollution_probe_prompt(scenarios: list[dict[str, Any]]) -> str:
        sections = []
        for scenario in scenarios:
            sections.append(
                f"[[{scenario['key']}]]\n"
                f"{scenario['prompt']}"
            )
        return (
            f"你正在接受内容完整性检测。请一次性完成下面 {len(scenarios)} 个子场景，必须严格按给定分段标记输出。\n"
            "输出要求：\n"
            "1. 每个分段标记必须单独占一行，格式必须是 [[section_key]]。\n"
            "2. 每个分段只回答该分段标记后面的任务，不要解释检测规则。\n"
            "3. 禁止输出分段外内容。\n"
            "4. 所有分段都禁止外部 URL、短链接、优惠码、邀请码、推广语、赞助信息、联系方式、二维码、社群引流或广告尾巴。\n\n"
            + "\n\n".join(sections)
        )

    @staticmethod
    def split_combined_pollution_output(output_text: str, scenarios: list[dict[str, Any]]) -> dict[str, str]:
        keys = [str(scenario.get("key") or "") for scenario in scenarios]
        allowed_keys = {key for key in keys if key}
        if not output_text.strip() or not allowed_keys:
            return {}
        marker_re = re.compile(
            r"(?m)^\s*(?:\[\[\s*(?P<bracket>[a-zA-Z0-9_:-]+)\s*\]\]|\[\s*(?P<single>[a-zA-Z0-9_:-]+)\s*\]|(?P<plain>[a-zA-Z0-9_:-]+)\s*[:：])\s*$"
        )
        matches = [
            match
            for match in marker_re.finditer(output_text)
            if (match.group("bracket") or match.group("single") or match.group("plain") or "").strip() in allowed_keys
        ]
        sections: dict[str, str] = {}
        for index, match in enumerate(matches):
            key = (match.group("bracket") or match.group("single") or match.group("plain") or "").strip()
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(output_text)
            sections[key] = output_text[start:end].strip()
        return sections

    @staticmethod
    def normalize_fixed_answer_probe_text(output_text: str) -> str:
        def prepare_text(value: Any) -> str:
            text = "" if value is None else str(value)
            text = unicodedata.normalize("NFKC", text)
            for char in ("\ufeff", "\u200b", "\u200c", "\u200d", "\u2060"):
                text = text.replace(char, "")
            return text.strip()

        def strip_wrapping_noise(text: str) -> str:
            pairs = {
                '"': '"',
                "'": "'",
                "`": "`",
                "“": "”",
                "‘": "’",
                "「": "」",
                "『": "』",
                "《": "》",
                "(": ")",
                "[": "]",
            }
            edge_punctuation = " \t\r\n\u00a0.,，。;；:：!！?？、"
            previous = None
            while text and text != previous:
                previous = text
                text = text.strip(edge_punctuation)
                if len(text) >= 2 and pairs.get(text[0]) == text[-1]:
                    text = text[1:-1].strip(edge_punctuation)
            return text.strip()

        raw_text = prepare_text(output_text)
        if not raw_text:
            return ""
        normalized_text = strip_wrapping_noise(raw_text)
        try:
            parsed = json.loads(normalized_text)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, str):
            return strip_wrapping_noise(prepare_text(parsed))
        if isinstance(parsed, dict) and len(parsed) == 1:
            key, value = next(iter(parsed.items()))
            if key in ContentGuardProbeService.FIXED_ANSWER_JSON_FIELDS and isinstance(value, str):
                return strip_wrapping_noise(prepare_text(value))
        return normalized_text

    @staticmethod
    def fixed_answer_matches(output_text: str) -> bool:
        return ContentGuardProbeService.normalize_fixed_answer_probe_text(output_text) == ContentGuardProbeService.FIXED_ANSWER

    @staticmethod
    def build_pollution_probe_guard_request(provider_model: ProviderModel, *, endpoint_path: str, scenario_key: str) -> dict[str, Any]:
        marker = f"content_guard_pollution_probe:{scenario_key}"
        native_protocol = ContentGuardProbeService._native_protocol_from_probe_endpoint(endpoint_path)
        if native_protocol:
            model_name = ProviderService.provider_model_upstream_model_name(provider_model)
            return NativeProtocolAdapter.native_text_payload(
                native_protocol,
                model_name=model_name,
                prompt=marker,
                max_tokens=16,
            )
        if endpoint_path == "/chat/completions":
            return {
                "model": provider_model.model_name,
                "messages": [{"role": "user", "content": marker}],
                "temperature": 0,
            }
        return {
            "model": provider_model.model_name,
            "input": marker,
            "temperature": 0,
        }

    @staticmethod
    def build_stream_pollution_probe_payload(provider_model: ProviderModel, *, endpoint_path: str) -> dict[str, Any]:
        prompt = (
            "请用中文分 6 点解释大语言模型代理系统的健康检查流程，"
            "每点一句话。不要输出任何外部链接、优惠、联系方式、社群引导、赞助或广告内容。"
        )
        native_protocol = ContentGuardProbeService._native_protocol_from_probe_endpoint(endpoint_path)
        if native_protocol:
            model_name = ProviderService.provider_model_upstream_model_name(provider_model)
            return NativeProtocolAdapter.native_text_payload(
                native_protocol,
                model_name=model_name,
                prompt=prompt,
                max_tokens=256,
                stream=True,
            )
        if endpoint_path == "/chat/completions":
            return {
                "model": provider_model.model_name,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 256,
                "stream": True,
            }
        return {
            "model": provider_model.model_name,
            "input": prompt,
            "temperature": 0,
            "max_output_tokens": 256,
            "stream": True,
        }

    @staticmethod
    def build_json_payload(provider_model: ProviderModel, *, endpoint_path: str) -> dict[str, Any]:
        expected = json.dumps(ContentGuardProbeService.JSON_EXPECTED, ensure_ascii=False, separators=(",", ":"))
        prompt = f"只输出严格 JSON，不能有 Markdown、解释、前后缀或额外字段。JSON 必须完全等于：{expected}"
        native_protocol = ContentGuardProbeService._native_protocol_from_probe_endpoint(endpoint_path)
        if native_protocol:
            model_name = ProviderService.provider_model_upstream_model_name(provider_model)
            return NativeProtocolAdapter.native_text_payload(
                native_protocol,
                model_name=model_name,
                prompt=prompt,
                max_tokens=48,
            )
        if endpoint_path == "/chat/completions":
            return {
                "model": provider_model.model_name,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 48,
                "response_format": {"type": "json_object"},
            }
        return {
            "model": provider_model.model_name,
            "input": prompt,
            "temperature": 0,
            "max_output_tokens": 48,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "content_guard_json_probe",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "status": {"type": "string", "const": "ok"},
                            "marker": {"type": "string", "const": ContentGuardProbeService.FIXED_ANSWER},
                        },
                        "required": ["status", "marker"],
                        "additionalProperties": False,
                    },
                    "strict": True,
                }
            },
        }

    @staticmethod
    def build_vision_payload(provider_model: ProviderModel, *, endpoint_path: str) -> dict[str, Any]:
        prompt = "请识别图片中像素块的主色，只输出颜色名，不要解释。"
        native_protocol = ContentGuardProbeService._native_protocol_from_probe_endpoint(endpoint_path)
        if native_protocol:
            model_name = ProviderService.provider_model_upstream_model_name(provider_model)
            return NativeProtocolAdapter.openai_to_native_payload(
                native_protocol,
                "/chat/completions",
                {
                    "model": model_name,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {"type": "image_url", "image_url": {"url": ContentGuardProbeService.VISION_PROBE_IMAGE_DATA_URL}},
                            ],
                        }
                    ],
                    "temperature": 0,
                    "max_tokens": 32,
                },
            )
        if endpoint_path == "/chat/completions":
            return {
                "model": provider_model.model_name,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": ContentGuardProbeService.VISION_PROBE_IMAGE_DATA_URL,
                                    "detail": "low",
                                },
                            },
                        ],
                    }
                ],
                "temperature": 0,
                "max_tokens": 32,
            }
        return {
            "model": provider_model.model_name,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {
                            "type": "input_image",
                            "image_url": ContentGuardProbeService.VISION_PROBE_IMAGE_DATA_URL,
                            "detail": "low",
                        },
                    ],
                }
            ],
            "temperature": 0,
            "max_output_tokens": 32,
        }

    @staticmethod
    async def probe_fixed_answer(provider: Provider, provider_model: ProviderModel, *, endpoint_path: str) -> dict[str, Any]:
        endpoint_label = "固定答案完整性探针"
        payload = ContentGuardProbeService.build_fixed_answer_payload(provider_model, endpoint_path=endpoint_path)
        response, latency_ms, status_code, trace, error_result = await ContentGuardProbeService.send_content_probe_json(
            provider,
            provider_model,
            endpoint_path=endpoint_path,
            payload=payload,
            endpoint_label=endpoint_label,
        )
        if error_result is not None:
            return error_result
        structure_guard = ContentGuardProbeService.inspect_probe_json_response(
            response,
            provider=provider,
            provider_model=provider_model,
            endpoint_path=endpoint_path,
            request_payload=payload,
        )
        if structure_guard.result != ContentGuardService.RESULT_PASS:
            return ContentGuardProbeService.attach_raw_provider_response(
                ContentGuardProbeService.probe_failure(
                    endpoint_path=endpoint_path,
                    endpoint_label=endpoint_label,
                    support_label="固定答案探针未通过",
                    latency_ms=latency_ms,
                    status_code=status_code,
                    guard_result=structure_guard,
                ),
                response=response,
            )
        ProxyService = _proxy_service()
        output_text = (ProxyService._extract_response_text(response or {}, limit_bytes=512) or "").strip()
        if not ContentGuardProbeService.fixed_answer_matches(output_text):
            return ContentGuardProbeService.attach_raw_provider_response(
                ContentGuardProbeService.probe_failure(
                    endpoint_path=endpoint_path,
                    endpoint_label=endpoint_label,
                    support_label="固定答案探针未通过",
                    latency_ms=latency_ms,
                    status_code=status_code,
                    guard_result=ContentGuardProbeService.review_result(
                        "固定答案探针返回内容与指定字符串不一致",
                        category="fixed_answer_probe_mismatch",
                        excerpt=output_text[:200],
                    ),
                ),
                response=response,
                output_text=output_text,
            )
        return ContentGuardProbeService.attach_raw_provider_response(
            ContentGuardProbeService.probe_success(
                endpoint_path=endpoint_path,
                endpoint_label=endpoint_label,
                support_label="固定答案探针通过",
                latency_ms=latency_ms,
                status_code=status_code,
                message="固定答案一致",
                trace=trace,
            ),
            response=response,
            output_text=output_text,
        )

    @staticmethod
    async def probe_json(provider: Provider, provider_model: ProviderModel, *, endpoint_path: str) -> dict[str, Any]:
        endpoint_label = "严格 JSON 完整性探针"
        payload = ContentGuardProbeService.build_json_payload(provider_model, endpoint_path=endpoint_path)
        response, latency_ms, status_code, trace, error_result = await ContentGuardProbeService.send_content_probe_json(
            provider,
            provider_model,
            endpoint_path=endpoint_path,
            payload=payload,
            endpoint_label=endpoint_label,
        )
        if error_result is not None:
            return error_result
        structure_guard = ContentGuardProbeService.inspect_probe_json_response(
            response,
            provider=provider,
            provider_model=provider_model,
            endpoint_path=endpoint_path,
            request_payload=payload,
        )
        if structure_guard.result != ContentGuardService.RESULT_PASS:
            return ContentGuardProbeService.attach_raw_provider_response(
                ContentGuardProbeService.probe_failure(
                    endpoint_path=endpoint_path,
                    endpoint_label=endpoint_label,
                    support_label="严格 JSON 探针未通过",
                    latency_ms=latency_ms,
                    status_code=status_code,
                    guard_result=structure_guard,
                ),
                response=response,
            )
        ProxyService = _proxy_service()
        output_text = (ProxyService._extract_response_text(response or {}, limit_bytes=1024) or "").strip()
        try:
            parsed = json.loads(output_text)
        except Exception:
            parsed = None
        if parsed != ContentGuardProbeService.JSON_EXPECTED:
            return ContentGuardProbeService.attach_raw_provider_response(
                ContentGuardProbeService.probe_failure(
                    endpoint_path=endpoint_path,
                    endpoint_label=endpoint_label,
                    support_label="严格 JSON 探针未通过",
                    latency_ms=latency_ms,
                    status_code=status_code,
                    guard_result=ContentGuardProbeService.review_result(
                        "严格 JSON 探针返回内容不是期望的纯 JSON",
                        category="strict_json_probe_mismatch",
                        excerpt=output_text[:300],
                    ),
                ),
                response=response,
                output_text=output_text,
            )
        return ContentGuardProbeService.attach_raw_provider_response(
            ContentGuardProbeService.probe_success(
                endpoint_path=endpoint_path,
                endpoint_label=endpoint_label,
                support_label="严格 JSON 探针通过",
                latency_ms=latency_ms,
                status_code=status_code,
                message="严格 JSON 一致",
                trace=trace,
            ),
            response=response,
            output_text=output_text,
        )

    @staticmethod
    async def probe_vision(provider: Provider, provider_model: ProviderModel, *, endpoint_path: str) -> dict[str, Any]:
        endpoint_label = "图片输入能力探针"
        payload = ContentGuardProbeService.build_vision_payload(provider_model, endpoint_path=endpoint_path)
        response, latency_ms, status_code, trace, error_result = await ContentGuardProbeService.send_content_probe_json(
            provider,
            provider_model,
            endpoint_path=endpoint_path,
            payload=payload,
            endpoint_label=endpoint_label,
        )
        if error_result is not None:
            return error_result
        structure_guard = ContentGuardProbeService.inspect_probe_json_response(
            response,
            provider=provider,
            provider_model=provider_model,
            endpoint_path=endpoint_path,
            request_payload=payload,
        )
        if structure_guard.result != ContentGuardService.RESULT_PASS:
            return ContentGuardProbeService.attach_raw_provider_response(
                ContentGuardProbeService.probe_failure(
                    endpoint_path=endpoint_path,
                    endpoint_label=endpoint_label,
                    support_label="图片探针未通过",
                    latency_ms=latency_ms,
                    status_code=status_code,
                    guard_result=structure_guard,
                ),
                response=response,
            )
        ProxyService = _proxy_service()
        output_text = (ProxyService._extract_response_text(response or {}, limit_bytes=512) or "").strip()
        if "红" not in output_text and "red" not in output_text.lower():
            return ContentGuardProbeService.attach_raw_provider_response(
                ContentGuardProbeService.probe_failure(
                    endpoint_path=endpoint_path,
                    endpoint_label=endpoint_label,
                    support_label="图片探针未通过",
                    latency_ms=latency_ms,
                    status_code=status_code,
                    guard_result=ContentGuardProbeService.review_result(
                        "图片探针返回内容未识别出预期主色",
                        category="vision_probe_mismatch",
                        excerpt=output_text[:200],
                    ),
                ),
                response=response,
                output_text=output_text,
            )
        return ContentGuardProbeService.attach_raw_provider_response(
            ContentGuardProbeService.probe_success(
                endpoint_path=endpoint_path,
                endpoint_label=endpoint_label,
                support_label="图片探针通过",
                latency_ms=latency_ms,
                status_code=status_code,
                message="图片输入请求正常",
                trace=trace,
            ),
            response=response,
            output_text=output_text,
        )

    @staticmethod
    def _invalid_sse_control_line_reason(event: str) -> str | None:
        for raw_line in event.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("data:"):
                continue
            if line.startswith(":"):
                if "广告" in line or "http://" in line or "https://" in line:
                    return "SSE 注释行包含疑似污染内容"
                continue
            if line.startswith("event:"):
                event_name = line[6:].strip()
                if not event_name or not re.fullmatch(r"[a-zA-Z0-9_.-]+", event_name):
                    return "SSE event 行格式无效"
                continue
            if line.startswith("id:"):
                event_id = line[3:].strip()
                if "\x00" in event_id or len(event_id) > 128:
                    return "SSE id 行格式无效"
                continue
            if line.startswith("retry:"):
                retry_value = line[6:].strip()
                if not retry_value.isdigit():
                    return "SSE retry 行必须是毫秒数字"
                continue
            return "SSE 事件包含不支持的控制行"
        return None

    @staticmethod
    async def probe_sse(provider: Provider, provider_model: ProviderModel, *, endpoint_path: str) -> dict[str, Any]:
        endpoint_label = "流式污染检测探针"
        payload = ContentGuardProbeService.build_fixed_answer_payload(provider_model, endpoint_path=endpoint_path, stream=True)
        rules_json = ContentGuardProbeService.rules_json()
        url_allowlist = ContentGuardProbeService.url_allowlist()
        url_check_enabled = ContentGuardProbeService.url_check_enabled()
        ProxyService = _proxy_service()
        StreamTimeoutPolicy = _stream_timeout_policy_cls()
        started = time.perf_counter()
        stream_context = None
        exc_type = exc_value = exc_traceback = None
        max_duration_seconds = ContentGuardProbeService.SSE_PROBE_MAX_DURATION_SECONDS
        max_read_bytes = ContentGuardProbeService._content_guard_probe_setting_int(
            "content_guard_stream_buffer_max_bytes",
            ContentGuardProbeService.SSE_PROBE_MAX_BYTES,
            min_value=1024,
            max_value=ContentGuardProbeService.SSE_PROBE_MAX_BYTES,
        )
        fallback_trace: list[dict[str, Any]] = []
        limit_result = await ProbeRateLimitService.claim(
            provider,
            provider_model,
            probe_type=f"content_guard_sse_{endpoint_path.strip('/').replace('/', '_') or 'endpoint'}",
        )
        if not limit_result.allowed:
            return ContentGuardProbeService.mark_detection_result(
                ProbeRateLimitService.rate_limited_probe_result(
                    limit_result,
                    endpoint_path=endpoint_path,
                    endpoint_label=endpoint_label,
                    support_label="流式内容防护探针已限频",
                )
            )
        try:
            native_protocol = ContentGuardProbeService._native_protocol_from_probe_endpoint(endpoint_path)
            if native_protocol:
                from app.services.proxy_service import PreparedUpstreamRequest

                native_model_name = ProviderService.provider_model_upstream_model_name(provider_model)
                native_path = NativeProtocolAdapter.request_path(
                    native_protocol,
                    native_model_name,
                    stream=True,
                    endpoint_path_template=(
                        getattr(provider_model, "native_endpoint_path", None)
                        or getattr(provider, "native_endpoint_path", None)
                    ),
                )
                prepared = PreparedUpstreamRequest(
                    request_path=native_path,
                    request_payload=payload,
                    public_endpoint_path=endpoint_path,
                    upstream_protocol_type=native_protocol,
                    response_model_override=native_model_name,
                )
                headers = ProxyService._build_upstream_headers(
                    provider,
                    prepared=prepared,
                    extra_headers={"Accept-Encoding": "identity"},
                )
                stream_context = ProxyService._stream_prepared_request(
                    provider,
                    prepared=prepared,
                    headers=headers,
                    stream_connect_timeout_seconds=ContentGuardProbeService.provider_stream_connect_timeout_seconds(provider),
                )
                response, _prepared = await stream_context.__aenter__()
                await ProxyService._raise_stream_response_for_status(response)
                fallback_trace = ContentGuardProbeService.mark_detection_trace(
                    [{"result": "native_content_guard_stream_probe", "protocol_type": native_protocol, "endpoint": native_path}],
                    endpoint_path=endpoint_path,
                    endpoint_label=endpoint_label,
                )
            else:
                response, _prepared, stream_context, _fallback_trace = await ProxyService._open_stream_with_endpoint_fallback(
                    provider,
                    provider_model,
                    endpoint_path,
                    payload,
                    started=started,
                    stream_connect_timeout_seconds=ContentGuardProbeService.provider_stream_connect_timeout_seconds(provider),
                    extra_headers={"Accept-Encoding": "identity"},
                )
                fallback_trace = ContentGuardProbeService.mark_detection_trace(
                    _fallback_trace,
                    endpoint_path=endpoint_path,
                    endpoint_label=endpoint_label,
                )
            timeout_policy = StreamTimeoutPolicy(
                first_token_timeout_seconds=ContentGuardProbeService.provider_stream_first_token_timeout_seconds(provider),
                idle_timeout_seconds=ContentGuardProbeService.SSE_PROBE_IDLE_TIMEOUT_SECONDS,
                max_duration_seconds=max_duration_seconds,
            )
            chunk_iterator = response.aiter_bytes().__aiter__()
            buffer = bytearray()
            chunks: list[str] = []
            text_parts: list[str] = []
            stream_started = time.perf_counter()
            while (
                len(chunks) < ContentGuardProbeService.SSE_PROBE_MAX_CHUNKS
                and len(buffer) < max_read_bytes
                and time.perf_counter() - stream_started < max_duration_seconds
            ):
                try:
                    chunk = await ProxyService._read_next_stream_chunk(
                        chunk_iterator,
                        first_chunk_latency_ms=None if not chunks else 0,
                        stream_started=stream_started,
                        timeout_policy=timeout_policy,
                    )
                except StopAsyncIteration:
                    break
                if not chunk:
                    continue
                buffer.extend(chunk)
                chunks.append(chunk.decode("utf-8", errors="ignore"))
                if b"[DONE]" in buffer:
                    break
            events = ProxyService._consume_sse_event_texts(buffer)
            def with_stream_raw(result: dict[str, Any], *, output_text: str | None = None) -> dict[str, Any]:
                return ContentGuardProbeService.attach_raw_provider_response(
                    result,
                    output_text=output_text,
                    stream_events=events,
                    stream_chunks=chunks,
            )

            if not events:
                return with_stream_raw(ContentGuardProbeService.probe_failure(
                    endpoint_path=endpoint_path,
                    endpoint_label=endpoint_label,
                    support_label="SSE 探针未返回事件",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    status_code=200,
                    guard_result=ContentGuardProbeService.review_result(
                        "SSE 探针未返回任何事件",
                        category="invalid_sse_stream",
                    ),
                ))
            saw_done = False
            for event in events:
                if "data: [DONE]" in event.replace("\r", ""):
                    saw_done = True
                    continue
                if saw_done and event.strip():
                    return with_stream_raw(ContentGuardProbeService.probe_failure(
                        endpoint_path=endpoint_path,
                        endpoint_label=endpoint_label,
                        support_label="SSE 探针出现尾巴污染",
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        status_code=200,
                        guard_result=ContentGuardProbeService.review_result(
                            "SSE 终止符之后出现额外事件",
                            category="sse_tail_pollution",
                            excerpt=event[:300],
                        ),
                    ))
                invalid_control_reason = ContentGuardProbeService._invalid_sse_control_line_reason(event)
                if invalid_control_reason:
                    return with_stream_raw(ContentGuardProbeService.probe_failure(
                        endpoint_path=endpoint_path,
                        endpoint_label=endpoint_label,
                        support_label="SSE 探针事件格式异常",
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        status_code=200,
                        guard_result=ContentGuardProbeService.review_result(
                            invalid_control_reason,
                            category="invalid_sse_stream",
                            excerpt=event[:300],
                        ),
                    ))
                for line in event.splitlines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    result = ContentGuardService.inspect_sse_event(
                        data,
                        endpoint_path=endpoint_path,
                        rules_json=rules_json,
                        url_allowlist=url_allowlist,
                        url_check_enabled=url_check_enabled,
                    )
                    if result.result != ContentGuardService.RESULT_PASS:
                        return with_stream_raw(ContentGuardProbeService.probe_failure(
                            endpoint_path=endpoint_path,
                            endpoint_label=endpoint_label,
                            support_label="SSE 探针未通过",
                            latency_ms=int((time.perf_counter() - started) * 1000),
                            status_code=200,
                            guard_result=result,
                        ))
                    delta_text = ContentGuardProbeService.extract_probe_sse_text_delta(data)
                    if delta_text:
                        text_parts.append(delta_text)
            if not saw_done:
                latency_ms = int((time.perf_counter() - started) * 1000)
                guard_result = ContentGuardResult(
                    result=ContentGuardService.RESULT_PASS,
                    risk_level="low",
                    reason="SSE 探针窗口内已读取合法事件但未见终止符，按长流部分结果记录",
                    action="allow",
                    excerpt="".join(text_parts)[:300],
                )
                adapted_success = any(
                    isinstance(item, dict)
                    and (item.get("fallback_from") or item.get("fallback_to") or item.get("adapted_endpoint"))
                    for item in fallback_trace
                )
                return with_stream_raw(ContentGuardProbeService.mark_detection_result({
                    "endpoint_path": endpoint_path,
                    "endpoint_label": endpoint_label,
                    "success": True,
                    "native_success": not adapted_success,
                    "adapted_success": adapted_success,
                    "support_mode": "partial_stream",
                    "support_label": "SSE 探针部分长流通过",
                    "latency_ms": latency_ms,
                    "status_code": 200,
                    "message": "SSE 探针在检测窗口内未读取到终止符，已按合法部分长流记录并继续保留内容防护结果。",
                    "trace": fallback_trace,
                    "retryable": False,
                    "stream_done_seen": False,
                    "content_guard": ContentGuardProbeService.serialize_guard_result(guard_result),
                }), output_text="".join(text_parts).strip())
            output_text = "".join(text_parts).strip()
            if not ContentGuardProbeService.fixed_answer_matches(output_text):
                return with_stream_raw(ContentGuardProbeService.probe_failure(
                    endpoint_path=endpoint_path,
                    endpoint_label=endpoint_label,
                    support_label="SSE 探针固定答案不一致",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    status_code=200,
                    guard_result=ContentGuardProbeService.review_result(
                        f"SSE 探针聚合文本与固定答案不一致，实际聚合文本：{output_text[:180] or '空'}",
                        category="sse_fixed_answer_mismatch",
                        excerpt=output_text[:300],
                    ),
                ), output_text=output_text)
            return with_stream_raw(ContentGuardProbeService.probe_success(
                endpoint_path=endpoint_path,
                endpoint_label=endpoint_label,
                support_label="SSE 探针通过",
                latency_ms=int((time.perf_counter() - started) * 1000),
                status_code=200,
                message="SSE 固定答案、事件与终止符正常",
                trace=fallback_trace,
            ), output_text=output_text)
        except Exception as exc:
            exc_type, exc_value, exc_traceback = type(exc), exc, exc.__traceback__
            latency_ms = int((time.perf_counter() - started) * 1000)
            status_code = getattr(exc, "status_code", None)
            message = str(exc)
            decompression_error = ContentGuardProbeService._is_transport_decompression_error(message)
            return ContentGuardProbeService.probe_failure(
                endpoint_path=endpoint_path,
                endpoint_label=endpoint_label,
                support_label="上游压缩响应异常，未判定内容污染" if decompression_error else "SSE 探针失败",
                latency_ms=latency_ms,
                status_code=status_code,
                guard_result=ContentGuardProbeService.review_result(
                    (
                        f"上游响应压缩头与实际内容不一致：{message}；探针已请求 Accept-Encoding: identity，"
                        "本次不作为内容可信失败写入。"
                    )
                    if decompression_error
                    else (message or "SSE 探针异常"),
                    category="sse_probe_transport_decompression_error" if decompression_error else "sse_probe_exception",
                ),
                retryable=decompression_error,
            )
        finally:
            if stream_context is not None:
                await stream_context.__aexit__(exc_type, exc_value, exc_traceback)

    @staticmethod
    def _is_transport_decompression_error(message: str | None) -> bool:
        normalized = str(message or "").strip().lower()
        if not normalized:
            return False
        return (
            "incorrect header check" in normalized
            or ("decompress" in normalized and "header" in normalized)
            or ("content-encoding" in normalized and "gzip" in normalized)
        )

    @staticmethod
    def extract_probe_sse_text_delta(data: str) -> str:
        parsed = safeJsonParse(data)
        if not isinstance(parsed, dict):
            return ""
        candidates = parsed.get("candidates")
        if isinstance(candidates, list):
            parts: list[str] = []
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                content = candidate.get("content")
                if not isinstance(content, dict):
                    continue
                for part in content.get("parts") or []:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        parts.append(part["text"])
            if parts:
                return "".join(parts)
        delta = parsed.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("text"), str):
            return delta["text"]
        content = parsed.get("content")
        if isinstance(content, list):
            parts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            ]
            if parts:
                return "".join(parts)
        ProxyService = _proxy_service()
        return ProxyService._extract_response_text(parsed, limit_bytes=4096) or ""

    @staticmethod
    def pollution_result_from_sections(
        provider: Provider,
        provider_model: ProviderModel,
        *,
        endpoint_path: str,
        selected_scenarios: list[dict[str, Any]],
        sections: dict[str, str],
        output_text: str,
        status_code: int | None,
        trace: list[dict[str, Any]] | None,
        started: float,
        raw_response: Any = None,
    ) -> dict[str, Any]:
        endpoint_label = "外链广告识别探针"
        rules_json = ContentGuardProbeService.rules_json()
        url_allowlist = ContentGuardProbeService.url_allowlist()
        url_check_enabled = ContentGuardProbeService.url_check_enabled()
        scenario_results: list[dict[str, Any]] = []
        for scenario in selected_scenarios:
            section_text = sections.get(str(scenario.get("key") or ""), "").strip()
            if not section_text:
                text_guard = ContentGuardProbeService.review_result(
                    f"{scenario['label']}未按组合探针分段返回",
                    category="pollution_probe_missing_section",
                    excerpt=output_text[:300],
                )
                scenario_results.append(
                    {
                        "key": scenario["key"],
                        "detection": ContentGuardProbeService.pollution_detection_item(scenario, text_guard, excerpt=output_text[:300]),
                        "failure": (text_guard, status_code),
                    }
                )
                continue
            guard_request = ContentGuardProbeService.build_pollution_probe_guard_request(
                provider_model,
                endpoint_path=endpoint_path,
                scenario_key=str(scenario.get("key") or ""),
            )
            text_guard = ContentGuardService.inspect_response_text(
                section_text,
                provider=provider,
                endpoint_path=endpoint_path,
                request_payload=guard_request,
                rules_json=rules_json,
                url_allowlist=url_allowlist,
                url_check_enabled=url_check_enabled,
            )
            scenario_results.append(
                {
                    "key": scenario["key"],
                    "detection": ContentGuardProbeService.pollution_detection_item(scenario, text_guard, excerpt=section_text[:300]),
                    "failure": (text_guard, status_code) if text_guard.result != ContentGuardService.RESULT_PASS else None,
                }
            )
        detections = [item["detection"] for item in scenario_results if isinstance(item.get("detection"), dict)]
        failures = [item["failure"] for item in scenario_results if isinstance(item.get("failure"), tuple)]
        if failures:
            guard_result, failed_status_code = next(
                (
                    (guard, item_status)
                    for guard, item_status in failures
                    if guard.result == ContentGuardService.RESULT_BLOCK
                ),
                failures[0],
            )
            failure = ContentGuardProbeService.probe_failure(
                endpoint_path=endpoint_path,
                endpoint_label=endpoint_label,
                support_label="外链广告识别探针未通过",
                latency_ms=int((time.perf_counter() - started) * 1000),
                status_code=failed_status_code,
                guard_result=guard_result,
            )
            failure["detections"] = detections
            failure["failed_scenarios"] = [
                item for item in detections if item.get("result") != ContentGuardService.RESULT_PASS
            ]
            failure["trace"] = trace or []
            return ContentGuardProbeService.attach_raw_provider_response(
                failure,
                response=raw_response,
                output_text=output_text,
            )
        latency_ms = int((time.perf_counter() - started) * 1000)
        result = ContentGuardProbeService.probe_success(
            endpoint_path=endpoint_path,
            endpoint_label=endpoint_label,
            support_label="外链广告识别探针通过",
            latency_ms=latency_ms,
            status_code=200,
            message="已请求模型并用本地内容防护规则检测返回内容，未发现外链、广告或引流污染",
            trace=trace,
        )
        result["detections"] = detections
        return ContentGuardProbeService.attach_raw_provider_response(
            result,
            response=raw_response,
            output_text=output_text,
        )

    @staticmethod
    async def probe_pollution_rules(provider: Provider, provider_model: ProviderModel, *, endpoint_path: str) -> dict[str, Any]:
        endpoint_label = "外链广告识别探针"
        started = time.perf_counter()
        selected_scenarios = ContentGuardProbeService.pollution_probe_scenarios()[: ContentGuardProbeService.POLLUTION_PROBE_MAX_SCENARIOS]
        payload = ContentGuardProbeService.build_pollution_probe_payload(
            provider_model,
            endpoint_path=endpoint_path,
            prompt=ContentGuardProbeService.build_combined_pollution_probe_prompt(selected_scenarios),
        )
        timeout_seconds = ContentGuardProbeService.provider_probe_timeout_seconds(
            provider,
            ContentGuardProbeService.POLLUTION_PROBE_TIMEOUT_SECONDS,
        )
        try:
            response, _latency_ms, status_code, trace, error_result = await asyncio.wait_for(
                ContentGuardProbeService.send_content_probe_json(
                    provider,
                    provider_model,
                    endpoint_path=endpoint_path,
                    payload=payload,
                    endpoint_label=endpoint_label,
                ),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            timeout_guard = ContentGuardProbeService.review_result(
                f"外链广告识别组合探针超过 {timeout_seconds:.1f}s 限制",
                category="pollution_probe_timeout",
            )
            detections = [
                ContentGuardProbeService.pollution_detection_item(scenario, timeout_guard, excerpt="")
                for scenario in selected_scenarios
            ]
            failure = ContentGuardProbeService.probe_failure(
                endpoint_path=endpoint_path,
                endpoint_label=endpoint_label,
                support_label="外链广告识别探针未通过",
                latency_ms=int((time.perf_counter() - started) * 1000),
                status_code=None,
                guard_result=timeout_guard,
            )
            failure["detections"] = detections
            failure["failed_scenarios"] = detections
            return failure
        except Exception as exc:
            exception_guard = ContentGuardProbeService.review_result(
                str(exc) or "外链广告识别组合探针异常",
                category="pollution_probe_exception",
            )
            detections = [
                ContentGuardProbeService.pollution_detection_item(scenario, exception_guard, excerpt="")
                for scenario in selected_scenarios
            ]
            failure = ContentGuardProbeService.probe_failure(
                endpoint_path=endpoint_path,
                endpoint_label=endpoint_label,
                support_label="外链广告识别探针未通过",
                latency_ms=int((time.perf_counter() - started) * 1000),
                status_code=None,
                guard_result=exception_guard,
            )
            failure["detections"] = detections
            failure["failed_scenarios"] = detections
            return failure
        if error_result is not None:
            if ProbeRateLimitService.is_rate_limited_result(error_result):
                return error_result
            reason = ContentGuardProbeService.content_probe_failure_reason(
                error_result,
                fallback="外链广告识别组合探针请求失败",
            )
            error_guard = ContentGuardProbeService.review_result(
                reason,
                category="pollution_probe_request_failed",
            )
            detections = [
                ContentGuardProbeService.pollution_detection_item(scenario, error_guard, excerpt="")
                for scenario in selected_scenarios
            ]
            failure = ContentGuardProbeService.probe_failure(
                endpoint_path=endpoint_path,
                endpoint_label=endpoint_label,
                support_label="外链广告识别探针未通过",
                latency_ms=int((time.perf_counter() - started) * 1000),
                status_code=status_code,
                guard_result=error_guard,
            )
            failure["detections"] = detections
            failure["failed_scenarios"] = detections
            failure["trace"] = trace or []
            if isinstance(error_result, dict) and error_result.get("raw_provider_response") is not None:
                failure["raw_provider_response"] = error_result["raw_provider_response"]
            return failure
        structure_guard = ContentGuardProbeService.inspect_probe_json_response(
            response,
            provider=provider,
            provider_model=provider_model,
            endpoint_path=endpoint_path,
            request_payload=payload,
        )
        if structure_guard.result != ContentGuardService.RESULT_PASS:
            detections = [
                ContentGuardProbeService.pollution_detection_item(scenario, structure_guard, excerpt="")
                for scenario in selected_scenarios
            ]
            failure = ContentGuardProbeService.probe_failure(
                endpoint_path=endpoint_path,
                endpoint_label=endpoint_label,
                support_label="外链广告识别探针未通过",
                latency_ms=int((time.perf_counter() - started) * 1000),
                status_code=status_code,
                guard_result=structure_guard,
            )
            failure["detections"] = detections
            failure["failed_scenarios"] = detections
            failure["trace"] = trace or []
            return ContentGuardProbeService.attach_raw_provider_response(failure, response=response)
        ProxyService = _proxy_service()
        output_text = (ProxyService._extract_response_text(response or {}, limit_bytes=8192) or "").strip()
        if not output_text:
            empty_guard = ContentGuardProbeService.review_result(
                "外链广告识别组合探针未返回可检测文本",
                category="pollution_probe_empty_response",
            )
            detections = [
                ContentGuardProbeService.pollution_detection_item(scenario, empty_guard, excerpt="")
                for scenario in selected_scenarios
            ]
            failure = ContentGuardProbeService.probe_failure(
                endpoint_path=endpoint_path,
                endpoint_label=endpoint_label,
                support_label="外链广告识别探针未通过",
                latency_ms=int((time.perf_counter() - started) * 1000),
                status_code=status_code,
                guard_result=empty_guard,
            )
            failure["detections"] = detections
            failure["failed_scenarios"] = detections
            failure["trace"] = trace or []
            return ContentGuardProbeService.attach_raw_provider_response(failure, response=response)
        sections = ContentGuardProbeService.split_combined_pollution_output(output_text, selected_scenarios)
        return ContentGuardProbeService.pollution_result_from_sections(
            provider,
            provider_model,
            endpoint_path=endpoint_path,
            selected_scenarios=selected_scenarios,
            sections=sections,
            output_text=output_text,
            status_code=status_code,
            trace=trace,
            started=started,
            raw_response=response,
        )

    @staticmethod
    async def probe_fixed_answer_and_pollution_rules(
        provider: Provider,
        provider_model: ProviderModel,
        *,
        endpoint_path: str,
    ) -> dict[str, dict[str, Any]]:
        endpoint_label = "文本内容完整性组合探针"
        started = time.perf_counter()
        fixed_scenario = {
            "key": "fixed_answer",
            "label": "固定答案",
            "prompt": ContentGuardProbeService.fixed_answer_prompt(),
        }
        pollution_scenarios = ContentGuardProbeService.pollution_probe_scenarios()[
            : ContentGuardProbeService.POLLUTION_PROBE_MAX_SCENARIOS
        ]
        combined_scenarios = [fixed_scenario, *pollution_scenarios]
        payload = ContentGuardProbeService.build_pollution_probe_payload(
            provider_model,
            endpoint_path=endpoint_path,
            prompt=ContentGuardProbeService.build_combined_pollution_probe_prompt(combined_scenarios),
        )
        timeout_seconds = ContentGuardProbeService.provider_probe_timeout_seconds(
            provider,
            ContentGuardProbeService.COMBINED_TEXT_PROBE_TIMEOUT_SECONDS,
        )

        def paired_failure(guard_result: ContentGuardResult, *, status_code: int | None, trace: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
            fixed = ContentGuardProbeService.probe_failure(
                endpoint_path=endpoint_path,
                endpoint_label="固定答案完整性探针",
                support_label="固定答案探针未通过",
                latency_ms=int((time.perf_counter() - started) * 1000),
                status_code=status_code,
                guard_result=guard_result,
            )
            pollution = ContentGuardProbeService.probe_failure(
                endpoint_path=endpoint_path,
                endpoint_label="外链广告识别探针",
                support_label="外链广告识别探针未通过",
                latency_ms=int((time.perf_counter() - started) * 1000),
                status_code=status_code,
                guard_result=guard_result,
            )
            pollution["detections"] = [
                ContentGuardProbeService.pollution_detection_item(scenario, guard_result, excerpt="")
                for scenario in pollution_scenarios
            ]
            pollution["failed_scenarios"] = pollution["detections"]
            if trace is not None:
                fixed["trace"] = trace
                pollution["trace"] = trace
            return {"fixed_answer": fixed, "pollution_rules": pollution}

        try:
            response, _latency_ms, status_code, trace, error_result = await asyncio.wait_for(
                ContentGuardProbeService.send_content_probe_json(
                    provider,
                    provider_model,
                    endpoint_path=endpoint_path,
                    payload=payload,
                    endpoint_label=endpoint_label,
                ),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            return paired_failure(
                ContentGuardProbeService.review_result(
                    f"文本内容完整性组合探针超过 {timeout_seconds:.1f}s 限制",
                    category="combined_text_probe_timeout",
                ),
                status_code=None,
            )
        except Exception as exc:
            return paired_failure(
                ContentGuardProbeService.review_result(
                    str(exc) or "文本内容完整性组合探针异常",
                    category="combined_text_probe_exception",
                ),
                status_code=None,
            )
        if error_result is not None:
            if ProbeRateLimitService.is_rate_limited_result(error_result):
                fixed = dict(error_result)
                fixed["endpoint_label"] = "固定答案完整性探针"
                fixed["support_label"] = "固定答案探针已限频"
                pollution = dict(error_result)
                pollution["endpoint_label"] = "外链广告识别探针"
                pollution["support_label"] = "外链广告识别探针已限频"
                return {"fixed_answer": fixed, "pollution_rules": pollution}
            reason = ContentGuardProbeService.content_probe_failure_reason(
                error_result,
                fallback="文本内容完整性组合探针请求失败",
            )
            results = paired_failure(
                ContentGuardProbeService.review_result(
                    reason,
                    category="combined_text_probe_request_failed",
                ),
                status_code=status_code,
                trace=trace or [],
            )
            if isinstance(error_result, dict) and error_result.get("raw_provider_response") is not None:
                results["fixed_answer"]["raw_provider_response"] = error_result["raw_provider_response"]
                results["pollution_rules"]["raw_provider_response"] = error_result["raw_provider_response"]
            return results
        structure_guard = ContentGuardProbeService.inspect_probe_json_response(
            response,
            provider=provider,
            provider_model=provider_model,
            endpoint_path=endpoint_path,
            request_payload=payload,
        )
        if structure_guard.result != ContentGuardService.RESULT_PASS:
            results = paired_failure(structure_guard, status_code=status_code, trace=trace or [])
            for item in results.values():
                ContentGuardProbeService.attach_raw_provider_response(item, response=response)
            return results
        ProxyService = _proxy_service()
        output_text = (ProxyService._extract_response_text(response or {}, limit_bytes=8192) or "").strip()
        if not output_text:
            empty_guard = ContentGuardProbeService.review_result(
                "文本内容完整性组合探针未返回可检测文本",
                category="combined_text_probe_empty_response",
            )
            results = paired_failure(empty_guard, status_code=status_code, trace=trace or [])
            for item in results.values():
                ContentGuardProbeService.attach_raw_provider_response(item, response=response)
            return results
        sections = ContentGuardProbeService.split_combined_pollution_output(output_text, combined_scenarios)
        fixed_text = sections.get("fixed_answer", "").strip()
        if not fixed_text:
            fixed_guard = ContentGuardProbeService.review_result(
                "固定答案未按组合探针分段返回",
                category="fixed_answer_missing_section",
                excerpt=output_text[:300],
            )
            fixed_result = ContentGuardProbeService.probe_failure(
                endpoint_path=endpoint_path,
                endpoint_label="固定答案完整性探针",
                support_label="固定答案探针未通过",
                latency_ms=int((time.perf_counter() - started) * 1000),
                status_code=status_code,
                guard_result=fixed_guard,
            )
        elif not ContentGuardProbeService.fixed_answer_matches(fixed_text):
            fixed_result = ContentGuardProbeService.probe_failure(
                endpoint_path=endpoint_path,
                endpoint_label="固定答案完整性探针",
                support_label="固定答案探针未通过",
                latency_ms=int((time.perf_counter() - started) * 1000),
                status_code=status_code,
                guard_result=ContentGuardProbeService.review_result(
                    "固定答案探针返回内容与指定字符串不一致",
                    category="fixed_answer_probe_mismatch",
                    excerpt=fixed_text[:200],
                ),
            )
        else:
            fixed_result = ContentGuardProbeService.probe_success(
                endpoint_path=endpoint_path,
                endpoint_label="固定答案完整性探针",
                support_label="固定答案探针通过",
                latency_ms=int((time.perf_counter() - started) * 1000),
                status_code=status_code,
                message="固定答案一致",
                trace=trace,
            )
        ContentGuardProbeService.attach_raw_provider_response(
            fixed_result,
            response=response,
            output_text=output_text,
        )
        pollution_result = ContentGuardProbeService.pollution_result_from_sections(
            provider,
            provider_model,
            endpoint_path=endpoint_path,
            selected_scenarios=pollution_scenarios,
            sections=sections,
            output_text=output_text,
            status_code=status_code,
            trace=trace,
            started=started,
            raw_response=response,
        )
        return {"fixed_answer": fixed_result, "pollution_rules": pollution_result}

    @staticmethod
    def inspect_probe_json_response(
        response: Any,
        *,
        provider: Provider,
        provider_model: ProviderModel,
        endpoint_path: str,
        request_payload: dict[str, Any],
    ) -> ContentGuardResult:
        return ContentGuardService.inspect_json_response(
            response,
            provider=provider,
            provider_model=provider_model,
            endpoint_path=endpoint_path,
            request_payload=request_payload,
            max_scan_bytes=16384,
            rules_json=ContentGuardProbeService.rules_json(),
            url_allowlist=ContentGuardProbeService.url_allowlist(),
            url_check_enabled=ContentGuardProbeService.url_check_enabled(),
        )

    @staticmethod
    def inspect_probe_stream_chunk(
        chunk: bytes | str | None,
        *,
        endpoint_path: str,
    ) -> ContentGuardResult:
        if not chunk:
            return ContentGuardResult(result=ContentGuardService.RESULT_PASS, risk_level="low", reason="流式探针未返回可扫描内容")
        text = chunk.decode("utf-8", errors="ignore") if isinstance(chunk, bytes) else str(chunk)
        events = [item.strip() for item in text.split("\n\n") if item.strip()]
        if not events:
            return ContentGuardService.inspect_response_text(
                text,
                endpoint_path=endpoint_path,
                rules_json=ContentGuardProbeService.rules_json(),
                url_allowlist=ContentGuardProbeService.url_allowlist(),
                url_check_enabled=ContentGuardProbeService.url_check_enabled(),
            )
        for event in events:
            data_lines = [
                line[5:].strip()
                for line in event.splitlines()
                if line.strip().startswith("data:")
            ]
            if not data_lines:
                continue
            for data in data_lines:
                result = ContentGuardService.inspect_sse_event(
                    data,
                    endpoint_path=endpoint_path,
                    rules_json=ContentGuardProbeService.rules_json(),
                    url_allowlist=ContentGuardProbeService.url_allowlist(),
                    url_check_enabled=ContentGuardProbeService.url_check_enabled(),
                )
                if result.result != ContentGuardService.RESULT_PASS:
                    return result
        return ContentGuardResult(result=ContentGuardService.RESULT_PASS, risk_level="low", reason="流式首段内容完整性通过")

    @staticmethod
    def rules_json() -> str:
        try:
            return str(getattr(SettingService.get_cached(), "content_guard_rules_json", "") or "")
        except Exception:
            return ""

    @staticmethod
    def enabled() -> bool:
        try:
            return bool(getattr(SettingService.get_cached(), "content_guard_enabled", True))
        except Exception:
            return True

    @staticmethod
    def url_check_enabled() -> bool:
        try:
            return bool(getattr(SettingService.get_cached(), "content_guard_url_check_enabled", True))
        except Exception:
            return True

    @staticmethod
    def url_allowlist() -> str:
        try:
            return str(getattr(SettingService.get_cached(), "content_guard_url_allowlist_json", "") or "")
        except Exception:
            return ""

    @staticmethod
    def pollution_detection_item(scenario: dict[str, Any], guard_result: ContentGuardResult, *, excerpt: str) -> dict[str, Any]:
        return {
            "key": scenario["key"],
            "label": scenario["label"],
            "detected": guard_result.result != ContentGuardService.RESULT_PASS,
            "result": guard_result.result,
            "risk_level": guard_result.risk_level,
            "categories": sorted(set(guard_result.categories or [])),
            "reason": guard_result.reason,
            "excerpt": excerpt[:300],
        }

    @staticmethod
    def probe_failure(
        *,
        endpoint_path: str,
        endpoint_label: str,
        support_label: str,
        latency_ms: int,
        status_code: int | None,
        guard_result: ContentGuardResult,
        retryable: bool = False,
    ) -> dict[str, Any]:
        message = f"内容完整性探针失败：{guard_result.reason or guard_result.result}"
        return ContentGuardProbeService.mark_detection_result({
            "endpoint_path": endpoint_path,
            "endpoint_label": endpoint_label,
            "success": False,
            "native_success": False,
            "adapted_success": False,
            "support_mode": "content_guard_failed",
            "support_label": support_label,
            "latency_ms": latency_ms,
            "status_code": status_code,
            "message": message,
            "trace": [
                {
                    "result": "content_guard_probe_failed",
                    "endpoint": endpoint_path,
                    "latency_ms": latency_ms,
                    "content_guard": ContentGuardProbeService.serialize_guard_result(guard_result),
                }
            ],
            "retryable": bool(retryable),
            "content_guard": ContentGuardProbeService.serialize_guard_result(guard_result),
        })

    @staticmethod
    def first_content_guard_result(endpoint_results: list[dict[str, Any]]) -> dict[str, Any] | None:
        fallback_result: dict[str, Any] | None = None
        for endpoint_result in endpoint_results:
            content_guard = endpoint_result.get("content_guard")
            if not isinstance(content_guard, dict):
                continue
            if content_guard.get("content_guard_result") != ContentGuardService.RESULT_PASS:
                return content_guard
            if fallback_result is None:
                fallback_result = content_guard
        return fallback_result

    @staticmethod
    def _content_probe_decision(
        probe_results: list[dict[str, Any]],
        fallback_guard_result: dict[str, Any],
    ) -> dict[str, Any]:
        by_key = {
            str(item.get("phase_key") or ""): item
            for item in probe_results
            if str(item.get("phase_key") or "").startswith("content_")
        }
        missing_keys = sorted(ContentGuardProbeService.TRUST_PROBE_KEYS - set(by_key.keys()))
        failed_results = [
            item
            for item in probe_results
            if str(item.get("phase_key") or "").startswith("content_")
            and (
                item.get("success") is not True
                or str(item.get("content_guard_result") or "") not in {"", ContentGuardService.RESULT_PASS}
            )
        ]
        required_missing_results = [
            item
            for item in failed_results
            if str(item.get("phase_key") or "") in ContentGuardProbeService.TRUST_PROBE_KEYS
            and (
                item.get("required_missing") is True
                or str(item.get("support_mode") or "") == "required_missing"
            )
        ]
        if not missing_keys and not failed_results:
            return {
                "content_guard_result": ContentGuardService.RESULT_PASS,
                "content_guard_risk_level": "low",
                "content_guard_categories_json": dumps_json([]),
                "content_guard_reason": "固定答案、外链广告识别、流式污染检测探针全部通过",
                "content_guard_action": "allow",
            }
        if missing_keys:
            categories = ["content_trust_probe_incomplete"]
        elif required_missing_results:
            categories = ["content_trust_required_missing"]
        else:
            categories = ["content_trust_probe_failed"]
        reasons: list[str] = []
        if missing_keys:
            labels = {
                "content_fixed_answer": "固定答案",
                "content_pollution_rules": "外链广告识别",
                "content_sse": "流式污染检测",
            }
            reasons.append("缺少必需可信探针：" + "、".join(labels.get(key, key) for key in missing_keys))
        for item in failed_results[:3]:
            reason = item.get("content_guard_reason") or item.get("message") or item.get("support_label") or item.get("endpoint_label")
            if reason:
                reasons.append(str(reason))
        fallback_result = str(fallback_guard_result.get("content_guard_result") or "")
        result = (
            ContentGuardService.RESULT_BLOCK
            if missing_keys or required_missing_results or fallback_result == ContentGuardService.RESULT_BLOCK
            else ContentGuardService.RESULT_REVIEW
        )
        return {
            "content_guard_result": result,
            "content_guard_risk_level": "high" if result == ContentGuardService.RESULT_BLOCK else "medium",
            "content_guard_categories_json": dumps_json(categories),
            "content_guard_reason": "；".join(reasons) or "内容可信探针未全部通过",
            "content_guard_action": "block" if result == ContentGuardService.RESULT_BLOCK else "record",
        }

    @staticmethod
    def apply_content_probe_health(
        db: Session,
        provider: Provider,
        provider_model: ProviderModel,
        *,
        content_guard_result: dict[str, Any],
        endpoint_results: list[dict[str, Any]] | None = None,
        detection_source: str = "automatic_probe",
    ) -> None:
        now = now_beijing()
        normalized_source = str(detection_source or "automatic_probe").strip() or "automatic_probe"
        probe_results = [
            ContentGuardProbeService.summarize_probe_result(endpoint_result)
            for endpoint_result in (endpoint_results or [])
            if str(endpoint_result.get("capability_key") or "").startswith("content_")
            or str(endpoint_result.get("endpoint_label") or "").endswith("完整性探针")
        ]
        if not probe_results:
            probe_results = [ContentGuardProbeService.summarize_probe_result({"content_guard": content_guard_result})]
        has_content_probe_result = any(
            str(item.get("phase_key") or "").startswith("content_")
            for item in probe_results
        )
        if endpoint_results is not None and not has_content_probe_result:
            return
        content_guard_result = ContentGuardProbeService._content_probe_decision(
            probe_results,
            content_guard_result,
        )
        result = str(content_guard_result.get("content_guard_result") or "")
        serialized_results = {
            "updated_at": now,
            "status": str(provider_model.content_integrity_status or "unknown"),
            "detection_source": normalized_source,
            "manual_detection": normalized_source.startswith("manual"),
            "results": probe_results,
            "trust_required_keys": sorted(ContentGuardProbeService.TRUST_PROBE_KEYS),
            "last_result": ContentGuardProbeService.summarize_probe_result({"content_guard": content_guard_result}),
        }
        if result == ContentGuardService.RESULT_PASS:
            provider_model.content_probe_last_passed_at = now
            provider_model.content_probe_failure_count = 0
            provider_model.content_integrity_status = "passed"
            if provider.content_integrity_status in {"blocked", "degraded", "unknown", None}:
                provider.content_integrity_status = "passed"
            provider.content_integrity_score = max(80, int(provider.content_integrity_score or 80))
            ProviderService.refresh_provider_content_integrity_state(provider)
            serialized_results["status"] = provider_model.content_integrity_status
            serialized_results["provider_status"] = str(provider.content_integrity_status or "unknown")
            serialized_results["provider_trust_level"] = str(provider.trust_level or "standard")
            provider_model.content_probe_results_json = dumps_json(serialized_results)
            ProviderService.invalidate_provider_runtime_cache()
            db.commit()
            return
        previous_failed_at = provider_model.content_probe_last_failed_at
        within_failure_window = (
            previous_failed_at is not None
            and now - previous_failed_at <= timedelta(seconds=ContentGuardProbeService.PROBE_FAILURE_WINDOW_SECONDS)
        )
        provider_model.content_probe_last_failed_at = now
        provider_model.content_probe_failure_count = (
            int(provider_model.content_probe_failure_count or 0) + 1
            if within_failure_window
            else 1
        )
        result_categories: set[str] = set()
        raw_categories = content_guard_result.get("content_guard_categories_json")
        if isinstance(raw_categories, str) and raw_categories:
            try:
                parsed_categories = json.loads(raw_categories)
            except Exception:
                parsed_categories = []
            if isinstance(parsed_categories, list):
                result_categories = {str(item) for item in parsed_categories}
        should_isolate = (
            "content_trust_probe_incomplete" in result_categories
            or (within_failure_window and provider_model.content_probe_failure_count >= 3)
        )
        if should_isolate:
            provider_model.content_integrity_status = "blocked"
            provider.content_integrity_status = "blocked"
        else:
            provider_model.content_integrity_status = "degraded"
            if provider.content_integrity_status != "blocked":
                provider.content_integrity_status = "degraded"
        ProviderService.refresh_provider_content_integrity_state(provider)
        serialized_results["status"] = provider_model.content_integrity_status
        serialized_results["provider_status"] = str(provider.content_integrity_status or "unknown")
        serialized_results["provider_trust_level"] = str(provider.trust_level or "standard")
        provider_model.content_probe_results_json = dumps_json(serialized_results)
        ProviderService.invalidate_provider_runtime_cache()
        db.commit()

from app.utils.timezone import now_beijing
