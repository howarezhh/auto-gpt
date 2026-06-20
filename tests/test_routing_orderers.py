from __future__ import annotations

from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.services.routing import AvailabilityFirstOrderer, BalancedScoreOrderer, RecentSessionRoute, RouteCandidate


def _provider(provider_id: int) -> Provider:
    return Provider(
        id=provider_id,
        name=f"测试提供商-{provider_id}",
        provider_type="openai_compatible",
        base_url="https://example.com/v1",
        api_key="secret",
        enabled=True,
        priority=10,
        weight=100,
        health_status="healthy",
        circuit_state="closed",
    )


def _provider_model(model_id: int, provider_id: int) -> ProviderModel:
    return ProviderModel(
        id=model_id,
        provider_id=provider_id,
        model_name="测试模型",
        enabled=True,
        priority=10,
        weight=100,
        health_status="healthy",
        circuit_state="closed",
        supports_stream=True,
        supports_chat_completions=True,
        supports_responses=True,
    )


def _candidate(provider_id: int, model_id: int, *, route_score: float, load_factor: float, health_tier: int = 0) -> RouteCandidate:
    return RouteCandidate(
        provider=_provider(provider_id),
        provider_model=_provider_model(model_id, provider_id),
        route_score=route_score,
        load_factor=load_factor,
        health_tier=health_tier,
        score_breakdown={"preferred_provider_bonus": 0},
    )


def test_balanced_score_orderer_prefers_lower_load_inside_score_bucket() -> None:
    high_load = _candidate(1, 11, route_score=100, load_factor=0.9)
    low_load = _candidate(2, 22, route_score=100, load_factor=0.1)
    lower_score = _candidate(3, 33, route_score=70, load_factor=0.0)

    current = BalancedScoreOrderer.order([high_load, lower_score, low_load]).candidates

    assert [(item.provider.id, item.provider_model.id) for item in current] == [
        (2, 22),
        (1, 11),
        (3, 33),
    ]


def test_availability_first_orderer_keeps_recent_session_first() -> None:
    healthy = _candidate(1, 11, route_score=10, load_factor=0.0, health_tier=0)
    unhealthy_recent = _candidate(2, 22, route_score=999, load_factor=0.0, health_tier=2)
    recent_route = RecentSessionRoute(provider_id=2, provider_model_id=22, model_name="测试模型")

    current = AvailabilityFirstOrderer.order(
        [unhealthy_recent, healthy],
        recent_route=recent_route,
        route_context=None,
    ).candidates

    assert [(item.provider.id, item.provider_model.id, item.selection_reason) for item in current] == [
        (2, 22, "recent_session"),
        (1, 11, "highest_score"),
    ]
