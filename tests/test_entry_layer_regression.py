from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services.proxy_request_context import clear_current_provider_candidate, set_current_provider_candidate
from app.services.request_header_log_service import RequestHeaderLogService
from app.services.api_key_auth_cache import ApiKeyAuthCache
from app.services.api_key_service import ApiClientAuthError, ApiKeyService
from app.utils.request_stream import RequestBodyReadTimeout, RequestBodyTooLarge, read_limited_request_body


def test_limited_request_body_rejects_oversized_stream() -> None:
    async def run_case() -> None:
        async def stream():
            yield b"abc"
            yield b"def"

        with pytest.raises(RequestBodyTooLarge) as exc_info:
            await read_limited_request_body(
                stream(),
                max_bytes=5,
                total_timeout_seconds=5,
                idle_timeout_seconds=5,
            )
        assert exc_info.value.max_bytes == 5

    asyncio.run(run_case())


def test_limited_request_body_rejects_slow_client() -> None:
    async def run_case() -> None:
        async def stream():
            await asyncio.sleep(0.05)
            yield b"abc"

        with pytest.raises(RequestBodyReadTimeout) as exc_info:
            await read_limited_request_body(
                stream(),
                max_bytes=1024,
                total_timeout_seconds=5,
                idle_timeout_seconds=0.01,
            )
        assert exc_info.value.timeout_kind == "idle"

    asyncio.run(run_case())


def test_api_key_source_ip_uses_direct_ip_without_trusted_resolution() -> None:
    request = SimpleNamespace(
        state=SimpleNamespace(),
        client=SimpleNamespace(host="10.0.0.5"),
        headers={"x-forwarded-for": "203.0.113.9"},
    )

    assert ApiKeyService.extract_source_ip(request) == "10.0.0.5"


def test_api_key_source_ip_uses_trusted_ip_management_resolution() -> None:
    resolution = SimpleNamespace(resolved_client_ip="203.0.113.9")
    request = SimpleNamespace(
        state=SimpleNamespace(ip_management=SimpleNamespace(resolution=resolution)),
        client=SimpleNamespace(host="10.0.0.5"),
        headers={"x-forwarded-for": "203.0.113.9"},
    )

    assert ApiKeyService.extract_source_ip(request) == "203.0.113.9"


def test_external_native_protocol_paths_are_treated_as_external_api() -> None:
    import app.main as main
    from app.services.ip_management_service import IpManagementService

    assert main._is_external_v1_path("/v1beta/models/gemini-2.5-pro:generateContent") is True
    assert main._is_external_v1_path("/v1/messages") is True
    assert IpManagementService.resolve_scope("/v1beta/models/gemini-2.5-pro:generateContent") == "external_v1"
    assert IpManagementService.resolve_scope("/v1/messages") == "external_v1"


def test_api_key_endpoint_allowlist_accepts_native_protocol_paths() -> None:
    gemini_key = SimpleNamespace(allowed_endpoint_paths_json='["/v1beta/models"]')
    claude_key = SimpleNamespace(allowed_endpoint_paths_json='["/v1/messages"]')
    openai_key = SimpleNamespace(allowed_endpoint_paths_json='["/v1/chat/completions"]')

    assert ApiKeyService.is_endpoint_allowed(gemini_key, "/v1beta/models/gemini-2.5-pro:generateContent") is True
    assert ApiKeyService.is_endpoint_allowed(gemini_key, "/v1beta/models/gemini-2.5-pro:streamGenerateContent") is True
    assert ApiKeyService.is_endpoint_allowed(claude_key, "/v1/messages") is True
    assert ApiKeyService.is_endpoint_allowed(openai_key, "/v1/messages") is True
    assert ApiKeyService.is_endpoint_allowed(gemini_key, "/v1/responses") is False


def test_invalid_api_key_negative_cache_skips_database_lookup(monkeypatch) -> None:
    raw_key = "sk-aotu-abcdefghijklmnopqrstuvwxyz"
    key_hash = ApiKeyService.hash_api_key(raw_key)
    settings = SimpleNamespace(
        redis_url="",
        api_key_auth_negative_cache_ttl_seconds=30,
        api_key_auth_l1_cache_ttl_seconds=1,
        api_key_auth_l1_max_entries=100,
    )
    ApiKeyAuthCache.close()
    monkeypatch.setattr("app.services.api_key_auth_cache.get_settings", lambda: settings)
    ApiKeyAuthCache.set_invalid_hash(key_hash)

    class FakeDb:
        def scalar(self, *_args, **_kwargs):
            raise AssertionError("invalid API Key negative cache should avoid DB lookup")

    with pytest.raises(ApiClientAuthError) as exc_info:
        ApiKeyService.authenticate_request(FakeDb(), f"Bearer {raw_key}")

    assert exc_info.value.code == "invalid_api_key"
    ApiKeyAuthCache.close()


