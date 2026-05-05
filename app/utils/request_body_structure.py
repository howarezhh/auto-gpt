from __future__ import annotations

from typing import Any


SENSITIVE_KEY_TOKENS = ("api_key", "authorization", "secret", "password", "token", "key")
CONTENT_KEYS = (
    "content",
    "text",
    "input_text",
    "output_text",
    "image",
    "image_url",
    "image_base64",
    "b64_json",
    "audio",
    "file",
)
SAFE_STRING_VALUE_KEYS = {
    "model",
    "role",
    "type",
    "object",
    "stream",
    "tool_choice",
    "reasoning_effort",
    "response_format",
}


def summarize_request_body_structure(
    value: Any,
    *,
    max_depth: int = 8,
    max_keys: int = 80,
    max_items: int = 20,
    max_safe_string_chars: int = 128,
) -> dict[str, Any]:
    return {
        "_summary": "request body structure only; prompt/content values are omitted",
        "structure": _summarize_value(
            value,
            key=None,
            depth=0,
            max_depth=max_depth,
            max_keys=max_keys,
            max_items=max_items,
            max_safe_string_chars=max_safe_string_chars,
        ),
    }


def _summarize_value(
    value: Any,
    *,
    key: str | None,
    depth: int,
    max_depth: int,
    max_keys: int,
    max_items: int,
    max_safe_string_chars: int,
) -> dict[str, Any]:
    if depth >= max_depth:
        return {"type": _type_name(value), "truncated": "max_depth"}

    if isinstance(value, dict):
        keys = [str(item) for item in value.keys()]
        selected_items = list(value.items())[:max_keys]
        return {
            "type": "object",
            "key_count": len(keys),
            "keys": keys[:max_keys],
            "truncated_keys": max(0, len(keys) - max_keys),
            "fields": {
                str(item_key): _summarize_value(
                    item_value,
                    key=str(item_key),
                    depth=depth + 1,
                    max_depth=max_depth,
                    max_keys=max_keys,
                    max_items=max_items,
                    max_safe_string_chars=max_safe_string_chars,
                )
                for item_key, item_value in selected_items
            },
        }

    if isinstance(value, list):
        selected_items = value[:max_items]
        return {
            "type": "array",
            "length": len(value),
            "items": [
                _summarize_value(
                    item,
                    key=key,
                    depth=depth + 1,
                    max_depth=max_depth,
                    max_keys=max_keys,
                    max_items=max_items,
                    max_safe_string_chars=max_safe_string_chars,
                )
                for item in selected_items
            ],
            "truncated_items": max(0, len(value) - max_items),
        }

    if isinstance(value, str):
        summary: dict[str, Any] = {"type": "string", "chars": len(value)}
        lowered_key = (key or "").lower()
        if _is_sensitive_key(lowered_key):
            summary["masked"] = True
        elif _is_content_key(lowered_key):
            summary["omitted"] = True
        elif key in SAFE_STRING_VALUE_KEYS and len(value) <= max_safe_string_chars:
            summary["value"] = value
        else:
            summary["omitted"] = True
        return summary

    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean", "value": value}
    if isinstance(value, int) and not isinstance(value, bool):
        return {"type": "integer", "value": value}
    if isinstance(value, float):
        return {"type": "number", "value": value}
    return {"type": _type_name(value), "repr_chars": len(str(value))}


def _type_name(value: Any) -> str:
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return type(value).__name__


def _is_sensitive_key(lowered_key: str) -> bool:
    return any(token in lowered_key for token in SENSITIVE_KEY_TOKENS)


def _is_content_key(lowered_key: str) -> bool:
    return lowered_key in CONTENT_KEYS or lowered_key.endswith("_content")
