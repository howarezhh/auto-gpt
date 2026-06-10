from __future__ import annotations

from app.models.provider import Provider
from app.services.proxy_service import ProxyService


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    provider = Provider(
        id=1,
        name="失败切换测试提供商",
        provider_type="openai_compatible",
        base_url="https://example.com/v1",
        api_key="upstream-secret",
        enabled=True,
    )

    for status_code in (405, 422, 501):
        _assert(
            not ProxyService._should_try_endpoint_fallback(
                provider,
                endpoint_path="/responses",
                status_code=status_code,
                error_detail={"message": "unsupported responses endpoint for this model"},
            ),
            f"endpoint fallback should not auto-convert status {status_code}",
        )

    for status_code in (401, 403, 404, 429):
        _assert(
            not ProxyService._should_retry_same_provider_status(status_code),
            f"status {status_code} should not retry on same provider",
        )

    for status_code in (500, 502, 503, 504):
        _assert(
            ProxyService._should_retry_same_provider_status(status_code),
            f"status {status_code} should allow same-provider retry within budget",
        )

    provider.max_retries = 5
    setting = type("Setting", (), {"global_max_retries": 5})()
    _assert(
        ProxyService._same_provider_retry_budget(provider, setting) == 2,
        "same-provider retry budget should be initial attempt plus one retry",
    )

    _assert(
        not ProxyService._should_try_endpoint_fallback(
            provider,
            endpoint_path="/images/generations",
            status_code=405,
            error_detail={"message": "unsupported endpoint"},
        ),
        "endpoint fallback should stay limited to chat/responses safe conversion",
    )

    print("stage15 failover latency regression check passed")


if __name__ == "__main__":
    main()
