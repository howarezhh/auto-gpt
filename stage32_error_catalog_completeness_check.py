from __future__ import annotations

import re
from pathlib import Path

from app.services.error_catalog_service import ErrorCatalogService
from app.services.log_service import LogService
from app.services.openai_error_service import OpenAIErrorService
from app.utils.json_utils import safeJsonParse


PUBLIC_CODES_FROM_REQUIREMENT = {
    "invalid_request",
    "request_validation_failed",
    "invalid_json_body",
    "request_body_too_large",
    "missing_model",
    "invalid_stream_mode",
    "invalid_api_key",
    "key_disabled",
    "key_expired",
    "endpoint_not_allowed",
    "source_ip_not_allowed",
    "model_not_allowed",
    "owner_user_disabled",
    "unauthorized",
    "forbidden",
    "insufficient_balance",
    "rate_limit_exceeded",
    "concurrency_limit_exceeded",
    "stream_concurrency_exceeded",
    "model_not_found",
    "model_not_available",
    "capability_not_supported",
    "chat_completions_endpoint_not_supported",
    "responses_endpoint_not_supported",
    "model_image_generation_not_available",
    "route_unavailable",
    "no_authorized_provider",
    "all_providers_failed",
    "all_providers_unavailable_after_retry",
    "upstream_request_failed",
    "upstream_connect_error",
    "upstream_read_timeout",
    "request_timeout",
    "invalid_upstream_response",
    "non_stream_response_too_large",
    "stream_first_token_timeout",
    "stream_idle_timeout",
    "stream_max_duration_exceeded",
    "stream_interrupted",
    "upstream_stream_empty",
    "client_cancelled",
    "invalid_image_file",
    "empty_image_file",
    "image_file_too_large",
    "invalid_image_count",
    "invalid_image_prompt",
    "missing_image_input",
    "invalid_image_output_format",
    "invalid_image_response_format",
    "invalid_image_output_compression",
    "resource_not_found",
    "api_key_not_found",
    "user_not_found",
    "conversation_not_found",
    "report_not_found",
    "endpoint_conversion_disabled",
    "endpoint_request_conversion_unsafe",
    "endpoint_response_conversion_unsafe",
    "responses_chat_previous_response_not_found",
    "responses_chat_adapter_tool_round_limit_exceeded",
    "invalid_responses_input",
    "request_tokens_exceeded",
    "request_token_estimation_failed",
    "model_input_tokens_exceeded",
    "model_output_tokens_exceeded",
    "output_token_limit_exceeded",
    "long_output_requires_stream",
    "unsupported_endpoint",
    "unsupported_endpoint_fallback",
    "endpoint_fallback_conversion_unsafe",
    "provider_capacity_exceeded",
    "provider_active_request_limit_exceeded",
    "provider_active_stream_limit_exceeded",
    "provider_qps_limit_exceeded",
    "provider_rpm_limit_exceeded",
    "provider_not_found",
    "provider_model_not_found",
    "chat_completion_not_found",
    "chat_completion_provider_not_available",
    "moderation_not_found",
    "moderation_provider_not_available",
    "files_not_found",
    "file_not_found",
    "file_content_not_found",
    "files_provider_not_available",
    "image_url_fetch_failed",
    "image_url_too_large_for_inline_conversion",
    "image_result_missing",
    "image_result_b64_unavailable",
    "legacy_image_result_missing",
    "legacy_images_stream_not_supported",
    "missing_function_call_output_call_id",
    "responses_chat_adapter_unsupported_input_type",
    "responses_chat_adapter_web_search_disabled",
    "responses_chat_adapter_web_search_query_missing",
    "responses_chat_adapter_stream_failed",
    "benchmark_job_running",
    "conflict",
}

INTERNAL_CODES_FROM_REQUIREMENT = {
    "responses_chat_adapter_web_search_proxy_not_configured",
    "responses_chat_adapter_web_search_proxy_failed",
    "responses_chat_adapter_snapshot_too_large",
    "provider_capacity_unavailable",
    "redis_unavailable",
    "internal_server_error",
    "request_log_queue_initialization_failed",
    "request_log_persist_failed",
    "token_finalize_failed",
    "billing_finalize_failed",
    "worker_exception",
    "db_connection_failed",
    "db_transaction_rollback",
    "db_unique_constraint_conflict",
    "cache_invalidated",
    "queue_backlog",
    "provider_health_probe_failed",
    "provider_capacity_snapshot_failed",
    "upstream_raw_error",
    "deployment_config_invalid",
    "secret_invalid",
    "postgresql_required",
    "nginx_sse_buffering_enabled",
    "unhandled_exception",
    "third_party_library_error",
    "serialization_failed",
    "assertion_failed",
}

CODE_PATTERNS = (
    re.compile(r"""["']code["']\s*:\s*["']([a-zA-Z0-9_-]+)["']"""),
    re.compile(r"""code\s*=\s*["']([a-zA-Z0-9_-]+)["']"""),
)


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _extract_code_literals() -> set[str]:
    found: set[str] = set()
    for path in Path("app").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in CODE_PATTERNS:
            found.update(pattern.findall(text))
    return found


def _assert_catalog_is_complete() -> None:
    validation_errors = ErrorCatalogService.validate_specs()
    _assert(not validation_errors, "错误目录字段不完整：\n" + "\n".join(validation_errors[:80]))

    required_codes = PUBLIC_CODES_FROM_REQUIREMENT | INTERNAL_CODES_FROM_REQUIREMENT
    missing_required = sorted(
        code
        for code in required_codes
        if code not in ErrorCatalogService.SPECS and code not in ErrorCatalogService.ALIAS_CODES
    )
    _assert(not missing_required, f"用户要求的错误码未进入统一目录：{missing_required}")

    code_literals = _extract_code_literals()
    missing_literals = sorted(
        code
        for code in code_literals
        if code not in ErrorCatalogService.SPECS and code not in ErrorCatalogService.ALIAS_CODES
    )
    _assert(not missing_literals, f"代码中存在未登记错误码：{missing_literals}")


