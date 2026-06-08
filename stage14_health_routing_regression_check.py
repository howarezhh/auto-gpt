from __future__ import annotations

from unittest.mock import patch

from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.services.health_service import HealthService
from app.services.provider_capacity_service import ProviderCapacitySnapshot
from app.services.router_service import RouterService


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _provider(**kwargs) -> Provider:
    return Provider(
        id=kwargs.get("id", 1),
        name=kwargs.get("name", "健康路由测试提供商"),
        provider_type=kwargs.get("provider_type", "openai_compatible"),
        protocol_type=kwargs.get("protocol_type", "both"),
        base_url="https://example.com/v1",
        api_key="upstream-secret",
        enabled=True,
        priority=kwargs.get("priority", 5),
        health_status=kwargs.get("health_status", "healthy"),
        circuit_state=kwargs.get("circuit_state", "closed"),
        max_active_requests=kwargs.get("max_active_requests", 20),
        max_active_streams=kwargs.get("max_active_streams", 10),
        max_qps=kwargs.get("max_qps", 20),
        max_rpm=kwargs.get("max_rpm", 20),
        trust_level=kwargs.get("trust_level", "standard"),
        content_integrity_status=kwargs.get("content_integrity_status", "unknown"),
        content_integrity_score=kwargs.get("content_integrity_score", 80),
    )


def _provider_model(**kwargs) -> ProviderModel:
    return ProviderModel(
        id=kwargs.get("id", 11),
        provider_id=kwargs.get("provider_id", 1),
        model_name=kwargs.get("model_name", "健康路由测试模型"),
        enabled=True,
        priority=kwargs.get("priority", 10),
        weight=100,
        health_status=kwargs.get("health_status", "healthy"),
        circuit_state=kwargs.get("circuit_state", "closed"),
        last_latency_ms=kwargs.get("last_latency_ms", 900),
        supports_tools=kwargs.get("supports_tools", True),
        supports_vision=kwargs.get("supports_vision", True),
        supports_stream=kwargs.get("supports_stream", True),
        supports_chat_completions=True,
        supports_responses=True,
        content_integrity_status=kwargs.get("content_integrity_status", "unknown"),
    )


def main() -> None:
    provider = _provider(max_active_requests=100)
    model = _provider_model()

    _assert(HealthService._determine_parallel_probe_limit(provider, 100) == 16, "parallel cap should respect max cap 16")
    provider.max_active_requests = 10
    _assert(HealthService._determine_parallel_probe_limit(provider, 100) == 2, "parallel cap should use 20% provider capacity")
    provider.max_active_requests = 0
    _assert(HealthService._determine_parallel_probe_limit(provider, 100) == 8, "missing capacity should use conservative cap 8")

    chat_payload = HealthService._build_chat_probe_payload(model, vision_probe=False, stream_probe=False, max_tokens=4)
    responses_payload = HealthService._build_responses_probe_payload(model, vision_probe=False, max_output_tokens=4)
    _assert(chat_payload["max_tokens"] == 4, "chat health probe should be tiny")
    _assert(responses_payload["max_output_tokens"] == 4, "responses health probe should be tiny")

    fast_score = RouterService._route_score(
        provider=provider,
        provider_model=model,
        recent_success_rate=0.8,
        recent_avg_latency_ms=1000,
        is_stream=True,
        model_health_state={
            "health_status": "healthy",
            "circuit_state": "closed",
            "ewma_latency_ms": 120,
            "ewma_ttfb_ms": 80,
            "success_rate_5m": 0.99,
            "failure_rate_5m": 0.01,
        },
        capacity_snapshot=ProviderCapacitySnapshot(active_requests=1, active_streams=1, current_qps=1),
    )
    slow_score = RouterService._route_score(
        provider=provider,
        provider_model=model,
        recent_success_rate=0.8,
        recent_avg_latency_ms=1000,
        is_stream=True,
        model_health_state={
            "health_status": "degraded",
            "circuit_state": "closed",
            "ewma_latency_ms": 2500,
            "ewma_ttfb_ms": 1800,
            "success_rate_5m": 0.6,
            "failure_rate_5m": 0.4,
        },
        capacity_snapshot=ProviderCapacitySnapshot(active_requests=9, active_streams=8, current_qps=18),
    )
    _assert(fast_score > slow_score, "Redis hot health should improve good candidate score")

    with patch("app.services.router_service.ProviderHealthStateService.get_model_capability_state", return_value=None), patch(
        "app.services.router_service.CacheService.get",
        return_value=None,
    ):
        _assert(not RouterService._capability_probe_failed(provider, model, "tools"), "missing Redis health should fall back without excluding")
        _assert(
            not hasattr(RouterService, "_endpoint_probe_failed"),
            "endpoint protocol support must rely on admin configuration instead of probe cache",
        )

    with patch(
        "app.services.router_service.ProviderHealthStateService.get_model_capability_state",
        return_value={"tools": {"native_ok": False, "success": False}},
    ):
        _assert(RouterService._capability_probe_failed(provider, model, "tools"), "failed native tools probe should remain observable")

    endpoint_capability_state = {
        "tools_chat_completions": {"native_ok": True, "success": True},
        "tools_responses": {"native_ok": False, "success": False},
        "vision_chat_completions": {"success": False},
        "vision_responses": {"success": True},
    }
    with patch(
        "app.services.router_service.ProviderHealthStateService.get_model_capability_state",
        return_value=endpoint_capability_state,
    ):
        _assert(
            not RouterService._capability_probe_failed(provider, model, "tools", endpoint_path="/chat/completions"),
            "Responses tools failure must not block native Chat tools route",
        )
        _assert(
            RouterService._capability_probe_failed(provider, model, "tools", endpoint_path="/responses"),
            "Responses tools failure should remain isolated to the native Responses probe key",
        )
        _assert(
            RouterService._capability_probe_failed(provider, model, "vision", endpoint_path="/chat/completions"),
            "Chat vision failure should block native Chat vision route",
        )
        _assert(
            not RouterService._capability_probe_failed(provider, model, "vision", endpoint_path="/responses"),
            "Chat vision failure must not block native Responses vision route",
        )

    provider.provider_models = [model]
    with patch("app.services.router_service.ProviderService.list_runtime_providers", return_value=[provider]), patch(
        "app.services.router_service.LogService.route_metric_summary",
        return_value={},
    ), patch(
        "app.services.router_service.ModelCatalogService.enabled_model_name_set",
        return_value={model.model_name},
    ), patch(
        "app.services.router_service.ProviderCapacityService.snapshots",
        return_value={provider.id: ProviderCapacitySnapshot(active_requests=0, active_streams=0, current_qps=0)},
    ), patch(
        "app.services.router_service.ProviderHealthStateService.get_model_state",
        return_value={"health_status": "healthy", "circuit_state": "closed"},
    ), patch(
        "app.services.router_service.ProviderHealthStateService.get_provider_state",
        return_value={"health_status": "healthy", "circuit_state": "closed"},
    ):
        candidates = RouterService._load_available_candidates_uncached(
            object(),
            cache_key="stage14-route-candidate-indent-regression",
            model_name=model.model_name,
            require_chat_completions=True,
            require_stream=True,
        )
    _assert(len(candidates) == 1, "enabled provider model must be appended as a route candidate")
    _assert(candidates[0].provider_model.id == model.id, "route candidate should preserve the matching provider model")

    print("stage14 health routing regression check passed")


if __name__ == "__main__":
    main()
