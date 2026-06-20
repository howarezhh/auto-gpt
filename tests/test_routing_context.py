from __future__ import annotations

from app.services.routing import (
    RouteCapabilitySet,
    RouteCandidateAction,
    RoutePipelineStrategy,
    RoutePolicyContext,
    RouteRequest,
    RouteRequestKind,
    RouteStage,
    RouteStageResult,
    RoutingService,
)
from app.services.routing.decision import RouteCandidateTraceEvent, RouteStageTrace


def test_route_request_fingerprint_changes_with_policy_context() -> None:
    base_request = RouteRequest(
        requested_model="测试模型",
        selected_model="测试模型",
        endpoint_path="/chat/completions",
        public_endpoint_path="/v1/chat/completions",
        request_kind=RouteRequestKind.CHAT_COMPLETIONS,
        capabilities=RouteCapabilitySet(require_chat_completions=True),
        policy_context=RoutePolicyContext(allowed_provider_ids=[1], health_gate_mode="permissive"),
    )
    changed_request = RouteRequest(
        requested_model="测试模型",
        selected_model="测试模型",
        endpoint_path="/chat/completions",
        public_endpoint_path="/v1/chat/completions",
        request_kind=RouteRequestKind.CHAT_COMPLETIONS,
        capabilities=RouteCapabilitySet(require_chat_completions=True),
        policy_context=RoutePolicyContext(allowed_provider_ids=[2], health_gate_mode="permissive"),
    )

    assert base_request.cache_fingerprint() != changed_request.cache_fingerprint()


def test_route_capability_set_exports_kwargs() -> None:
    capabilities = RouteCapabilitySet(
        require_vision=True,
        require_stream=True,
        require_tools=True,
        require_image_generation=True,
        require_chat_completions=True,
        required_upstream_protocol_type="gemini",
    )

    assert capabilities.as_kwargs() == {
        "require_vision": True,
        "require_stream": True,
        "require_tools": True,
        "require_image_generation": True,
        "require_chat_completions": True,
        "require_responses": False,
        "required_upstream_protocol_type": "gemini",
    }


def test_default_route_strategy_registry_contains_supported_strategies() -> None:
    registered = RoutingService.registry().registered_strategies()

    assert registered == [
        RoutePipelineStrategy.AVAILABILITY_FIRST,
        RoutePipelineStrategy.CAPACITY_AVOIDANCE,
        RoutePipelineStrategy.LATENCY_FIRST,
    ]


def test_route_stage_trace_serializes_stage_events() -> None:
    trace = RouteStageTrace(
        stage=RouteStage.FILTERS,
        strategy_id=RoutePipelineStrategy.AVAILABILITY_FIRST.value,
        result=RouteStageResult.SUCCESS,
        input_count=2,
        output_count=1,
        events=[
            RouteCandidateTraceEvent(
                provider_id=1,
                provider_model_id=11,
                action=RouteCandidateAction.KEPT,
                reason_code="provider_enabled",
                reason_label="提供商已启用",
                details={"rank": 1},
            )
        ],
    )

    payload = trace.to_dict()

    assert payload["stage"] == "filters"
    assert payload["events"][0]["action"] == "kept"
    assert payload["events"][0]["details"] == {"rank": 1}
