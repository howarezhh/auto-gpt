from __future__ import annotations

from typing import Any


class OpenAIErrorService:
    @staticmethod
    def build_error_payload(
        *,
        message: str,
        code: str | None,
        trace_id: str | None,
        error_type: str = "invalid_request_error",
        param: str | None = None,
        retryable: bool | None = None,
        detail: Any | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error": {
                "message": message,
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
        if detail is not None:
            payload["error"]["detail"] = detail
        return payload

    @staticmethod
    def classify_status_code(status_code: int) -> tuple[str, str, bool]:
        if status_code in {400, 404, 422}:
            return "invalid_request_error", "invalid_request", False
        if status_code in {401, 403}:
            return "authentication_error", "authentication_error", False
        if status_code == 408:
            return "timeout_error", "request_timeout", True
        if status_code == 409:
            return "conflict_error", "conflict", True
        if status_code == 429:
            return "rate_limit_error", "rate_limit_exceeded", True
        if status_code >= 500:
            return "server_error", "server_error", True
        return "invalid_request_error", "invalid_request", False

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
