from __future__ import annotations

import json
import time
from typing import Any

from sqlalchemy.orm import Session
from starlette import status
from starlette.concurrency import run_in_threadpool

from app.database import SessionLocal
from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.models.logging_events import RequestContentGuardEvent
from app.services.openai_error_service import OpenAIErrorService
from app.services.content_guard_rule_service import ContentGuardRuleService
from app.services.content_guard_service import ContentGuardResult
from app.utils.content_guard_config import CONTENT_GUARD_MAX_SCAN_BYTES_LIMIT
from app.utils.json_utils import dumps_json


class ContentRuntimeGuardService:
    """请求过程中防护：正式响应的非流式与流式实时检测、策略决策和违规记录。"""

    CONTENT_GUARD_ERROR_STATUS_CODE = status.HTTP_422_UNPROCESSABLE_ENTITY

    @staticmethod
    def bounded_max_scan_bytes(setting: Any) -> int:
        try:
            value = int(getattr(setting, "content_guard_max_scan_bytes", 16384) or 16384)
        except Exception:
            value = 16384
        return min(CONTENT_GUARD_MAX_SCAN_BYTES_LIMIT, max(1024, value))

    @staticmethod
    def enhanced_detection_kwargs(setting: Any) -> dict[str, Any]:
        return {
            "enhanced_detection_enabled": bool(getattr(setting, "content_guard_enhanced_detection_enabled", True)),
            "enhanced_illegal_enabled": bool(getattr(setting, "content_guard_enhanced_illegal_enabled", True)),
            "enhanced_ad_enabled": bool(getattr(setting, "content_guard_enhanced_ad_enabled", True)),
            "enhanced_custom_enabled": bool(getattr(setting, "content_guard_enhanced_custom_enabled", True)),
            "enhanced_obfuscation_enabled": bool(getattr(setting, "content_guard_enhanced_obfuscation_enabled", True)),
            "enhanced_threshold": int(getattr(setting, "content_guard_enhanced_threshold", 70) or 70),
            "enhanced_context_window_chars": int(getattr(setting, "content_guard_enhanced_context_window_chars", 96) or 96),
        }

    @staticmethod
    async def inspect_non_stream_response(
        *,
        db: Session | None,
        setting: Any,
        provider: Provider,
        provider_model: ProviderModel,
        endpoint_path: str,
        request_payload: dict[str, Any],
        response_payload: dict[str, Any],
        route_context: Any,
    ) -> ContentGuardResult:
        if not ContentRuntimeGuardService.enabled_for_request(
            setting=setting,
            provider=provider,
            route_context=route_context,
        ):
            return ContentGuardResult(
                result=ContentGuardRuleService.RESULT_PASS,
                risk_level="low",
                reason="内容完整性防护未启用",
                action="allow",
            )
        guard_result = ContentGuardRuleService.inspect_json_response(
            response_payload,
            provider=provider,
            provider_model=provider_model,
            endpoint_path=endpoint_path,
            request_payload=request_payload,
            max_scan_bytes=ContentRuntimeGuardService.bounded_max_scan_bytes(setting),
            rules_json=getattr(setting, "content_guard_rules_json", ""),
            url_allowlist=getattr(setting, "content_guard_url_allowlist_json", ""),
            url_check_enabled=bool(getattr(setting, "content_guard_url_check_enabled", True)),
            **ContentRuntimeGuardService.enhanced_detection_kwargs(setting),
        )
        if ContentRuntimeGuardService.should_record_violation(guard_result, setting=setting):
            await ContentRuntimeGuardService.record_runtime_violation(
                db=db,
                provider_id=provider.id,
                provider_model_id=provider_model.id,
                guard_result=guard_result,
            )
        elif guard_result.result == ContentGuardRuleService.RESULT_PASS and getattr(provider_model, "content_integrity_status", "unknown") != "blocked":
            ContentRuntimeGuardService.record_runtime_pass(
                db=db,
                provider=provider,
                provider_model=provider_model,
            )
        return guard_result

    @staticmethod
    async def inspect_stream_prefetch_buffer(
        *,
        db: Session | None,
        setting: Any,
        provider: Provider,
        provider_model: ProviderModel,
        endpoint_path: str,
        request_payload: dict[str, Any],
        buffered_bytes: bytes,
        route_context: Any = None,
    ) -> ContentGuardResult:
        if not ContentRuntimeGuardService.enabled_for_request(
            setting=setting,
            provider=provider,
            route_context=route_context,
        ):
            return ContentGuardResult(
                result=ContentGuardRuleService.RESULT_PASS,
                risk_level="low",
                reason="内容完整性防护未启用",
                action="allow",
            )
        text = buffered_bytes.decode("utf-8", errors="ignore")
        max_scan_bytes = ContentRuntimeGuardService.bounded_max_scan_bytes(setting)
        event_buffer = bytearray(buffered_bytes)
        combined_result = ContentGuardResult(
            result=ContentGuardRuleService.RESULT_PASS,
            risk_level="low",
            reason="流式缓冲片段通过内容完整性审核",
            action="allow",
        )
        for data in ContentRuntimeGuardService.consume_sse_data_payloads(event_buffer):
            current = ContentGuardRuleService.inspect_sse_event(
                data,
                endpoint_path=endpoint_path,
                request_payload=request_payload,
                max_scan_bytes=max_scan_bytes,
                rules_json=getattr(setting, "content_guard_rules_json", ""),
                url_allowlist=getattr(setting, "content_guard_url_allowlist_json", ""),
                url_check_enabled=bool(getattr(setting, "content_guard_url_check_enabled", True)),
                **ContentRuntimeGuardService.enhanced_detection_kwargs(setting),
            )
            if current.result == ContentGuardRuleService.RESULT_BLOCK:
                combined_result = current
                break
            if current.result == ContentGuardRuleService.RESULT_REVIEW:
                combined_result = current
        if combined_result.result == ContentGuardRuleService.RESULT_PASS:
            text_result = ContentGuardRuleService.inspect_response_text(
                text,
                provider=provider,
                endpoint_path=endpoint_path,
                request_payload=request_payload,
                max_scan_bytes=max_scan_bytes,
                rules_json=getattr(setting, "content_guard_rules_json", ""),
                url_allowlist=getattr(setting, "content_guard_url_allowlist_json", ""),
                url_check_enabled=bool(getattr(setting, "content_guard_url_check_enabled", True)),
                **ContentRuntimeGuardService.enhanced_detection_kwargs(setting),
            )
            if text_result.result != ContentGuardRuleService.RESULT_PASS:
                combined_result = text_result
        if ContentRuntimeGuardService.should_record_violation(combined_result, setting=setting):
            await ContentRuntimeGuardService.record_runtime_violation(
                db=db,
                provider_id=provider.id,
                provider_model_id=provider_model.id,
                guard_result=combined_result,
            )
        return combined_result

    @staticmethod
    def inspect_stream_chunk(
        *,
        event_buffer: bytearray,
        chunk: bytes,
        setting: Any,
        endpoint_path: str,
        request_payload: dict[str, Any] | None,
        provider: Provider | None = None,
        route_context: Any = None,
        text_window: bytearray | None = None,
    ) -> ContentGuardResult:
        if provider is not None and not ContentRuntimeGuardService.enabled_for_request(
            setting=setting,
            provider=provider,
            route_context=route_context,
        ):
            return ContentGuardResult(
                result=ContentGuardRuleService.RESULT_PASS,
                risk_level="low",
                reason="内容完整性防护未启用",
                action="allow",
            )
        max_scan_bytes = ContentRuntimeGuardService.bounded_max_scan_bytes(setting)
        ContentRuntimeGuardService.append_limited_bytes(event_buffer, chunk, limit_bytes=max_scan_bytes)
        combined_result = ContentGuardResult(
            result=ContentGuardRuleService.RESULT_PASS,
            risk_level="low",
            reason="流式分块通过内容完整性审核",
            action="allow",
        )
        for data in ContentRuntimeGuardService.consume_sse_data_payloads(event_buffer):
            current = ContentGuardRuleService.inspect_sse_event(
                data,
                endpoint_path=endpoint_path,
                request_payload=request_payload,
                max_scan_bytes=max_scan_bytes,
                rules_json=getattr(setting, "content_guard_rules_json", ""),
                url_allowlist=getattr(setting, "content_guard_url_allowlist_json", ""),
                url_check_enabled=bool(getattr(setting, "content_guard_url_check_enabled", True)),
                **ContentRuntimeGuardService.enhanced_detection_kwargs(setting),
            )
            if current.result == ContentGuardRuleService.RESULT_BLOCK:
                ContentRuntimeGuardService.apply_runtime_action(current, setting=setting)
                return current
            if current.result == ContentGuardRuleService.RESULT_REVIEW:
                combined_result = current
            if text_window is not None:
                delta_text = ContentRuntimeGuardService.extract_sse_scan_text(
                    data,
                    endpoint_path=endpoint_path,
                    max_scan_bytes=max_scan_bytes,
                )
                if delta_text:
                    ContentRuntimeGuardService.append_limited_text_window(
                        text_window,
                        delta_text,
                        limit_bytes=max_scan_bytes,
                    )
                    window_result = ContentGuardRuleService.inspect_response_text(
                        text_window.decode("utf-8", errors="ignore"),
                        endpoint_path=endpoint_path,
                        request_payload=request_payload,
                        max_scan_bytes=max_scan_bytes,
                        rules_json=getattr(setting, "content_guard_rules_json", ""),
                        url_allowlist=getattr(setting, "content_guard_url_allowlist_json", ""),
                        url_check_enabled=bool(getattr(setting, "content_guard_url_check_enabled", True)),
                        **ContentRuntimeGuardService.enhanced_detection_kwargs(setting),
                    )
                    if window_result.result == ContentGuardRuleService.RESULT_BLOCK:
                        ContentRuntimeGuardService.apply_runtime_action(window_result, setting=setting)
                        return window_result
                    if window_result.result == ContentGuardRuleService.RESULT_REVIEW:
                        combined_result = window_result
        residual_text = ContentRuntimeGuardService.extract_pending_sse_data_text(event_buffer)
        if combined_result.result == ContentGuardRuleService.RESULT_PASS and residual_text:
            text_result = ContentGuardRuleService.inspect_response_text(
                residual_text,
                endpoint_path=endpoint_path,
                request_payload=request_payload,
                max_scan_bytes=max_scan_bytes,
                rules_json=getattr(setting, "content_guard_rules_json", ""),
                url_allowlist=getattr(setting, "content_guard_url_allowlist_json", ""),
                url_check_enabled=bool(getattr(setting, "content_guard_url_check_enabled", True)),
                **ContentRuntimeGuardService.enhanced_detection_kwargs(setting),
            )
            if len(event_buffer) >= max_scan_bytes:
                keep_bytes = max(512, min(4096, max_scan_bytes // 4))
                del event_buffer[:-keep_bytes]
            if text_result.result != ContentGuardRuleService.RESULT_PASS:
                ContentRuntimeGuardService.apply_runtime_action(text_result, setting=setting)
                return text_result
        ContentRuntimeGuardService.trim_stream_window(event_buffer, max_scan_bytes=max_scan_bytes)
        if combined_result.result != ContentGuardRuleService.RESULT_PASS:
            ContentRuntimeGuardService.apply_runtime_action(combined_result, setting=setting)
        return combined_result

    @staticmethod
    def decide_runtime_action(guard_result: ContentGuardResult, *, setting: Any) -> str:
        return ContentRuntimeGuardService.resolve_runtime_action(guard_result, setting=setting)

    @staticmethod
    def resolve_runtime_action(guard_result: ContentGuardResult, *, setting: Any) -> str:
        if guard_result.result == ContentGuardRuleService.RESULT_PASS:
            return "allow"
        if (
            ContentRuntimeGuardService.high_risk_strategy(setting) == "record_only"
            or not bool(getattr(setting, "content_guard_block_on_high_risk", True))
        ):
            return "record"
        should_block = ContentGuardRuleService.should_block(guard_result, setting=setting)
        if should_block:
            return ContentRuntimeGuardService.high_risk_strategy(setting)
        if guard_result.result == ContentGuardRuleService.RESULT_REVIEW and bool(getattr(setting, "content_guard_async_review_enabled", True)):
            return "async_review"
        return "allow"

    @staticmethod
    def apply_runtime_action(guard_result: ContentGuardResult, *, setting: Any) -> str:
        action = ContentRuntimeGuardService.resolve_runtime_action(guard_result, setting=setting)
        if action == "record":
            guard_result.final_strategy = "record_only"
            guard_result.action = "record"
        elif action in {"block", "switch_provider", "safe_error"}:
            guard_result.final_strategy = action
        elif action == "async_review":
            guard_result.final_strategy = "async_review"
            guard_result.action = "async_review"
        return action

    @staticmethod
    def should_block_for_response(guard_result: ContentGuardResult, *, setting: Any) -> bool:
        return ContentRuntimeGuardService.decide_runtime_action(guard_result, setting=setting) in {"block", "switch_provider", "safe_error"}

    @staticmethod
    def should_emit_safe_error(guard_result: ContentGuardResult, *, setting: Any) -> bool:
        if ContentRuntimeGuardService.decide_runtime_action(guard_result, setting=setting) != "safe_error":
            return False
        guard_result.final_strategy = "safe_error"
        return True

    @staticmethod
    def should_switch_provider(setting: Any) -> bool:
        return ContentRuntimeGuardService.high_risk_strategy(setting) == "switch_provider"

    @staticmethod
    def build_guard_error(*, guard_result: ContentGuardResult, trace_id: str | None, retried: bool, final: bool = False) -> dict[str, Any]:
        is_safe_error = str(getattr(guard_result, "final_strategy", "") or "") == "safe_error"
        message = (
            "上游响应未通过内容完整性防护，系统已返回安全错误内容。"
            if is_safe_error
            else
            "所有可用提供商的上游响应均未通过内容完整性防护，已阻断返回。"
            if final
            else "当前提供商的上游响应未通过内容完整性防护，已尝试切换其它可用提供商。"
            if retried
            else "上游响应未通过内容完整性防护，已阻断返回。"
        )
        content_guard_detail = {
            "result": guard_result.result,
            "risk_level": guard_result.risk_level,
            "categories": guard_result.categories,
            "reason": guard_result.reason,
            "action": guard_result.action,
            "excerpt": guard_result.excerpt,
            "confidence": guard_result.confidence,
            "score_delta": guard_result.score_delta,
            "matched_rules": guard_result.matched_rules,
            "latency_ms": guard_result.latency_ms,
            "buffer_wait_ms": guard_result.buffer_wait_ms,
            "final_strategy": guard_result.final_strategy,
            "retried": retried,
            "final": final,
        }
        detail = {
            "message": message,
            "code": "content_integrity_violation",
            "content_guard": content_guard_detail,
        }
        classified = OpenAIErrorService.classify_error(
            status_code=ContentRuntimeGuardService.CONTENT_GUARD_ERROR_STATUS_CODE,
            detail=detail,
        )
        return OpenAIErrorService.build_error_payload(
            message=message,
            code="content_integrity_safe_error" if is_safe_error else "content_integrity_violation",
            trace_id=trace_id,
            error_type=str(classified["error_type"]),
            retryable=False,
            recoverable=False,
            category=str(classified["category"]),
            status_code=ContentRuntimeGuardService.CONTENT_GUARD_ERROR_STATUS_CODE,
            detail=detail,
        )

    @staticmethod
    async def record_runtime_violation(
        *,
        db: Session | None,
        provider_id: int,
        provider_model_id: int,
        guard_result: Any,
    ) -> None:
        if db is not None:
            await run_in_threadpool(
                ContentRuntimeGuardService.record_content_guard_violation_by_id,
                db,
                provider_id,
                provider_model_id,
                guard_result,
            )
            return
        await run_in_threadpool(
            ContentRuntimeGuardService.record_content_guard_violation_by_id_in_new_session,
            provider_id,
            provider_model_id,
            guard_result,
        )

    @staticmethod
    def enabled_for_request(*, setting: Any, provider: Provider, route_context: Any) -> bool:
        if not bool(getattr(setting, "content_guard_enabled", True)):
            return False
        if route_context is not None and getattr(route_context, "content_guard_required", None) is False:
            return False
        return bool(getattr(provider, "content_guard_enabled", True))

    @staticmethod
    def high_risk_strategy(setting: Any) -> str:
        strategy = str(getattr(setting, "content_guard_high_risk_strategy", "switch_provider") or "switch_provider")
        return strategy if strategy in {"block", "switch_provider", "record_only", "safe_error"} else "switch_provider"

    @staticmethod
    def stream_mode(setting: Any) -> str:
        mode = str(getattr(setting, "content_guard_stream_mode", "buffer_300ms") or "buffer_300ms")
        return mode if mode in {"pass_through_scan", "buffer_300ms", "full_buffer"} else "buffer_300ms"

    @staticmethod
    def should_record_violation(guard_result: ContentGuardResult, *, setting: Any) -> bool:
        action = ContentRuntimeGuardService.apply_runtime_action(guard_result, setting=setting)
        return action in {"block", "switch_provider", "safe_error", "async_review", "record"}

    @staticmethod
    def record_runtime_pass(
        *,
        db: Session | None,
        provider: Provider,
        provider_model: ProviderModel,
    ) -> None:
        # 正式请求通过不等同于可信探针通过，避免混写 content_probe_* 可信探针状态。
        return None

    @staticmethod
    def stream_should_buffer(*, setting: Any, provider: Provider, route_context: Any) -> bool:
        if not ContentRuntimeGuardService.enabled_for_request(
            setting=setting,
            provider=provider,
            route_context=route_context,
        ):
            return False
        trust_level = str(getattr(provider, "trust_level", "standard") or "standard")
        stream_mode = ContentRuntimeGuardService.stream_mode(setting)
        content_status = str(getattr(provider, "content_integrity_status", "unknown") or "unknown")
        health_status = str(getattr(provider, "health_status", "unknown") or "unknown")
        if stream_mode in {"buffer_300ms", "full_buffer"} and (
            content_status in {"unknown", "degraded"} or health_status in {"unknown", "degraded"}
        ):
            return True
        buffer_stream_for_guard = getattr(provider, "buffer_stream_for_guard", True)
        buffer_stream_enabled = True if buffer_stream_for_guard is None else bool(buffer_stream_for_guard)
        if (
            trust_level == "low"
            and buffer_stream_enabled
            and bool(getattr(setting, "content_guard_low_trust_requires_buffer", True))
        ):
            return True
        if (
            route_context
            and getattr(route_context, "require_trusted_provider", False)
            and buffer_stream_enabled
        ):
            return True
        if trust_level in {"standard", "unknown", "trusted", "official"} and buffer_stream_enabled:
            return stream_mode in {"buffer_300ms", "full_buffer"}
        return False

    @staticmethod
    def append_limited_bytes(buffer: bytearray, chunk: bytes, *, limit_bytes: int) -> None:
        if limit_bytes <= 0 or len(buffer) >= limit_bytes:
            return
        remaining = limit_bytes - len(buffer)
        buffer.extend(chunk[:remaining])

    @staticmethod
    def append_limited_text_window(buffer: bytearray, text: str, *, limit_bytes: int) -> None:
        if limit_bytes <= 0:
            return
        buffer.extend(str(text or "").encode("utf-8", errors="ignore"))
        if len(buffer) > limit_bytes:
            del buffer[: len(buffer) - limit_bytes]

    @staticmethod
    def extract_sse_scan_text(data: str, *, endpoint_path: str | None, max_scan_bytes: int) -> str:
        stripped = str(data or "").strip()
        if not stripped or stripped == "[DONE]" or not stripped.startswith("{"):
            return ""
        try:
            payload = json.loads(stripped)
        except Exception:
            return ""
        return ContentGuardRuleService.extract_response_scan_text(
            payload,
            endpoint_path=endpoint_path,
            max_scan_bytes=max_scan_bytes,
        )

    @staticmethod
    def consume_sse_data_payloads(event_buffer: bytearray) -> list[str]:
        payloads: list[str] = []
        while True:
            separator_length = 0
            separator_index = event_buffer.find(b"\r\n\r\n")
            if separator_index >= 0:
                separator_length = 4
            else:
                separator_index = event_buffer.find(b"\n\n")
                if separator_index >= 0:
                    separator_length = 2
            if separator_index < 0:
                break
            raw_event = bytes(event_buffer[:separator_index])
            del event_buffer[: separator_index + separator_length]
            if not raw_event:
                continue
            stripped = raw_event.strip()
            if not stripped:
                continue
            if stripped.startswith(b"data:") and b"\n" not in stripped and b"\r" not in stripped:
                payload = stripped[5:].strip()
                if payload:
                    payloads.append(payload.decode("utf-8", errors="ignore"))
                continue
            event_payload_lines: list[str] = []
            for raw_line in raw_event.splitlines():
                line = raw_line.strip()
                if not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if payload:
                    event_payload_lines.append(payload.decode("utf-8", errors="ignore"))
            if event_payload_lines:
                payloads.append("\n".join(event_payload_lines))
        return payloads

    @staticmethod
    def record_content_guard_violation_by_id(
        db: Session,
        provider_id: int,
        provider_model_id: int,
        guard_result: Any,
    ) -> None:
        provider = db.get(Provider, provider_id)
        provider_model = db.get(ProviderModel, provider_model_id)
        if provider is None or provider_model is None:
            ContentRuntimeGuardService.record_unresolved_violation_event(
                db,
                provider_id=provider_id,
                provider_model_id=provider_model_id,
                provider=provider,
                provider_model=provider_model,
                guard_result=guard_result,
            )
            return
        final_strategy = str(getattr(guard_result, "final_strategy", "") or getattr(guard_result, "action", "") or "")
        severe = None
        if final_strategy in {"record_only", "record", "async_review"}:
            severe = False
        ContentGuardRuleService.record_violation(
            db,
            provider=provider,
            provider_model=provider_model,
            result=guard_result,
            severe=severe,
            auto_commit=False,
        )
        if final_strategy == "async_review":
            ContentRuntimeGuardService.record_async_review_event(
                db,
                provider=provider,
                guard_result=guard_result,
            )

    @staticmethod
    def record_unresolved_violation_event(
        db: Session,
        *,
        provider_id: int,
        provider_model_id: int,
        provider: Provider | None,
        provider_model: ProviderModel | None,
        guard_result: Any,
    ) -> None:
        missing_parts: list[str] = []
        if provider is None:
            missing_parts.append("provider")
        if provider_model is None:
            missing_parts.append("provider_model")
        db.add(
            RequestContentGuardEvent(
                guard_stage="runtime_violation_unresolved_target",
                provider_id=getattr(provider, "id", provider_id),
                provider_name=getattr(provider, "name", None),
                provider_model_id=getattr(provider_model, "id", provider_model_id),
                model_name=getattr(provider_model, "model_name", None),
                guard_result=getattr(guard_result, "result", None),
                risk_level=getattr(guard_result, "risk_level", None),
                matched_categories_json=dumps_json(getattr(guard_result, "categories", None) or []),
                matched_rules_json=guard_result.matched_rules_json()
                if hasattr(guard_result, "matched_rules_json")
                else None,
                reason=getattr(guard_result, "reason", None),
                action=str(getattr(guard_result, "final_strategy", "") or getattr(guard_result, "action", "") or ""),
                excerpt=getattr(guard_result, "excerpt", None),
                provider_status_after=getattr(provider, "content_integrity_status", None),
                confidence=getattr(guard_result, "confidence", None),
                score_delta=getattr(guard_result, "score_delta", None),
                diagnostics_json=dumps_json(
                    {
                        "missing": missing_parts,
                        "provider_id": provider_id,
                        "provider_model_id": provider_model_id,
                    }
                ),
            )
        )

    @staticmethod
    def record_content_guard_violation_by_id_in_new_session(
        provider_id: int,
        provider_model_id: int,
        guard_result: Any,
    ) -> None:
        db = SessionLocal()
        try:
            ContentRuntimeGuardService.record_content_guard_violation_by_id(
                db,
                provider_id,
                provider_model_id,
                guard_result,
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def extract_pending_sse_data_text(event_buffer: bytearray) -> str:
        payload_lines: list[str] = []
        for raw_line in bytes(event_buffer).splitlines():
            line = raw_line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload:
                payload_lines.append(payload.decode("utf-8", errors="ignore"))
        return "\n".join(payload_lines)

    @staticmethod
    def trim_stream_window(event_buffer: bytearray, *, max_scan_bytes: int) -> None:
        keep_bytes = max(512, min(4096, max(1, max_scan_bytes) // 4))
        if len(event_buffer) > keep_bytes:
            del event_buffer[:-keep_bytes]

    @staticmethod
    def record_async_review_event(
        db: Session,
        *,
        provider: Provider | None,
        guard_result: Any,
    ) -> None:
        db.add(
            RequestContentGuardEvent(
                guard_stage="runtime_async_review",
                provider_id=getattr(provider, "id", None) if provider is not None else None,
                provider_name=getattr(provider, "name", None) if provider is not None else None,
                guard_result=getattr(guard_result, "result", None),
                risk_level=getattr(guard_result, "risk_level", None),
                matched_categories_json=dumps_json(getattr(guard_result, "categories", None) or []),
                matched_rules_json=guard_result.matched_rules_json()
                if hasattr(guard_result, "matched_rules_json")
                else None,
                reason=getattr(guard_result, "reason", None),
                action="pending_review",
                excerpt=getattr(guard_result, "excerpt", None),
                provider_status_after=getattr(provider, "content_integrity_status", None) if provider is not None else None,
                confidence=getattr(guard_result, "confidence", None),
                score_delta=getattr(guard_result, "score_delta", None),
            )
        )
