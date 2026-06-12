from __future__ import annotations

from typing import Any

from app.services.proxy_request_context import set_current_request_headers_json
from app.utils.json_utils import dumps_json


class RequestHeaderLogService:
    """提取可安全落库的外部请求头诊断摘要。"""

    VALUE_MAX_CHARS = 512
    JSON_MAX_BYTES = 4096
    HEADER_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("user_agent", ("user-agent",)),
        ("client_request_id", ("x-request-id", "x-client-request-id", "request-id", "x-correlation-id")),
        ("idempotency_key", ("idempotency-key",)),
        ("retry_count", ("x-stainless-retry-count", "x-retry-count", "openai-retry-count")),
        ("sdk_name", ("x-sdk-name", "x-client-sdk", "x-stainless-package-name")),
        ("sdk_version", ("x-sdk-version", "x-stainless-package-version")),
        ("sdk_language", ("x-stainless-lang",)),
        ("sdk_runtime", ("x-stainless-runtime",)),
        ("sdk_runtime_version", ("x-stainless-runtime-version",)),
        ("sdk_os", ("x-stainless-os",)),
        ("sdk_arch", ("x-stainless-arch",)),
        ("openai_client_user_agent", ("x-openai-client-user-agent",)),
    )

    @classmethod
    def capture_request(cls, request: Any) -> str | None:
        summary = cls.extract(getattr(request, "headers", None))
        serialized = cls.serialize(summary)
        try:
            request.state.v1_request_headers_json = serialized
        except Exception:
            pass
        set_current_request_headers_json(serialized)
        return serialized

    @classmethod
    def extract(cls, headers: Any) -> dict[str, str]:
        if headers is None:
            return {}
        result: dict[str, str] = {}
        for field, aliases in cls.HEADER_ALIASES:
            for name in aliases:
                value = cls._get_header(headers, name)
                if value:
                    result[field] = cls._truncate_value(value)
                    break
        return result

    @classmethod
    def serialize(cls, summary: dict[str, str]) -> str | None:
        if not summary:
            return None
        serialized = dumps_json(summary)
        encoded = serialized.encode("utf-8", errors="ignore")
        if len(encoded) <= cls.JSON_MAX_BYTES:
            return serialized
        clipped = encoded[: cls.JSON_MAX_BYTES].decode("utf-8", errors="ignore")
        return f"{clipped}...[truncated]"

    @classmethod
    def _get_header(cls, headers: Any, name: str) -> str | None:
        try:
            value = headers.get(name)
        except Exception:
            value = None
        if value is None:
            try:
                value = headers.get(name.lower())
            except Exception:
                value = None
        if not isinstance(value, str):
            return None
        value = value.strip()
        return value or None

    @classmethod
    def _truncate_value(cls, value: str) -> str:
        if len(value) <= cls.VALUE_MAX_CHARS:
            return value
        return f"{value[: cls.VALUE_MAX_CHARS]}...[truncated]"
