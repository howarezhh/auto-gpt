from __future__ import annotations

from unittest.mock import patch

from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.services.model_mapping_service import ModelMappingResolution
from app.services.model_mapping_service import ModelMappingService
from app.services.provider_capacity_service import ProviderCapacitySnapshot
from app.services.proxy_service import ProxyService
from app.services.router_service import RecentSessionRoute, RouteCandidate, RouterService


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _provider(provider_id: int, *, max_active_requests: int | None = 100) -> Provider:
    return Provider(
        id=provider_id,
        name=f"stage28-provider-{provider_id}",
        provider_type="openai_compatible",
        base_url="https://example.com/v1",
        api_key="upstream-secret",
        enabled=True,
        priority=10,
        weight=100,
        health_status="healthy",
        circuit_state="closed",
        max_active_requests=max_active_requests,
        max_active_streams=100,
        max_qps=100,
        max_rpm=100,
    )


def _provider_model(model_id: int, provider_id: int, *, model_name: str = "stage28-model") -> ProviderModel:
    return ProviderModel(
        id=model_id,
        provider_id=provider_id,
        model_name=model_name,
        enabled=True,
        priority=10,
        weight=100,
        health_status="healthy",
        circuit_state="closed",
        supports_stream=True,
        supports_vision=True,
        supports_tools=True,
        supports_chat_completions=True,
        supports_responses=True,
    )


def _candidate(
    provider_id: int,
    model_id: int,
    *,
    health_tier: int = 0,
    route_score: float = 100.0,
    load_factor: float = 0.0,
    model_name: str = "stage28-model",
) -> RouteCandidate:
    return RouteCandidate(
        provider=_provider(provider_id),
        provider_model=_provider_model(model_id, provider_id, model_name=model_name),
        recent_failure_rate=0.0,
        recent_success_rate=1.0,
        recent_avg_latency_ms=50,
        dynamic_weight=100.0,
        route_score=route_score,
        health_tier=health_tier,
        load_factor=load_factor,
    )


def _check_recent_session_actual_target_wins_when_available() -> None:
    healthy = _candidate(1, 101, health_tier=0, route_score=10)
    unhealthy_recent = _candidate(2, 202, health_tier=2, route_score=999)
    ordered = RouterService._primary_route_order(
        [unhealthy_recent, healthy],
        sticky_key="会话-1",
        recent_route=RecentSessionRoute(provider_id=2, provider_model_id=202, model_name="stage28-model"),
    )
    _assert(
        ordered[0] is unhealthy_recent,
        "recent successful session target should be tried first when it remains in the available candidates",
    )


def _check_health_order_when_recent_session_target_unavailable() -> None:
    healthy = _candidate(1, 101, health_tier=0, route_score=1)
    unhealthy = _candidate(2, 202, health_tier=2, route_score=999)
    ordered = RouterService._primary_route_order(
        [unhealthy, healthy],
        sticky_key="会话-2",
        recent_route=RecentSessionRoute(provider_id=9, provider_model_id=909, model_name="stage28-model"),
    )
    _assert(
        ordered[0] is healthy,
        "when the recent session target is unavailable, routing should fall back to normal health-priority ordering",
    )


def _check_load_affects_distribution_weight() -> None:
    low_load = _candidate(1, 101, load_factor=0.1)
    high_load = _candidate(2, 202, load_factor=0.9)
    _assert(
        RouterService._route_selection_weight(low_load) > RouterService._route_selection_weight(high_load),
        "lower loaded provider should receive a higher distribution weight",
    )


def _check_capacity_is_hard_filter() -> None:
    full = _candidate(1, 101)
    available = _candidate(2, 202)
    full.provider.max_active_requests = 1
    available.provider.max_active_requests = 10
    snapshots = {
        1: ProviderCapacitySnapshot(active_requests=1, active_streams=0, current_qps=0, current_rpm=0),
        2: ProviderCapacitySnapshot(active_requests=0, active_streams=0, current_qps=0, current_rpm=0),
    }
    with patch("app.services.router_service.ProviderCapacityService.snapshots", return_value=snapshots):
        filtered = RouterService._filter_capacity_candidates([full, available], is_stream=False)
    _assert(filtered == [available], "provider at max active concurrency must be excluded")


def _check_provider_rpm_is_hard_filter() -> None:
    full = _candidate(1, 101)
    available = _candidate(2, 202)
    full.provider.max_rpm = 20
    available.provider.max_rpm = 20
    snapshots = {
        1: ProviderCapacitySnapshot(active_requests=0, active_streams=0, current_qps=0, current_rpm=20),
        2: ProviderCapacitySnapshot(active_requests=0, active_streams=0, current_qps=0, current_rpm=19),
    }
    with patch("app.services.router_service.ProviderCapacityService.snapshots", return_value=snapshots):
        filtered = RouterService._filter_capacity_candidates([full, available], is_stream=False)
    _assert(filtered == [available], "provider at max RPM must be excluded until the minute window refreshes")


