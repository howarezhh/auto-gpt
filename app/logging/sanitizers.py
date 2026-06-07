from __future__ import annotations

import hashlib
from typing import Any

from app.utils.json_utils import dumps_json


SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "bearer",
    "password",
    "secret",
    "token",
    "raw_key",
    "raw_api_key",
}


def sanitize_value(value: Any, *, max_string_length: int = 1000) -> Any:
    if isinstance(value, dict):
        return {
            str(key): ("***" if _is_sensitive_key(str(key)) else sanitize_value(item, max_string_length=max_string_length))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_value(item, max_string_length=max_string_length) for item in value[:100]]
    if isinstance(value, str):
        if len(value) > max_string_length:
            return f"{value[:max_string_length]}...[truncated:{len(value)}]"
        return value
    return value


def dumps_sanitized(value: Any, *, max_string_length: int = 1000) -> str | None:
    if value is None:
        return None
    return dumps_json(sanitize_value(value, max_string_length=max_string_length))


def stack_hash(text: str | None) -> str | None:
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]


def sha256_prefix(value: str | None, *, length: int = 16) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:length]


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(item in lowered for item in SENSITIVE_KEYS)
