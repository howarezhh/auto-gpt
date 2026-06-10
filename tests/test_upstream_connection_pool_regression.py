from __future__ import annotations

from types import SimpleNamespace

import httpx

from app.services.proxy_service import ProxyService
from app.services.upstream_client import UpstreamClientService


def _settings(**overrides):
    values = {
        "request_timeout_ms": 60000,
        "upstream_pool_timeout_s": 0.01,
        "upstream_max_connections": 3,
        "upstream_max_keepalive_connections": 2,
        "upstream_keepalive_expiry_seconds": 45.0,
        "upstream_dns_cache_ttl_seconds": 120,
        "upstream_requests_pool_block": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_upstream_client_fingerprint_includes_keepalive_and_dns_settings(monkeypatch) -> None:
    monkeypatch.setattr("app.services.upstream_client.get_settings", lambda: _settings())
    baseline = UpstreamClientService._build_fingerprint(http2=False)

    monkeypatch.setattr(
        "app.services.upstream_client.get_settings",
        lambda: _settings(upstream_keepalive_expiry_seconds=10.0),
    )
    assert UpstreamClientService._build_fingerprint(http2=False) != baseline

    monkeypatch.setattr(
        "app.services.upstream_client.get_settings",
        lambda: _settings(upstream_dns_cache_ttl_seconds=0),
    )
    assert UpstreamClientService._build_fingerprint(http2=False) != baseline


def test_requests_fallback_uses_global_pool_timeout(monkeypatch) -> None:
    ProxyService._requests_connection_semaphore = None
    ProxyService._requests_connection_semaphore_fingerprint = None
    monkeypatch.setattr("app.services.proxy_service.get_settings", lambda: _settings(upstream_max_connections=1))

    with ProxyService._requests_connection_slot():
        try:
            with ProxyService._requests_connection_slot():
                raise AssertionError("second slot should not be acquired while pool is full")
        except httpx.PoolTimeout:
            pass

    with ProxyService._requests_connection_slot():
        pass
