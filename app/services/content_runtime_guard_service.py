from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session
from starlette import status
from starlette.concurrency import run_in_threadpool

from app.database import SessionLocal
from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.services.openai_error_service import OpenAIErrorService
from app.services.content_guard_rule_service import ContentGuardRuleService
from app.services.content_guard_service import ContentGuardResult


class ContentRuntimeGuardService:
    """请求过程中防护：正式响应的非流式与流式实时检测、策略决策和违规记录。"""

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
            max_scan_bytes=int(getattr(setting, "content_guard_max_scan_bytes", 16384) or 16384),
            rules_json=getattr(setting, "content_guard_rules_json", ""),
            url_allowlist=getattr(setting, "content_guard_url_allowlist_json", ""),
            url_check_enabled=bool(getattr(setting, "content_guard_url_check_enabled", True)),
        )
        if ContentRuntimeGuardService.should_record_violation(guard_result, setting=setting):
            await ContentRuntimeGuardService.record_runtime_violation(
                db=db,
                provider_id=provider.id,
                provider_model_id=provider_model.id,
                guard_result=guard_result,
            )
        elif guard_result.result == ContentGuardRuleService.RESULT_PASS and getattr(provider_model, "content_integrity_status", "unknown") != "blocked":
            provider_model.content_probe_last_passed_at = datetime.utcnow()
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
    ) -> ContentGuardResult:
        text = buffered_bytes.decode("utf-8", errors="ignore")
        max_scan_bytes = int(getattr(setting, "content_guard_max_scan_bytes", 16384) or 16384)
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
    ) -> ContentGuardResult:
        max_scan_bytes = int(getattr(setting, "content_guard_max_scan_bytes", 16384) or 16384)
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
            )
            if current.result == ContentGuardRuleService.RESULT_BLOCK:
                return current
            if current.result == ContentGuardRuleService.RESULT_REVIEW:
                combined_result = current
        if combined_result.result == ContentGuardRuleService.RESULT_PASS and event_buffer:
            text_result = ContentGuardRuleService.inspect_response_text(
                event_buffer.decode("utf-8", errors="ignore"),
                endpoint_path=endpoint_path,
                request_payload=request_payload,
                max_scan_bytes=max_scan_bytes,
                rules_json=getattr(setting, "content_guard_rules_json", ""),
                url_allowlist=getattr(setting, "content_guard_url_allowlist_json", ""),
                url_check_enabled=bool(getattr(setting, "content_guard_url_check_enabled", True)),
            )
            if len(event_buffer) >= max_scan_bytes:
                event_buffer.clear()
            if text_result.result != ContentGuardRuleService.RESULT_PASS:
                return text_result
        return combined_result

    @staticmethod
    def decide_runtime_action(guard_result: ContentGuardResult, *, setting: Any) -> str:
        if ContentRuntimeGuardService.high_risk_strategy(setting) == "record_only":
            guard_result.final_strategy = "record_only"
            guard_result.action = "record"
            return "record"
        should_block = ContentGuardRuleService.should_block(guard_result, setting=setting)
        if should_block:
            strategy = ContentRuntimeGuardService.high_risk_strategy(setting)
            guard_result.final_strategy = strategy
            return strategy
        if guard_result.result == ContentGuardRuleService.RESULT_REVIEW and bool(getattr(setting, "content_guard_async_review_enabled", True)):
            guard_result.final_strategy = "async_review"
            guard_result.action = "async_review"
            return "async_review"
        return "allow"

    @staticmethod
    def should_block_for_response(guard_result: ContentGuardResult, *, setting: Any) -> bool:
        return ContentRuntimeGuardService.decide_runtime_action(guard_result, setting=setting) in {"block", "switch_provider", "safe_error"}

    @staticmethod
    def should_switch_provider(setting: Any) -> bool:
        return ContentRuntimeGuardService.high_risk_strategy(setting) == "switch_provider"

    @staticmethod
    def build_guard_error(*, guard_result: ContentGuardResult, trace_id: str | None, retried: bool, final: bool = False) -> dict[str, Any]:
        message = (
            "所有可用渠道的上游响应均未通过内容完整性防护，已阻断返回。"
            if final
            else "当前渠道的上游响应未通过内容完整性防护，已尝试切换其它可用渠道。"
            if retried
            else "上游响应未通过内容完整性防护，已阻断返回。"
        )
        detail = {
            "message": message,
            "code": "content_integrity_violation",
            "content_guard": {
                "result": guard_result.result,
                "risk_level": guard_result.risk_level,
                "categories": guard_result.categories,
                "reason": guard_result.reason,
                "action": guard_result.action,
            },
        }
        classified = OpenAIErrorService.classify_error(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)
        return OpenAIErrorService.build_error_payload(
            message=message,
            code="content_integrity_violation",
            trace_id=trace_id,
            error_type=str(classified["error_type"]),
            retryable=True if retried and not final else bool(classified["retryable"]),
            recoverable=True if retried and not final else bool(classified["recoverable"]),
            category=str(classified["category"]),
            status_code=status.HTTP_502_BAD_GATEWAY,
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
        return ContentRuntimeGuardService.should_block_for_response(guard_result, setting=setting)

    @staticmethod
    def stream_should_buffer(*, setting: Any, provider: Provider, route_context: Any) -> bool:
        if not ContentRuntimeGuardService.enabled_for_request(
            setting=setting,
            provider=provider,
            route_context=route_context,
        ):
            return False
        trust_level = str(getattr(provider, "trust_level", "standard") or "standard")
        if (
            trust_level == "low"
            and bool(getattr(provider, "buffer_stream_for_guard", True))
            and bool(getattr(setting, "content_guard_low_trust_requires_buffer", True))
        ):
            return True
        return bool(
            route_context
            and getattr(route_context, "require_trusted_provider", False)
            and bool(getattr(provider, "buffer_stream_for_guard", True))
        )

    @staticmethod
    def append_limited_bytes(buffer: bytearray, chunk: bytes, *, limit_bytes: int) -> None:
        if limit_bytes <= 0 or len(buffer) >= limit_bytes:
            return
        remaining = limit_bytes - len(buffer)
        buffer.extend(chunk[:remaining])

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
        ContentGuardRuleService.record_violation(
            db,
            provider=provider,
            provider_model=provider_model,
            result=guard_result,
            auto_commit=False,
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