def test_limited_v1_json_payload_stores_requested_model(monkeypatch) -> None:
    from app.routers import proxy as proxy_router

    monkeypatch.setattr(
        proxy_router,
        "_get_setting_with_scoped_session",
        lambda: SimpleNamespace(
            max_v1_request_body_bytes=1024,
            max_v1_chat_request_body_bytes=1024,
            max_v1_responses_request_body_bytes=1024,
            max_logged_body_bytes=4096,
            request_timeout_ms=60000,
            v1_request_body_idle_timeout_seconds=15,
        ),
    )

    async def run_case() -> None:
        async def stream():
            yield b'{"model":"gpt-test","messages":[]}'

        request = SimpleNamespace(headers={}, stream=stream, state=SimpleNamespace())
        payload = await proxy_router._read_limited_v1_json_payload(request, endpoint_path="/chat/completions")

        assert payload["model"] == "gpt-test"
        assert request.state.v1_requested_model == "gpt-test"
        assert "gpt-test" in request.state.v1_request_body_structure_json

    asyncio.run(run_case())


def test_v1_fallback_log_includes_requested_model_and_last_candidate(monkeypatch) -> None:
    import app.main as main

    captured: dict = {}

    class FakeDb:
        def close(self) -> None:
            pass

    request = SimpleNamespace(
        state=SimpleNamespace(
            trace_id="trace-1",
            v1_requested_model="user-model",
            v1_request_headers_json='{"user_agent":"pytest-client","client_request_id":"req-1"}',
        ),
        url=SimpleNamespace(path="/v1/responses"),
        method="POST",
        client=SimpleNamespace(host="127.0.0.1"),
        headers={},
    )
    provider = SimpleNamespace(id=12, name="测试提供商")
    provider_model = SimpleNamespace(id=34, model_name="mounted-model")

    clear_current_provider_candidate()
    set_current_provider_candidate(provider=provider, provider_model=provider_model)
    monkeypatch.setattr(main.RequestLogQueueService, "enqueue", lambda **_kwargs: False)
    monkeypatch.setattr(main, "SessionLocal", lambda: FakeDb())
    monkeypatch.setattr(main.LogService, "create_log", lambda _db, **kwargs: captured.update(kwargs))

    main._log_v1_request_rejected_before_route(
        request=request,
        status_code=500,
        message="boom",
        error_code="internal_server_error",
        retryable=True,
        detail={"message": "boom", "code": "internal_server_error"},
        request_body_json='{"structure":{"model":"gpt-test"}}',
    )

    assert captured["requested_model"] == "user-model"
    assert captured["model_name"] == "mounted-model"
    assert captured["provider_id"] == 12
    assert captured["provider_name"] == "测试提供商"
    assert captured["resolved_provider_model_id"] == 34
    assert captured["request_headers_json"] == '{"user_agent":"pytest-client","client_request_id":"req-1"}'
    assert captured["trace"][0]["requested_model"] == "user-model"
    assert captured["trace"][0]["model_name"] == "mounted-model"


def test_request_header_log_service_only_keeps_safe_diagnostics() -> None:
    headers = {
        "user-agent": "pytest-sdk/1.0",
        "x-request-id": "req-123",
        "idempotency-key": "idem-abc",
        "x-stainless-retry-count": "2",
        "x-sdk-name": "openai-python",
        "x-sdk-version": "1.2.3",
        "authorization": "Bearer secret",
        "cookie": "session=secret",
    }

    summary = RequestHeaderLogService.extract(headers)

    assert summary["user_agent"] == "pytest-sdk/1.0"
    assert summary["client_request_id"] == "req-123"
    assert summary["idempotency_key"] == "idem-abc"
    assert summary["retry_count"] == "2"
    assert summary["sdk_name"] == "openai-python"
    assert summary["sdk_version"] == "1.2.3"
    assert "authorization" not in summary
    assert "cookie" not in summary
