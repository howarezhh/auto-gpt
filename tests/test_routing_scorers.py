from __future__ import annotations

from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.services.provider_capacity_service import ProviderCapacitySnapshot
from app.services.routing import AvailabilityFirstScorer, CapacityAvoidanceScorer, LatencyFirstScorer, RoutePolicyContext


def _provider() -> Provider:
    return Provider(
        id=1,
        name="测试提供商",
        provider_type="openai_compatible",
        base_url="https://example.com/v1",
        api_key="secret",
        enabled=True,
        priority=5,
        weight=100,
        health_status="healthy",
        circuit_state="closed",
        max_active_requests=10,
        max_active_streams=5,
        max_qps=20,
        max_rpm=100,
        region_tag="华东",
    )


def _provider_model() -> ProviderModel:
    return ProviderModel(
        id=11,
        provider_id=1,
        model_name="测试模型",
        enabled=True,
        priority=3,
        weight=100,
        health_status="healthy",
        circuit_state="closed",
        last_latency_ms=180,
        supports_stream=True,
        supports_vision=True,
        supports_tools=True,
        supports_chat_completions=True,
        supports_responses=True,
    )


def test_availability_first_scorer_returns_structured_breakdown() -> None:
    provider = _provider()
    provider_model = _provider_model()
    capacity = ProviderCapacitySnapshot(active_requests=3, active_streams=2, current_qps=4, current_rpm=30)
    route_context = RoutePolicyContext(
        preferred_provider_ids=[1],
        preferred_region_tags=["华东"],
        latency_bias=2,
        success_rate_bias=2,
    )

    current = AvailabilityFirstScorer.score_breakdown(
        provider=provider,
        provider_model=provider_model,
        recent_success_rate=0.75,
        recent_avg_latency_ms=250,
        is_stream=True,
        model_health_state={"health_status": "healthy", "success_rate_5m": 0.8, "failure_rate_5m": 0.2, "ewma_ttfb_ms": 120},
        provider_health_state={},
        capacity_snapshot=capacity,
        route_context=route_context,
    )

    assert current["health_score"] == 100.0
    assert current["preferred_provider_bonus"] == 35.0
    assert current["region_bonus"] == 20.0
    assert current["success_score"] == 64.0
    assert current["latency_penalty"] == 5.0
    assert current["ttfb_penalty"] == 1.2
    assert len(current["score_items"]) == 10


def test_latency_first_scorer_increases_latency_penalties() -> None:
    provider = _provider()
    provider_model = _provider_model()
    base = AvailabilityFirstScorer.score_breakdown(
        provider=provider,
        provider_model=provider_model,
        recent_success_rate=0.9,
        recent_avg_latency_ms=200,
        is_stream=True,
        model_health_state={"health_status": "healthy", "ewma_ttfb_ms": 100},
        provider_health_state={},
        capacity_snapshot=None,
        route_context=RoutePolicyContext(),
    )
    latency_first = LatencyFirstScorer.score_breakdown(
        provider=provider,
        provider_model=provider_model,
        recent_success_rate=0.9,
        recent_avg_latency_ms=200,
        is_stream=True,
        model_health_state={"health_status": "healthy", "ewma_ttfb_ms": 100},
        provider_health_state={},
        capacity_snapshot=None,
        route_context=RoutePolicyContext(),
    )

    assert latency_first["latency_penalty"] == base["latency_penalty"] * 2.5
    assert latency_first["ttfb_penalty"] == base["ttfb_penalty"] * 2.5
    assert latency_first["final_score"] < base["final_score"]


def test_capacity_avoidance_scorer_increases_saturation_penalty() -> None:
    provider = _provider()
    provider_model = _provider_model()
    capacity = ProviderCapacitySnapshot(active_requests=8, active_streams=4, current_qps=16, current_rpm=80)
    base = AvailabilityFirstScorer.score_breakdown(
        provider=provider,
        provider_model=provider_model,
        recent_success_rate=0.9,
        recent_avg_latency_ms=200,
        is_stream=False,
        capacity_snapshot=capacity,
        route_context=RoutePolicyContext(),
    )
    capacity_avoidance = CapacityAvoidanceScorer.score_breakdown(
        provider=provider,
        provider_model=provider_model,
        recent_success_rate=0.9,
        recent_avg_latency_ms=200,
        is_stream=False,
        capacity_snapshot=capacity,
        route_context=RoutePolicyContext(),
    )

    assert capacity_avoidance["saturation_penalty"] == base["saturation_penalty"] * 2.5
    assert capacity_avoidance["final_score"] < base["final_score"]
