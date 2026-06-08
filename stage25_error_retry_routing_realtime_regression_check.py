from __future__ import annotations

import asyncio
from unittest.mock import patch

from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.services.model_mapping_service import ModelMappingService
from app.services.openai_error_service import OpenAIErrorService
from app.services.proxy_service import ProxyService
from app.services.request_log_queue_service import RequestLogQueueService
from app.services.router_service import RecentSessionRoute, RouteCandidate, RoutePolicyContext, RouterService


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _provider(provider_id: int, *, priority: int = 10, health_status: str = "healthy") -> Provider:
    return Provider(
        id=provider_id,
        name=f"stage25-provider-{provider_id}",
        provider_type="openai_compatible",
        base_url="https://example.com/v1",
        api_key="upstream-secret",
        enabled=True,
        priority=priority,
        health_status=health_status,
        circuit_state="closed",
    )


def _provider_model(
    model_id: int,
    provider_id: int,
    *,
    priority: int = 10,
    health_status: str = "healthy",
    route_score: float = 100.0,
) -> ProviderModel:
    return ProviderModel(
        id=model_id,
        provider_id=provider_id,
        model_name="stage25-model",
        enabled=True,
        priority=priority,
        weight=100,
        health_status=health_status,
        circuit_state="closed",
        supports_stream=True,
        supports_vision=True,
        supports_tools=True,
        supports_chat_completions=True,
        supports_responses=True,
        last_latency_ms=max(1, int(1000 - route_score)),
    )


def _candidate(
    provider_id: int,
    model_id: int,
    *,
    health_tier: int,
    route_score: float,
) -> RouteCandidate:
    provider = _provider(provider_id, health_status="unhealthy" if health_tier >= 2 else "healthy")
    provider_model = _provider_model(
        model_id,
        provider_id,
        health_status="unhealthy" if health_tier >= 2 else "healthy",
        route_score=route_score,
    )
    return RouteCandidate(
        provider=provider,
        provider_model=provider_model,
        recent_failure_rate=0.0,
        recent_success_rate=1.0,
        route_score=route_score,
        dynamic_weight=100.0,
        health_tier=health_tier,
    )


async def _check_request_log_idle_wait_counts_ingress() -> None:
    calls = {"count": 0}

    async def fake_queue_lengths() -> dict[str, int]:
        calls["count"] += 1
        if calls["count"] == 1:
            return {"queued": 0, "processing": 0, "ingress": 1}
        return {"queued": 0, "processing": 0, "ingress": 0}

    with patch.object(RequestLogQueueService, "queue_lengths", fake_queue_lengths):
        result = await RequestLogQueueService.wait_until_idle(timeout_seconds=0.2, poll_interval_seconds=0.01)
    _assert(result["idle"] is True, f"wait_until_idle should wait for ingress queue: {result}")
    _assert(calls["count"] >= 2, "wait_until_idle returned before ingress drained")


