from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.provider import Provider
from app.models.provider_model import ProviderModel
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
        from app.services.proxy_service import ProxyService

        text = buffered_bytes.decode("utf-8", errors="ignore")
        max_scan_bytes = int(getattr(setting, "content_guard_max_scan_bytes", 16384) or 16384)
        event_buffer = bytearray(buffered_bytes)
        combined_result = ContentGuardResult(
            result=ContentGuardRuleService.RESULT_PASS,
            risk_level="low",
            reason="流式缓冲片段通过内容完整性审核",
            action="allow",
        )
        for data in ProxyService._consume_sse_data_payloads(event_buffer):
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
        from app.services.proxy_service import ProxyService

        max_scan_bytes = int(getattr(setting, "content_guard_max_scan_bytes", 16384) or 16384)
        ProxyService._append_limited_bytes(event_buffer, chunk, limit_bytes=max_scan_bytes)
        combined_result = ContentGuardResult(
            result=ContentGuardRuleService.RESULT_PASS,
            risk_level="low",
            reason="流式分块通过内容完整性审核",
            action="allow",
        )
        for data in ProxyService._consume_sse_data_payloads(event_buffer):
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
        from app.services.proxy_service import ProxyService

        return ProxyService._build_content_guard_error_detail(
            guard_result=guard_result,
            trace_id=trace_id,
            retried=retried,
            final=final,
        )

    @staticmethod
    async def record_runtime_violation(
        *,
        db: Session | None,
        provider_id: int,
        provider_model_id: int,
        guard_result: Any,
    ) -> None:
        from app.services.proxy_service import ProxyService

        await ProxyService._run_db_write(
            ProxyService._record_content_guard_violation_by_id,
            provider_id,
            provider_model_id,
            guard_result,
            db=db,
        )

    @staticmethod
    def enabled_for_request(*, setting: Any, provider: Provider, route_context: Any) -> bool:
        if not bool(getattr(setting, "content_guard_enabled", True)):
            return False
        if route_context is not None and not bool(getattr(route_context, "content_guard_required", True)):
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
            and getattr(route_context, "content_guard_required", True)
            and bool(getattr(provider, "buffer_stream_for_guard", True))
        )
