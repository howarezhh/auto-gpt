from __future__ import annotations

from urllib.parse import quote


def build_proxy_response_headers(
    *,
    provider_id: int,
    provider_name: str,
    latency_ms: int,
    trace_length: int,
    trace_id: str | None = None,
) -> dict[str, str]:
    """构造代理响应头，向调用方暴露路由和追踪信息。"""
    headers = {
        "X-Proxy-Provider-Id": str(provider_id),
        # Provider 名称可能含非 ASCII 字符，统一转为百分号编码。
        "X-Proxy-Provider-Name": quote(provider_name, safe=""),
        "X-Proxy-Provider-Name-Encoding": "utf-8-percent-encoded",
        "X-Proxy-Latency-Ms": str(latency_ms),
        "X-Proxy-Trace-Length": str(trace_length),
        "X-Request-Id": trace_id or "",
    }
    if trace_id:
        headers["X-Trace-Id"] = trace_id
    return headers
