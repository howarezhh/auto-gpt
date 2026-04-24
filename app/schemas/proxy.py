from typing import Any

from pydantic import BaseModel


class ProxyTraceItem(BaseModel):
    provider_id: int | None = None
    provider_name: str
    result: str
    status_code: int | None = None
    latency_ms: int | None = None
    error: str | None = None


class ProxyTestResponse(BaseModel):
    provider_name: str
    latency_ms: int
    trace: list[ProxyTraceItem]
    response: dict[str, Any]
