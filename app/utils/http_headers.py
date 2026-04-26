from __future__ import annotations

from urllib.parse import quote


def build_proxy_response_headers(
    *,
    provider_id: int,
    provider_name: str,
    latency_ms: int,
    trace_length: int,
) -> dict[str, str]:
    return {
        "X-Proxy-Provider-Id": str(provider_id),
        "X-Proxy-Provider-Name": quote(provider_name, safe=""),
        "X-Proxy-Provider-Name-Encoding": "utf-8-percent-encoded",
        "X-Proxy-Latency-Ms": str(latency_ms),
        "X-Proxy-Trace-Length": str(trace_length),
    }