def _assert_internal_errors_are_safely_mapped() -> None:
    for code in INTERNAL_CODES_FROM_REQUIREMENT:
        spec = ErrorCatalogService.resolve(code=code)
        _assert(spec.public is False, f"{code} 应为对内错误")
        public_spec = ErrorCatalogService.public_spec_for(spec)
        _assert(public_spec.public is True, f"{code} 未映射到公开错误")
        _assert(public_spec.code != spec.code, f"{code} 对外不应暴露内部错误码")
        payload = ErrorCatalogService.build_error_object(
            status_code=spec.status_code,
            detail={"code": code, "message": "REDIS_URL 为空，Secret=raw-secret，上游返回 <html>stack</html>"},
            trace_id="stage32-trace",
        )
        text = str(payload)
        _assert("raw-secret" not in text, f"{code} 对外泄露 Secret")
        _assert("REDIS_URL" not in text, f"{code} 对外泄露内部配置名")
        _assert("<html>" not in text, f"{code} 对外泄露上游原始 HTML")
        _assert(payload["code"] == public_spec.code, f"{code} 对外 code 映射错误")
        _assert("trace_id" in payload, f"{code} 对外缺少 trace_id")
        openai_payload = OpenAIErrorService.build_error_payload(
            message=spec.message,
            code=code,
            trace_id="stage32-trace",
            error_type=spec.error_type,
            retryable=spec.retryable,
            recoverable=spec.recoverable,
            category=spec.category,
            status_code=spec.status_code,
            detail={"code": code, "message": "REDIS_URL 为空，Secret=raw-secret，上游返回 <html>stack</html>"},
        )
        openai_text = str(openai_payload)
        _assert(openai_payload["error"]["code"] == public_spec.code, f"{code} OpenAI payload 未映射公开 code")
        _assert(openai_payload["error"]["type"] == public_spec.error_type, f"{code} OpenAI payload 未映射公开 type")
        _assert(openai_payload["error"]["category"] == public_spec.category, f"{code} OpenAI payload 未映射公开 category")
        _assert("raw-secret" not in openai_text, f"{code} OpenAI payload 泄露 Secret")
        _assert("REDIS_URL" not in openai_text, f"{code} OpenAI payload 泄露内部配置名")
        _assert("<html>" not in openai_text, f"{code} OpenAI payload 泄露上游原始 HTML")


def _assert_public_errors_have_actionable_metadata() -> None:
    for code in PUBLIC_CODES_FROM_REQUIREMENT:
        canonical_code = ErrorCatalogService.ALIAS_CODES.get(code, code)
        spec = ErrorCatalogService.SPECS[canonical_code]
        _assert(spec.public is True, f"{code} 应为对外错误")
        error_object = ErrorCatalogService.build_error_object(
            status_code=spec.status_code,
            detail={"code": code, "message": spec.message},
            trace_id="stage32-trace",
        )
        for field in ("message", "type", "code", "trace_id", "retryable", "recoverable", "category", "next_action"):
            _assert(field in error_object, f"{code} 对外错误缺少 {field}")
        log_context = ErrorCatalogService.build_log_context(
            status_code=spec.status_code,
            detail={"code": code, "message": spec.message},
            trace_id="stage32-trace",
        )
        for field in ("code", "message", "public_code", "public_message", "handling_strategy", "alert_level", "trace_id"):
            _assert(field in log_context and log_context[field] not in (None, ""), f"{code} 日志上下文缺少 {field}")


def _assert_log_enrichment_uses_catalog() -> None:
    log_context = ErrorCatalogService.build_log_context(
        status_code=503,
        detail={"code": "provider_capacity_unavailable", "message": "capacity snapshot failed"},
        trace_id="stage32-trace",
    )
    body_json = LogService._merge_error_context_into_response_body(
        response_body_json=None,
        error_context=log_context,
    )
    parsed_body = safeJsonParse(body_json)
    _assert(isinstance(parsed_body, dict), "日志响应摘要必须保持 JSON 对象")
    context = parsed_body.get("error_context")
    _assert(isinstance(context, dict), "日志响应摘要缺少 error_context")
    for field in ("code", "message", "public_code", "public_message", "handling_strategy", "alert_level", "trace_id"):
        _assert(context.get(field) not in (None, ""), f"日志 error_context 缺少 {field}")
    trace = LogService._merge_error_context_into_trace(trace=[{"result": "route_start"}], error_context=log_context)
    _assert(isinstance(trace, list), "trace 必须保持列表结构")
    catalog_events = [item for item in trace if item.get("result") == "error_catalog_context"]
    _assert(catalog_events, "trace 缺少 error_catalog_context")
    trace_context = catalog_events[-1].get("error_context")
    _assert(isinstance(trace_context, dict), "trace error_context 必须是对象")
    for field in ("code", "public_code", "handling_strategy", "alert_level", "trace_id"):
        _assert(trace_context.get(field) not in (None, ""), f"trace error_context 缺少 {field}")


def main() -> None:
    _assert_catalog_is_complete()
    _assert_public_errors_have_actionable_metadata()
    _assert_internal_errors_are_safely_mapped()
    _assert_log_enrichment_uses_catalog()
    print("stage32 error catalog completeness check passed")


if __name__ == "__main__":
    main()