def main() -> None:
    payload = OpenAIErrorService.build_error_payload(
        message="upstream timeout",
        code="upstream_read_timeout",
        trace_id="trace-stage25",
        error_type="timeout_error",
        retryable=True,
        recoverable=True,
        category="timeout",
        status_code=504,
    )
    error = payload["error"]
    _assert(error["trace_id"] == "trace-stage25", "error payload should include trace_id")
    _assert(error["retryable"] is True and error["recoverable"] is True, "error payload should expose retry flags")
    _assert(error["category"] == "timeout" and error["status_code"] == 504, "error payload should expose category/status")

    invalid = OpenAIErrorService.classify_error(
        status_code=400,
        detail={"code": "endpoint_response_conversion_unsafe", "message": "cannot safely convert"},
    )
    _assert(invalid["recoverable"] is False, "unsafe endpoint conversion must not be recoverable")
    transient = OpenAIErrorService.classify_error(
        status_code=503,
        detail={"code": "upstream_service_unavailable", "message": "upstream overloaded"},
    )
    _assert(transient["recoverable"] is True, "upstream transient failure should be recoverable")

    _assert(ProxyService._should_retry_same_provider_status(503, {"code": "server_error"}), "503 should allow same provider retry")
    _assert(not ProxyService._should_retry_same_provider_status(429, {"code": "rate_limit_exceeded"}), "429 should not immediate retry same provider")
    _assert(
        not ProxyService._should_retry_route_upstream_error(
            {"status_code": 400, "detail": {"code": "request_validation_failed"}}
        ),
        "request validation error should not wait route retry window",
    )
    _assert(
        ProxyService._should_retry_route_upstream_error(
            {"status_code": 504, "detail": {"code": "upstream_connect_timeout"}}
        ),
        "upstream timeout should wait route retry window",
    )
    _assert(
        not ProxyService._should_retry_mapped_model(
            {"status_code": 400, "detail": {"code": "request_validation_failed"}}
        ),
        "invalid request should not fail over mapped models",
    )
    _assert(
        ProxyService._should_retry_mapped_model(
            {"status_code": 404, "detail": {"code": "model_not_found"}}
        ),
        "model-specific failure may fail over mapped targets",
    )
    global_retry_off = type("Setting", (), {"route_exhausted_retry_infinite_enabled": False})()
    global_retry_on = type("Setting", (), {"route_exhausted_retry_infinite_enabled": True})()
    api_key_context = RoutePolicyContext()
    _assert(
        not ProxyService._route_exhausted_retry_infinite_enabled(global_retry_off, api_key_context),
        "infinite retry must be disabled when the global infinite retry switch is off",
    )
    _assert(
        ProxyService._route_exhausted_retry_infinite_enabled(global_retry_on, api_key_context),
        "infinite retry should be controlled by the global switch only",
    )

    healthy_low_score = _candidate(1, 11, health_tier=0, route_score=10.0)
    unhealthy_high_score = _candidate(2, 22, health_tier=2, route_score=999.0)
    ordered = RouterService._order_filtered_candidates(
        None,
        [unhealthy_high_score, healthy_low_score],
        sticky_key=None,
        route_context=RoutePolicyContext(),
    )
    _assert(ordered[0].provider.id == 1, "healthy route candidate must outrank unhealthy high-score fallback")

    sticky_a = _candidate(3, 33, health_tier=0, route_score=10.0)
    sticky_b = _candidate(4, 44, health_tier=0, route_score=900.0)
    with patch.object(
        RouterService,
        "load_recent_session_route",
        return_value=RecentSessionRoute(provider_id=3, provider_model_id=33, model_name="stage25-model"),
    ):
        sticky_ordered = RouterService._order_filtered_candidates(
            None,
            [sticky_b, sticky_a],
            sticky_key="stage25-session",
            route_context=RoutePolicyContext(),
        )
    _assert(sticky_ordered[0].provider.id == 3, "same health tier should prefer recent session candidate")

    default_unhealthy = _candidate(5, 55, health_tier=2, route_score=999.0)
    fallback_healthy = _candidate(6, 66, health_tier=0, route_score=10.0)
    failover_ordered = RouterService._order_filtered_candidates(
        None,
        [default_unhealthy, fallback_healthy],
        sticky_key=None,
        route_context=RoutePolicyContext(),
    )
    _assert(failover_ordered[0].provider.id == 6, "unhealthy provider must not leapfrog healthy fallback")

    mapped_targets = [
        {"model_name": "unhealthy-target", "available": True, "health_tier": 2, "score": 999, "priority": 1, "weight": 100, "order": 0},
        {"model_name": "healthy-target", "available": True, "health_tier": 0, "score": 10, "priority": 99, "weight": 100, "order": 1},
    ]
    mapped_ordered = ModelMappingService._order_targets(mapped_targets, strategy="auto", sticky_key="stage25-session")
    _assert(mapped_ordered[0]["model_name"] == "unhealthy-target", "model mapping must preserve target config order; provider health is handled later")

    asyncio.run(_check_request_log_idle_wait_counts_ingress())
    print("stage25 error retry routing realtime regression check passed")


if __name__ == "__main__":
    main()
