from __future__ import annotations

from contextvars import ContextVar
from typing import Any


_CURRENT_PROVIDER_CANDIDATE: ContextVar[dict[str, Any] | None] = ContextVar(
    "proxy_current_provider_candidate",
    default=None,
)
_CURRENT_REQUEST_HEADERS_JSON: ContextVar[str | None] = ContextVar(
    "proxy_current_request_headers_json",
    default=None,
)
_CURRENT_IP_MANAGEMENT_EVENT_ID: ContextVar[int | None] = ContextVar(
    "proxy_current_ip_management_event_id",
    default=None,
)


def clear_current_provider_candidate() -> None:
    _CURRENT_PROVIDER_CANDIDATE.set(None)


def clear_current_request_headers_json() -> None:
    _CURRENT_REQUEST_HEADERS_JSON.set(None)


def clear_current_ip_management_event_id() -> None:
    _CURRENT_IP_MANAGEMENT_EVENT_ID.set(None)


def get_current_provider_candidate() -> dict[str, Any] | None:
    candidate = _CURRENT_PROVIDER_CANDIDATE.get()
    if not isinstance(candidate, dict):
        return None
    return dict(candidate)


def get_current_request_headers_json() -> str | None:
    value = _CURRENT_REQUEST_HEADERS_JSON.get()
    return value if isinstance(value, str) and value else None


def get_current_ip_management_event_id() -> int | None:
    value = _CURRENT_IP_MANAGEMENT_EVENT_ID.get()
    return int(value) if isinstance(value, int) and value > 0 else None


def set_current_provider_candidate(*, provider: Any, provider_model: Any) -> None:
    _CURRENT_PROVIDER_CANDIDATE.set(
        {
            "provider_id": getattr(provider, "id", None),
            "provider_name": getattr(provider, "name", None),
            "resolved_provider_model_id": getattr(provider_model, "id", None),
            "model_name": getattr(provider_model, "model_name", None),
        }
    )


def set_current_request_headers_json(value: str | None) -> None:
    _CURRENT_REQUEST_HEADERS_JSON.set(value if isinstance(value, str) and value else None)


def set_current_ip_management_event_id(value: int | None) -> None:
    _CURRENT_IP_MANAGEMENT_EVENT_ID.set(int(value) if isinstance(value, int) and value > 0 else None)
