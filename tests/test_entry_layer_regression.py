from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

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
