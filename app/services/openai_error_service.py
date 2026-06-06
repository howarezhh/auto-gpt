from __future__ import annotations

from typing import Any

from app.services.error_catalog_service import ErrorCatalogService


class OpenAIErrorService:
    RECOVERABLE_STATUS_CODES = {408, 409, 425, 429}
    NONRECOVERABLE_STATUS_CODES = {400, 401, 403, 404, 413, 422}
    LOGICAL_ERROR_TOKENS = (
        "invalid_request",
        "request_validation",
        "request_body_too_large",
        "max_tokens_exceeded",
        "context_length",
        "context_length_exceeded",
        "input_too_large",
        "output_token_limit",
        "endpoint_response_conversion_unsafe",
        "endpoint_request_conversion_unsafe",
        "unsupported_endpoint",
    )
    AUTH_ERROR_TOKENS = (
        "invalid_api_key",
        "authentication",
        "unauthorized",
        "permission_denied",
        "access_denied",
        "forbidden",
        "insufficient_permissions",
    )
    MODEL_ERROR_TOKENS = (
        "model_not_found",
        "unknown_model",
        "no_such_model",
        "resource_not_found",
    )
    CAPABILITY_ERROR_TOKENS = (
        "not_supported",
        "unsupported",
        "tools_not_supported",
        "vision_not_supported",
        "image_generation_not_supported",
        "chat_not_supported",
        "responses_not_supported",
        "chat_probe_unhealthy",
        "responses_probe_unhealthy",
        "capability",
    )
    TRANSIENT_ERROR_TOKENS = (
        "timeout",
        "rate_limit",
        "too_many_requests",
        "capacity",
        "quota",
        "overloaded",
        "temporarily_unavailable",
        "service_unavailable",
        "upstream_connect",
        "upstream_network",
        "upstream_request_failed",
        "all_providers_unavailable_after_retry",
        "all_providers_failed",
    )

    @staticmethod
    def build_error_payload(
        *,
        message: str,
        code: str | None,
        trace_id: str | None,
        error_type: str = "invalid_request_error",
        param: str | None = None,
        retryable: bool | None = None,
        recoverable: bool | None = None,
        category: str | None = None,
        status_code: int | None = None,
        retry_after_ms: int | None = None,
        next_action: str | None = None,
        detail: Any | None = None,
    ) -> dict[str, Any]:
        resolved = ErrorCatalogService.resolve(
            status_code=status_code,
            detail=detail,
            code=code,
            message=message,
        )
        public_resolved = ErrorCatalogService.public_spec_for(resolved)
        if resolved.public:
            effective_message = ErrorCatalogService.effective_message(public_resolved, raw_message=message)
            payload_detail = detail
        else:
            effective_message = resolved.public_message or public_resolved.message
            payload_detail = {"message": "详细内部原因已写入服务端日志，请使用 trace_id 排查。"} if detail is not None else None
            retryable = public_resolved.retryable
            recoverable = public_resolved.recoverable
            category = public_resolved.category
            next_action = public_resolved.next_action
            error_type = public_resolved.error_type
        if retryable is None:
            retryable = public_resolved.retryable
        if recoverable is None:
            recoverable = public_resolved.recoverable
        if category is None:
            category = public_resolved.category
        if next_action is None:
            next_action = public_resolved.next_action
        if code is None or not resolved.public:
            code = public_resolved.code
        if error_type == "invalid_request_error" and public_resolved.error_type != "invalid_request_error":
            error_type = public_resolved.error_type
        payload: dict[str, Any] = {
            "error": {
                "message": effective_message,
                "type": error_type,
                "code": code,
            }
        }
        if param is not None:
            payload["error"]["param"] = param
        if trace_id is not None:
            payload["error"]["trace_id"] = trace_id
        if retryable is not None:
            payload["error"]["retryable"] = retryable
        if recoverable is not None:
            payload["error"]["recoverable"] = recoverable
        if category is not None:
            payload["error"]["category"] = category
        if status_code is not None:
            payload["error"]["status_code"] = status_code
        if retry_after_ms is not None:
            payload["error"]["retry_after_ms"] = retry_after_ms
        if next_action is not None:
            payload["error"]["next_action"] = next_action
        if payload_detail is not None:
            payload["error"]["detail"] = payload_detail
        return payload

    @staticmethod
    def classify_status_code(status_code: int) -> tuple[str, str, bool]:
        classified = OpenAIErrorService.classify_error(status_code=status_code)
        return classified["error_type"], classified["code"], bool(classified["retryable"])

    @staticmethod
    def classify_error(*, status_code: int, detail: Any | None = None) -> dict[str, Any]:
        detail_code = OpenAIErrorService.extract_code(detail)
        catalog_spec = ErrorCatalogService.resolve(status_code=status_code, detail=detail, code=detail_code)
        if detail_code is not None and ErrorCatalogService.normalize_code(detail_code) in ErrorCatalogService.SPECS:
            return OpenAIErrorService._classification(
                catalog_spec.error_type,
                catalog_spec.code,
                catalog_spec.retryable,
                catalog_spec.recoverable,
                catalog_spec.category,
                catalog_spec.next_action,
            )
        normalized_code = (detail_code or "").strip().lower()
        normalized_message = OpenAIErrorService.extract_message(detail, fallback="").strip().lower()
        basis = f"{normalized_code} {normalized_message}"

        if status_code == 499:
            return OpenAIErrorService._classification(
                "client_error",
                detail_code or "client_cancelled",
                False,
                False,
                "client_cancelled",
                catalog_spec.next_action,
            )
        if status_code == 401:
            return OpenAIErrorService._classification(
                "authentication_error",
                detail_code or "authentication_error",
                False,
                False,
                "authentication",
                catalog_spec.next_action,
            )
        if status_code == 403:
            return OpenAIErrorService._classification(
                "authentication_error",
                detail_code or "authorization_error",
                False,
                False,
                "authorization",
                catalog_spec.next_action,
            )
        if any(token in basis for token in OpenAIErrorService.AUTH_ERROR_TOKENS):
            return OpenAIErrorService._classification(
                "authentication_error",
                detail_code or "authentication_error",
                False,
                False,
                "authentication",
                catalog_spec.next_action,
            )
        if any(token in basis for token in OpenAIErrorService.MODEL_ERROR_TOKENS):
            return OpenAIErrorService._classification(
                "invalid_request_error",
                detail_code or "model_not_found",
                False,
                False,
                "model_unavailable",
                catalog_spec.next_action,
            )
        if status_code == 413 or any(token in basis for token in OpenAIErrorService.LOGICAL_ERROR_TOKENS):
            return OpenAIErrorService._classification(
                "invalid_request_error",
                detail_code or ("request_body_too_large" if status_code == 413 else "invalid_request"),
                False,
                False,
                "invalid_request",
                catalog_spec.next_action,
            )
        if any(token in basis for token in OpenAIErrorService.CAPABILITY_ERROR_TOKENS):
            return OpenAIErrorService._classification(
                "invalid_request_error",
                detail_code or "capability_not_supported",
                False,
                False,
                "capability_not_supported",
                catalog_spec.next_action,
            )
        if status_code in {400, 422}:
            return OpenAIErrorService._classification(
                "invalid_request_error",
                detail_code or "invalid_request",
                False,
                False,
                "invalid_request",
                catalog_spec.next_action,
            )
        if status_code in {408, 504} or "timeout" in basis:
            return OpenAIErrorService._classification(
                "timeout_error",
                detail_code or "request_timeout",
                True,
                True,
                "timeout",
                catalog_spec.next_action,
            )
        if status_code == 429:
            return OpenAIErrorService._classification(
                "rate_limit_error",
                detail_code or "rate_limit_exceeded",
                True,
                True,
                "rate_limit",
                catalog_spec.next_action,
            )
        if status_code == 409:
            return OpenAIErrorService._classification(
                "conflict_error",
                detail_code or "conflict",
                True,
                True,
                "upstream_transient",
                catalog_spec.next_action,
            )
        if status_code == 425:
            return OpenAIErrorService._classification(
                "server_error",
                detail_code or "too_early",
                True,
                True,
                "upstream_transient",
                catalog_spec.next_action,
            )
        if 500 <= status_code < 600:
            category = "network" if any(token in basis for token in ("connect", "network", "pool")) else "server_error"
            return OpenAIErrorService._classification(
                "server_error",
                detail_code or "server_error",
                True,
                True,
                category,
                catalog_spec.next_action,
            )
        if any(token in basis for token in OpenAIErrorService.TRANSIENT_ERROR_TOKENS):
            return OpenAIErrorService._classification(
                "server_error",
                detail_code or "upstream_transient_error",
                True,
                True,
                "upstream_transient",
                catalog_spec.next_action,
            )
        return OpenAIErrorService._classification(
            "invalid_request_error",
            detail_code or "invalid_request",
            False,
            False,
            "invalid_request",
            catalog_spec.next_action,
        )

    @staticmethod
    def _classification(
        error_type: str,
        code: str,
        retryable: bool,
        recoverable: bool,
        category: str,
        next_action: str | None = None,
    ) -> dict[str, Any]:
        return {
            "error_type": error_type,
            "code": code,
            "retryable": retryable,
            "recoverable": recoverable,
            "category": category,
            "next_action": next_action,
        }

    @staticmethod
    def extract_code(detail: Any) -> str | None:
        if isinstance(detail, dict):
            if isinstance(detail.get("code"), str) and detail["code"].strip():
                return detail["code"].strip()
            if isinstance(detail.get("error"), dict) and isinstance(detail["error"].get("code"), str):
                return detail["error"]["code"].strip()
        return None

    @staticmethod
    def extract_message(detail: Any, *, fallback: str) -> str:
        if isinstance(detail, dict):
            if isinstance(detail.get("error"), dict) and detail["error"].get("message"):
                return str(detail["error"]["message"])
            if isinstance(detail.get("message"), str) and detail["message"].strip():
                return detail["message"].strip()
            if isinstance(detail.get("detail"), str) and detail["detail"].strip():
                return detail["detail"].strip()
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
        return fallback