def _check_mapping_recent_model_policy() -> None:
    healthy_recent = {
        "model_name": "健康近期模型",
        "available": True,
        "health_tier": 0,
        "score": 1,
        "weight": 100,
        "priority": 10,
        "order": 0,
    }
    healthy_other = {
        "model_name": "健康高分模型",
        "available": True,
        "health_tier": 0,
        "score": 999,
        "weight": 100,
        "priority": 10,
        "order": 1,
    }
    ordered = ModelMappingService._order_targets(
        [healthy_other, healthy_recent],
        strategy="weighted",
        sticky_key="会话-3",
        recent_model_name="健康近期模型",
    )
    _assert(ordered[0]["model_name"] == "健康近期模型", "mapping should prefer recent model when it remains available")

    unhealthy_recent = {
        **healthy_recent,
        "model_name": "异常近期模型",
        "health_tier": 2,
    }
    ordered = ModelMappingService._order_targets(
        [healthy_other, unhealthy_recent],
        strategy="priority",
        sticky_key="会话-4",
        recent_model_name="异常近期模型",
    )
    _assert(
        ordered[0]["model_name"] == "异常近期模型",
        "mapping should still prefer the recent actual model after hard filters, even across health tiers",
    )


def _check_session_sticky_key_precision() -> None:
    payload = {"user": "用户不是会话", "metadata": {"conversation_id": "会话-5"}}
    _assert(ProxyService._extract_session_sticky_key(payload) == "会话-5", "session sticky key should use explicit session metadata")
    client_metadata_payload = {"client_metadata": {"session_id": "会话-6"}, "prompt_cache_key": "缓存-1"}
    _assert(
        ProxyService._extract_session_sticky_key(client_metadata_payload) == "会话-6",
        "session sticky key should support Responses client_metadata",
    )
    _assert(
        ProxyService._extract_session_sticky_key({"prompt_cache_key": "缓存-2"}) == "缓存-2",
        "prompt_cache_key should be the fallback sticky key when explicit session metadata is absent",
    )
    _assert(ProxyService._extract_session_sticky_key({"user": "用户不是会话"}) is None, "new session should not use user as session stickiness")


def _check_stateful_responses_mapping_is_allowed_with_trace() -> None:
    mapping_resolution = ModelMappingResolution(
        source_model_name="not-gpt",
        selected_model_name="grok-4.3",
        strategy="priority",
        mapping_id=1,
        candidate_model_names=("grok-4.3",),
        trace={"result": "model_mapping_selected", "selection_reason": "同一会话上次成功目标模型仍可用，优先复用"},
    )
    payload = {
        "model": "not-gpt",
        "input": [{"type": "reasoning", "encrypted_content": "enc_state"}],
    }
    ProxyService._assert_stateful_responses_mapping_safe(
        endpoint_path="/responses",
        payload=payload,
        requested_model_name="not-gpt",
        mapping_resolution=mapping_resolution,
    )
    trace: list[dict] = []
    ProxyService._append_stateful_responses_route_trace(
        trace,
        endpoint_path="/responses",
        payload=payload,
        requested_model_name="not-gpt",
        selected_model_name="grok-4.3",
        mapping_resolution=mapping_resolution,
    )
    _assert(trace and trace[0]["result"] == "stateful_responses_routing", "stateful mapping should be observable in trace")
    _assert(
        trace[0]["policy"] == "prefer_recent_success_target_then_failover",
        f"unexpected stateful routing policy trace: {trace}",
    )


def _check_encrypted_content_blocks_endpoint_conversion() -> None:
    safety = ProxyService._assess_responses_to_chat_conversion_safety({
        "model": "gpt-5.4",
        "input": [{"type": "reasoning", "encrypted_content": "enc_state"}],
    })
    _assert(safety.safe is False, f"encrypted_content should not be convertible to chat: {safety}")
    _assert(
        any("encrypted_content" in item for item in (safety.unsafe_reasons or [])),
        f"encrypted_content reason should be surfaced: {safety}",
    )


def main() -> None:
    _check_recent_session_actual_target_wins_when_available()
    _check_health_order_when_recent_session_target_unavailable()
    _check_load_affects_distribution_weight()
    _check_capacity_is_hard_filter()
    _check_provider_rpm_is_hard_filter()
    _check_mapping_recent_model_policy()
    _check_session_sticky_key_precision()
    _check_stateful_responses_mapping_is_allowed_with_trace()
    _check_encrypted_content_blocks_endpoint_conversion()
    print("stage28 routing policy regression check passed")


if __name__ == "__main__":
    main()
