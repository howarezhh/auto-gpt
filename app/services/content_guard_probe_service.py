from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.services.content_guard_service import ContentGuardResult, ContentGuardService
from app.services.provider_service import ProviderService
from app.services.setting_service import SettingService
from app.utils.json_utils import dumps_json


def _proxy_service():
    from app.services.proxy_service import ProxyService

    return ProxyService


def _stream_timeout_policy_cls():
    from app.services.proxy_service import StreamTimeoutPolicy

    return StreamTimeoutPolicy


class ContentGuardProbeService:
    """内容完整性探针、检测结果与内容完整性状态维护。"""

    FIXED_ANSWER = "AOTU_CONTENT_GUARD_OK"
    JSON_EXPECTED = {"status": "ok", "marker": "AOTU_CONTENT_GUARD_OK"}
    TOOL_NAME = "verify_order"
    TOOL_ARGS = {"order_id": "AOTU-20260606", "city": "杭州"}
    PROBE_PHASE_KEYS = frozenset(
        {
            "content_fixed_answer",
            "content_pollution_rules",
            "content_json",
            "content_sse",
            "content_tools",
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
    POLLUTION_PROBE_SCENARIO_TIMEOUT_SECONDS = 8
    POLLUTION_PROBE_TOTAL_TIMEOUT_SECONDS = 24
    DETECTION_TRAFFIC_TYPE = "content_guard_probe"

    @staticmethod
    def content_probe_endpoint_path(provider: Provider, provider_model: ProviderModel) -> str | None:
        if ProviderService.provider_supports_responses(provider) and bool(getattr(provider_model, "supports_responses", False)):
            return "/responses"
        if ProviderService.provider_supports_chat_completions(provider) and bool(getattr(provider_model, "supports_chat_completions", False)):
            return "/chat/completions"
        return None

    @staticmethod
    def build_health_phase_specs(
        provider: Provider,
        *,
        should_test_endpoint: Callable[[ProviderModel, str], bool],
    ) -> list[dict[str, Any]]:
        """构造健康检测可复用的内容完整性阶段，具体探针仍由内容防护模块负责。"""
        return [
            {
                "key": "content_fixed_answer",
                "label": "固定答案完整性探针",
                "targets": lambda model: bool(
                    should_test_endpoint(model, "chat") or should_test_endpoint(model, "responses")
                ),
                "probes": [
                    {
                        "key": "content_fixed_answer",
                        "probe": lambda model: ContentGuardProbeService.probe_fixed_answer(
                            provider,
                            model,
                            endpoint_path=ContentGuardProbeService.content_probe_endpoint_path(provider, model) or "/responses",
                        ),
                    }
                ],
            },
            {
                "key": "content_json",
                "label": "严格 JSON 完整性探针",
                "targets": lambda model: bool(
                    should_test_endpoint(model, "chat") or should_test_endpoint(model, "responses")
                ),
                "probes": [
                    {
                        "key": "content_json",
                        "probe": lambda model: ContentGuardProbeService.probe_json(
                            provider,
                            model,
                            endpoint_path=ContentGuardProbeService.content_probe_endpoint_path(provider, model) or "/responses",
                        ),
                    }
                ],
            },
            {
                "key": "content_pollution_rules",
                "label": "外链广告识别探针",
                "targets": lambda model: bool(
                    should_test_endpoint(model, "chat") or should_test_endpoint(model, "responses")
                ),
                "probes": [
                    {
                        "key": "content_pollution_rules",
                        "probe": lambda model: ContentGuardProbeService.probe_pollution_rules(
                            provider,
                            model,
                            endpoint_path=ContentGuardProbeService.content_probe_endpoint_path(provider, model) or "/responses",
                        ),
                    }
                ],
            },
            {
                "key": "content_sse",
                "label": "流式污染检测探针",
                "targets": lambda model: bool(
                    model.supports_stream
                    and (should_test_endpoint(model, "chat") or should_test_endpoint(model, "responses"))
                ),
                "probes": [
                    {
                        "key": "content_sse",
                        "probe": lambda model: ContentGuardProbeService.probe_sse(
                            provider,
                            model,
                            endpoint_path=ContentGuardProbeService.content_probe_endpoint_path(provider, model) or "/responses",
                        ),
                    }
                ],
            },
            {
                "key": "content_tools",
                "label": "工具调用完整性探针",
                "targets": lambda model: (
                    ProviderService.provider_model_supports_tools(model)
                    and (should_test_endpoint(model, "chat") or should_test_endpoint(model, "responses"))
                ),
                "probes": [
                    {
                        "key": "content_tools",
                        "probe": lambda model: ContentGuardProbeService.probe_tools(
                            provider,
                            model,
                            endpoint_path=ContentGuardProbeService.content_probe_endpoint_path(provider, model) or "/responses",
                        ),
                    }
                ],
            },
        ]

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
        setting = await ProxyService._get_setting_async()
        try:
            response, _, fallback_trace = await ProxyService._forward_json_with_endpoint_fallback(
                provider,
                provider_model,
                endpoint_path,
                payload,
                started=started,
                setting=setting,
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
            return None, int((time.perf_counter() - started) * 1000), status_code, [], ContentGuardProbeService.mark_detection_result({
                "endpoint_path": endpoint_path,
                "endpoint_label": endpoint_label,
                "success": False,
                "native_success": False,
                "adapted_success": False,
                "support_mode": "unsupported",
                "support_label": f"{endpoint_label} 请求失败",
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "status_code": status_code,
                "message": message,
                "trace": [],
                "retryable": False,
                "error_detail": detail,
            })
        except Exception as exc:
            status_code = getattr(exc, "status_code", None)
            detail = getattr(exc, "detail", None)
            message = ProxyService._error_message_for_log(detail) if detail is not None else str(exc)
            return None, int((time.perf_counter() - started) * 1000), status_code, [], ContentGuardProbeService.mark_detection_result({
                "endpoint_path": endpoint_path,
                "endpoint_label": endpoint_label,
                "success": False,
                "native_success": False,
                "adapted_success": False,
                "support_mode": "unsupported",
                "support_label": f"{endpoint_label} 请求失败",
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "status_code": status_code,
                "message": message,
                "trace": [],
                "retryable": False,
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
        trace = result.get("trace") if isinstance(result.get("trace"), list) else []
        result["trace"] = ContentGuardProbeService.mark_detection_trace(
            trace,
            endpoint_path=str(result.get("endpoint_path") or ""),
            endpoint_label=str(result.get("endpoint_label") or ""),
        )
        return result

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
            "native_success": True,
            "adapted_success": False,
            "support_mode": "native",
            "support_label": support_label,
            "latency_ms": latency_ms,
            "status_code": status_code,
            "message": message,
            "trace": trace or [],
            "retryable": False,
            "content_guard": guard_result.to_log_kwargs(),
        })

    @staticmethod
    def summarize_probe_result(endpoint_result: dict[str, Any]) -> dict[str, Any]:
        content_guard = endpoint_result.get("content_guard")
        if not isinstance(content_guard, dict):
            content_guard = {}
        return {
            "phase_key": endpoint_result.get("capability_key") or endpoint_result.get("endpoint_label"),
            "endpoint_label": endpoint_result.get("endpoint_label"),
            "endpoint_path": endpoint_result.get("endpoint_path"),
            "support_label": endpoint_result.get("support_label"),
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
        return ContentGuardResult(
            result=ContentGuardService.RESULT_REVIEW,
            risk_level="medium",
            categories=[category],
            reason=reason,
            action="record",
            excerpt=excerpt,
            score_delta=-10,
        )

    @staticmethod
    def build_fixed_answer_payload(provider_model: ProviderModel, *, endpoint_path: str, stream: bool = False) -> dict[str, Any]:
        prompt = f"只输出这一段固定字符串，不要输出任何解释、标点或换行：{ContentGuardProbeService.FIXED_ANSWER}"
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
        if endpoint_path == "/chat/completions":
            return {
                "model": provider_model.model_name,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 96,
            }
        return {
            "model": provider_model.model_name,
            "input": prompt,
            "temperature": 0,
            "max_output_tokens": 96,
        }

    @staticmethod
    def build_json_payload(provider_model: ProviderModel, *, endpoint_path: str) -> dict[str, Any]:
        expected = json.dumps(ContentGuardProbeService.JSON_EXPECTED, ensure_ascii=False, separators=(",", ":"))
        prompt = f"只输出严格 JSON，不能有 Markdown、解释、前后缀或额外字段。JSON 必须完全等于：{expected}"
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
        }

    @staticmethod
    def build_tools_payload(provider_model: ProviderModel, *, endpoint_path: str) -> dict[str, Any]:
        args = ContentGuardProbeService.TOOL_ARGS
        prompt = f"必须调用 verify_order 工具，参数必须为 order_id={args['order_id']}，city={args['city']}。不要输出自然语言。"
        if endpoint_path == "/chat/completions":
            return {
                "model": provider_model.model_name,
                "messages": [{"role": "user", "content": prompt}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": ContentGuardProbeService.TOOL_NAME,
                            "description": "校验订单归属城市。",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "order_id": {"type": "string"},
                                    "city": {"type": "string"},
                                },
                                "required": ["order_id", "city"],
                                "additionalProperties": False,
                            },
                        },
                    }
                ],
                "tool_choice": {"type": "function", "function": {"name": ContentGuardProbeService.TOOL_NAME}},
                "temperature": 0,
                "max_tokens": 48,
            }
        return {
            "model": provider_model.model_name,
            "input": prompt,
            "tools": [
                {
                    "type": "function",
                    "name": ContentGuardProbeService.TOOL_NAME,
                    "description": "校验订单归属城市。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "order_id": {"type": "string"},
                            "city": {"type": "string"},
                        },
                        "required": ["order_id", "city"],
                        "additionalProperties": False,
                    },
                }
            ],
            "tool_choice": "required",
            "temperature": 0,
            "max_output_tokens": 48,
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
            return ContentGuardProbeService.probe_failure(
                endpoint_path=endpoint_path,
                endpoint_label=endpoint_label,
                support_label="固定答案探针未通过",
                latency_ms=latency_ms,
                status_code=status_code,
                guard_result=structure_guard,
            )
        ProxyService = _proxy_service()
        output_text = (ProxyService._extract_response_text(response or {}, limit_bytes=512) or "").strip()
        if output_text != ContentGuardProbeService.FIXED_ANSWER:
            return ContentGuardProbeService.probe_failure(
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
            )
        return ContentGuardProbeService.probe_success(
            endpoint_path=endpoint_path,
            endpoint_label=endpoint_label,
            support_label="固定答案探针通过",
            latency_ms=latency_ms,
            status_code=status_code,
            message="固定答案一致",
            trace=trace,
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
            return ContentGuardProbeService.probe_failure(
                endpoint_path=endpoint_path,
                endpoint_label=endpoint_label,
                support_label="严格 JSON 探针未通过",
                latency_ms=latency_ms,
                status_code=status_code,
                guard_result=structure_guard,
            )
        ProxyService = _proxy_service()
        output_text = (ProxyService._extract_response_text(response or {}, limit_bytes=1024) or "").strip()
        try:
            parsed = json.loads(output_text)
        except Exception:
            parsed = None
        if parsed != ContentGuardProbeService.JSON_EXPECTED or output_text != json.dumps(parsed, ensure_ascii=False, separators=(",", ":")):
            return ContentGuardProbeService.probe_failure(
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
            )
        return ContentGuardProbeService.probe_success(
            endpoint_path=endpoint_path,
            endpoint_label=endpoint_label,
            support_label="严格 JSON 探针通过",
            latency_ms=latency_ms,
            status_code=status_code,
            message="严格 JSON 一致",
            trace=trace,
        )

    @staticmethod
    async def probe_sse(provider: Provider, provider_model: ProviderModel, *, endpoint_path: str) -> dict[str, Any]:
        endpoint_label = "流式污染检测探针"
        payload = ContentGuardProbeService.build_fixed_answer_payload(provider_model, endpoint_path=endpoint_path, stream=True)
        ProxyService = _proxy_service()
        StreamTimeoutPolicy = _stream_timeout_policy_cls()
        started = time.perf_counter()
        setting = await ProxyService._get_setting_async()
        stream_context = None
        exc_type = exc_value = exc_traceback = None
        try:
            response, _prepared, stream_context, _fallback_trace = await ProxyService._open_stream_with_endpoint_fallback(
                provider,
                provider_model,
                endpoint_path,
                payload,
                started=started,
                stream_connect_timeout_seconds=ContentGuardProbeService.STREAM_CONNECT_TIMEOUT_SECONDS,
            )
            timeout_policy = StreamTimeoutPolicy(
                first_token_timeout_seconds=ContentGuardProbeService.STREAM_FIRST_TOKEN_TIMEOUT_SECONDS,
                idle_timeout_seconds=max(0, int(getattr(setting, "stream_idle_timeout_seconds", 0) or 0)),
                max_duration_seconds=(
                    max(0, int(getattr(setting, "stream_max_duration_seconds", 0) or 0))
                    or ContentGuardProbeService.SSE_PROBE_MAX_DURATION_SECONDS
                ),
            )
            chunk_iterator = response.aiter_bytes().__aiter__()
            buffer = bytearray()
            chunks: list[str] = []
            stream_started = time.perf_counter()
            while (
                len(chunks) < ContentGuardProbeService.SSE_PROBE_MAX_CHUNKS
                and len(buffer) < ContentGuardProbeService.SSE_PROBE_MAX_BYTES
                and time.perf_counter() - stream_started < ContentGuardProbeService.SSE_PROBE_MAX_DURATION_SECONDS
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
            if not events:
                return ContentGuardProbeService.probe_failure(
                    endpoint_path=endpoint_path,
                    endpoint_label=endpoint_label,
                    support_label="SSE 探针未返回事件",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    status_code=200,
                    guard_result=ContentGuardProbeService.review_result(
                        "SSE 探针未返回任何事件",
                        category="invalid_sse_stream",
                    ),
                )
            saw_done = False
            for event in events:
                if "data: [DONE]" in event.replace("\r", ""):
                    saw_done = True
                    continue
                if saw_done and event.strip():
                    return ContentGuardProbeService.probe_failure(
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
                    )
                for line in event.splitlines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    result = ContentGuardService.inspect_sse_event(
                        data,
                        endpoint_path=endpoint_path,
                        rules_json=ContentGuardProbeService.rules_json(),
                        url_check_enabled=ContentGuardProbeService.url_check_enabled(),
                    )
                    if result.result != ContentGuardService.RESULT_PASS:
                        return ContentGuardProbeService.probe_failure(
                            endpoint_path=endpoint_path,
                            endpoint_label=endpoint_label,
                            support_label="SSE 探针未通过",
                            latency_ms=int((time.perf_counter() - started) * 1000),
                            status_code=200,
                            guard_result=result,
                        )
            if not saw_done:
                return ContentGuardProbeService.probe_failure(
                    endpoint_path=endpoint_path,
                    endpoint_label=endpoint_label,
                    support_label="SSE 探针缺少终止符",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    status_code=200,
                    guard_result=ContentGuardProbeService.review_result(
                        "SSE 流缺少 [DONE] 终止符",
                        category="sse_missing_done",
                    ),
                )
            return ContentGuardProbeService.probe_success(
                endpoint_path=endpoint_path,
                endpoint_label=endpoint_label,
                support_label="SSE 探针通过",
                latency_ms=int((time.perf_counter() - started) * 1000),
                status_code=200,
                message="SSE 事件与终止符正常",
                trace=[],
            )
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            status_code = getattr(exc, "status_code", None)
            message = str(exc)
            return ContentGuardProbeService.mark_detection_result({
                "endpoint_path": endpoint_path,
                "endpoint_label": endpoint_label,
                "success": False,
                "native_success": False,
                "adapted_success": False,
                "support_mode": "unsupported",
                "support_label": "SSE 探针失败",
                "latency_ms": latency_ms,
                "status_code": status_code,
                "message": message,
                "trace": [],
                "retryable": False,
            })
        finally:
            if stream_context is not None:
                await stream_context.__aexit__(exc_type, exc_value, exc_traceback)

    @staticmethod
    async def probe_pollution_rules(provider: Provider, provider_model: ProviderModel, *, endpoint_path: str) -> dict[str, Any]:
        endpoint_label = "外链广告识别探针"
        started = time.perf_counter()
        url_check_enabled = ContentGuardProbeService.url_check_enabled()
        scenarios = [
            {
                "key": "simple_answer",
                "label": "普通问答",
                "prompt": "请用中文直接回答：1+1 等于几？只输出答案，不要提供链接、优惠、社群、联系方式或赞助信息。",
            },
            {
                "key": "knowledge_answer",
                "label": "知识问答",
                "prompt": "请用一句中文解释什么是大语言模型。不要输出外部链接、优惠码、推广语、社群引导、联系方式或赞助信息。",
            },
            {
                "key": "translation_answer",
                "label": "翻译问答",
                "prompt": "请把 hello world 翻译为中文，只输出译文，不要附加任何链接、优惠、联系方式、社群或广告内容。",
            },
        ]
        detections: list[dict[str, Any]] = []
        trace: list[dict[str, Any]] = []
        total_deadline = started + ContentGuardProbeService.POLLUTION_PROBE_TOTAL_TIMEOUT_SECONDS
        for scenario in scenarios:
            remaining_seconds = total_deadline - time.perf_counter()
            if remaining_seconds <= 0:
                failure = ContentGuardProbeService.probe_failure(
                    endpoint_path=endpoint_path,
                    endpoint_label=endpoint_label,
                    support_label="外链广告识别探针超时",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    status_code=None,
                    guard_result=ContentGuardProbeService.review_result(
                        "外链广告识别探针总耗时超过限制",
                        category="pollution_probe_timeout",
                    ),
                )
                failure["detections"] = detections
                return failure
            payload = ContentGuardProbeService.build_pollution_probe_payload(
                provider_model,
                endpoint_path=endpoint_path,
                prompt=scenario["prompt"],
            )
            scenario_timeout = max(
                0.1,
                min(ContentGuardProbeService.POLLUTION_PROBE_SCENARIO_TIMEOUT_SECONDS, remaining_seconds),
            )
            try:
                response, _latency_ms, status_code, fallback_trace, error_result = await asyncio.wait_for(
                    ContentGuardProbeService.send_content_probe_json(
                        provider,
                        provider_model,
                        endpoint_path=endpoint_path,
                        payload=payload,
                        endpoint_label=endpoint_label,
                    ),
                    timeout=scenario_timeout,
                )
            except asyncio.TimeoutError:
                failure = ContentGuardProbeService.probe_failure(
                    endpoint_path=endpoint_path,
                    endpoint_label=endpoint_label,
                    support_label="外链广告识别探针超时",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    status_code=None,
                    guard_result=ContentGuardProbeService.review_result(
                        f"{scenario['label']}检测超过 {scenario_timeout:.1f}s 限制",
                        category="pollution_probe_timeout",
                    ),
                )
                failure["detections"] = detections
                return failure
            trace.extend(fallback_trace or [])
            if error_result is not None:
                error_result["detections"] = detections
                return error_result
            structure_guard = ContentGuardProbeService.inspect_probe_json_response(
                response,
                provider=provider,
                provider_model=provider_model,
                endpoint_path=endpoint_path,
                request_payload=payload,
            )
            if structure_guard.result != ContentGuardService.RESULT_PASS:
                failure = ContentGuardProbeService.probe_failure(
                    endpoint_path=endpoint_path,
                    endpoint_label=endpoint_label,
                    support_label="外链广告识别探针未通过",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    status_code=status_code,
                    guard_result=structure_guard,
                )
                failure["detections"] = detections
                return failure
            ProxyService = _proxy_service()
            output_text = (ProxyService._extract_response_text(response or {}, limit_bytes=2048) or "").strip()
            if not output_text:
                failure = ContentGuardProbeService.probe_failure(
                    endpoint_path=endpoint_path,
                    endpoint_label=endpoint_label,
                    support_label="外链广告识别探针未通过",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    status_code=status_code,
                    guard_result=ContentGuardProbeService.review_result(
                        f"{scenario['label']}未返回可检测文本",
                        category="pollution_probe_empty_response",
                    ),
                )
                failure["detections"] = detections
                return failure
            text_guard = ContentGuardService.inspect_response_text(
                output_text,
                provider=provider,
                endpoint_path=endpoint_path,
                request_payload=payload,
                rules_json=ContentGuardProbeService.rules_json(),
                url_check_enabled=url_check_enabled,
            )
            categories = set(text_guard.categories or [])
            detections.append(
                {
                    "key": scenario["key"],
                    "label": scenario["label"],
                    "detected": text_guard.result != ContentGuardService.RESULT_PASS,
                    "result": text_guard.result,
                    "risk_level": text_guard.risk_level,
                    "categories": sorted(categories),
                    "reason": text_guard.reason,
                    "excerpt": output_text[:300],
                }
            )
            if text_guard.result != ContentGuardService.RESULT_PASS:
                failure = ContentGuardProbeService.probe_failure(
                    endpoint_path=endpoint_path,
                    endpoint_label=endpoint_label,
                    support_label="外链广告识别探针未通过",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    status_code=status_code,
                    guard_result=text_guard,
                )
                failure["detections"] = detections
                return failure
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
        return result

    @staticmethod
    async def probe_tools(provider: Provider, provider_model: ProviderModel, *, endpoint_path: str) -> dict[str, Any]:
        endpoint_label = "工具调用完整性探针"
        payload = ContentGuardProbeService.build_tools_payload(provider_model, endpoint_path=endpoint_path)
        response, latency_ms, status_code, trace, error_result = await ContentGuardProbeService.send_content_probe_json(
            provider,
            provider_model,
            endpoint_path=endpoint_path,
            payload=payload,
            endpoint_label=endpoint_label,
        )
        if error_result is not None:
            return error_result
        if endpoint_path == "/chat/completions":
            choices = response.get("choices") if isinstance(response, dict) else None
            tool_calls = None
            if isinstance(choices, list) and choices:
                first_choice = choices[0] if isinstance(choices[0], dict) else {}
                message = first_choice.get("message") if isinstance(first_choice, dict) else {}
                if isinstance(message, dict):
                    tool_calls = message.get("tool_calls")
        else:
            ProxyService = _proxy_service()
            tool_calls = ProxyService._extract_tool_calls_from_responses_output(response or {})
        if not isinstance(tool_calls, list) or not tool_calls:
            return ContentGuardProbeService.probe_failure(
                endpoint_path=endpoint_path,
                endpoint_label=endpoint_label,
                support_label="工具调用探针未通过",
                latency_ms=latency_ms,
                status_code=status_code,
                guard_result=ContentGuardProbeService.review_result(
                    "未返回工具调用",
                    category="missing_tool_call",
                ),
            )
        first_call = tool_calls[0] if isinstance(tool_calls[0], dict) else {}
        function = first_call.get("function") if isinstance(first_call, dict) else None
        tool_name = None
        arguments = None
        if isinstance(function, dict):
            tool_name = function.get("name")
            arguments = function.get("arguments")
        elif isinstance(first_call, dict):
            tool_name = first_call.get("name")
            arguments = first_call.get("arguments")
        if tool_name != ContentGuardProbeService.TOOL_NAME:
            return ContentGuardProbeService.probe_failure(
                endpoint_path=endpoint_path,
                endpoint_label=endpoint_label,
                support_label="工具调用探针未通过",
                latency_ms=latency_ms,
                status_code=status_code,
                guard_result=ContentGuardProbeService.review_result(
                    "工具名被篡改",
                    category="tool_name_mismatch",
                    excerpt=str(tool_name or "")[:200],
                ),
            )
        try:
            parsed_arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
        except Exception:
            parsed_arguments = None
        if not isinstance(parsed_arguments, dict):
            return ContentGuardProbeService.probe_failure(
                endpoint_path=endpoint_path,
                endpoint_label=endpoint_label,
                support_label="工具调用探针未通过",
                latency_ms=latency_ms,
                status_code=status_code,
                guard_result=ContentGuardProbeService.review_result(
                    "工具参数不是对象",
                    category="tool_arguments_invalid",
                    excerpt=str(arguments or "")[:300],
                ),
            )
        expected_args = ContentGuardProbeService.TOOL_ARGS
        if parsed_arguments.get("order_id") != expected_args["order_id"] or parsed_arguments.get("city") != expected_args["city"]:
            return ContentGuardProbeService.probe_failure(
                endpoint_path=endpoint_path,
                endpoint_label=endpoint_label,
                support_label="工具调用探针未通过",
                latency_ms=latency_ms,
                status_code=status_code,
                guard_result=ContentGuardProbeService.review_result(
                    "工具参数与期望值不一致",
                    category="tool_arguments_mismatch",
                    excerpt=str(parsed_arguments)[:300],
                ),
            )
        return ContentGuardProbeService.probe_success(
            endpoint_path=endpoint_path,
            endpoint_label=endpoint_label,
            support_label="工具调用探针通过",
            latency_ms=latency_ms,
            status_code=status_code,
            message="工具名与参数一致",
            trace=trace,
        )

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
    def probe_failure(
        *,
        endpoint_path: str,
        endpoint_label: str,
        support_label: str,
        latency_ms: int,
        status_code: int | None,
        guard_result: ContentGuardResult,
    ) -> dict[str, Any]:
        message = f"内容完整性探针失败：{guard_result.reason or guard_result.result}"
        return ContentGuardProbeService.mark_detection_result({
            "endpoint_path": endpoint_path,
            "endpoint_label": endpoint_label,
            "success": False,
            "native_success": False,
            "adapted_success": False,
            "support_mode": "unsupported",
            "support_label": support_label,
            "latency_ms": latency_ms,
            "status_code": status_code,
            "message": message,
            "trace": [
                {
                    "result": "content_guard_probe_failed",
                    "endpoint": endpoint_path,
                    "latency_ms": latency_ms,
                    "content_guard": guard_result.to_log_kwargs(),
                }
            ],
            "retryable": False,
            "content_guard": guard_result.to_log_kwargs(),
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
        if not missing_keys and not failed_results:
            return {
                "content_guard_result": ContentGuardService.RESULT_PASS,
                "content_guard_risk_level": "low",
                "content_guard_categories_json": dumps_json([]),
                "content_guard_reason": "固定答案、外链广告识别、流式污染检测探针全部通过",
                "content_guard_action": "allow",
            }
        categories = ["content_trust_probe_incomplete"] if missing_keys else ["content_trust_probe_failed"]
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
        result = ContentGuardService.RESULT_BLOCK if fallback_result == ContentGuardService.RESULT_BLOCK else ContentGuardService.RESULT_REVIEW
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
    ) -> None:
        now = datetime.utcnow()
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
            "status": provider_model.content_integrity_status,
            "results": probe_results,
            "trust_required_keys": sorted(ContentGuardProbeService.TRUST_PROBE_KEYS),
            "last_result": ContentGuardProbeService.summarize_probe_result({"content_guard": content_guard_result}),
        }
        if result == ContentGuardService.RESULT_PASS:
            provider_model.content_probe_last_passed_at = now
            provider_model.content_probe_failure_count = 0
            provider_model.content_integrity_status = "passed"
            provider_model.circuit_state = "closed"
            provider_model.circuit_opened_at = None
            serialized_results["status"] = provider_model.content_integrity_status
            provider_model.content_probe_results_json = dumps_json(serialized_results)
            ProviderService.refresh_provider_state(provider)
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
        if provider_model.content_probe_failure_count >= 3:
            provider_model.content_integrity_status = "blocked"
            provider_model.circuit_state = "open"
            provider_model.circuit_opened_at = now
            provider.content_integrity_status = "blocked"
            provider.circuit_state = "open"
        else:
            provider_model.content_integrity_status = "degraded"
            if provider.content_integrity_status != "blocked":
                provider.content_integrity_status = "degraded"
        serialized_results["status"] = provider_model.content_integrity_status
        provider_model.content_probe_results_json = dumps_json(serialized_results)
        ProviderService.refresh_provider_state(provider)
        ProviderService.invalidate_provider_runtime_cache()
        db.commit()
