from __future__ import annotations

import json

from app.services.routing import (
    RouteCapabilitySet,
    RouteDecision,
    RoutePipelineStrategy,
    RouteRequest,
    RouteRequestKind,
    RoutingService,
)
from app.services.routing.filters import RouteFilterChain


def test_default_registry_exposes_pluggable_pipeline_definition() -> None:
    definition = RoutingService.registry().get(RoutePipelineStrategy.AVAILABILITY_FIRST)
    latency_definition = RoutingService.registry().get(RoutePipelineStrategy.LATENCY_FIRST)
    capacity_definition = RoutingService.registry().get(RoutePipelineStrategy.CAPACITY_AVOIDANCE)

    assert isinstance(definition.filters[0], RouteFilterChain)
    assert definition.scorers[0].id == "availability_first_score"
    assert definition.orderers[0].id == "availability_first_orderer"
    assert definition.diagnosers[0].id == "stage_trace_diagnoser"
    assert latency_definition.scorers[0].id == "latency_first_score"
    assert capacity_definition.scorers[0].id == "capacity_avoidance_score"


def test_route_decision_trace_payload_keeps_stage_contract() -> None:
    request = RouteRequest(
        requested_model="源模型",
        selected_model="目标模型",
        endpoint_path="/chat/completions",
        public_endpoint_path="/v1/chat/completions",
        request_kind=RouteRequestKind.CHAT_COMPLETIONS,
        capabilities=RouteCapabilitySet(require_stream=True, require_chat_completions=True),
    )
    decision = RouteDecision(request=request)
    decision.stage_traces.extend(RoutingService._build_stage_traces(decision))

    payload = decision.to_trace_payload()

    assert payload["route_policy"] == "availability_first"
    assert payload["policy_version"] == "route-pipeline-v1"
    assert [item["stage"] for item in payload["stage_traces"]] == [
        "context",
        "filters",
        "scorer",
        "orderer",
        "diagnoser",
    ]


def test_route_decision_event_payload_keeps_log_field_compatibility() -> None:
    payload = RoutingService.route_decision_event_payload(
        route_round=2,
        candidate_count=1,
        selected_provider_id=3,
        selected_provider_model_id=33,
        failed_candidate_keys={(1, 11)},
        diagnostics={"final_candidate_count": 1, "reason_counts": {"provider_disabled": 2}},
        stage_traces=[{"stage": "filters"}],
        retry_wait_plan={"sleep_seconds": 1},
    )

    assert payload["route_round"] == 2
    assert payload["route_policy"] == "可用性优先"
    assert payload["failed_candidate_count"] == 1
    assert json.loads(payload["hard_filter_reason_counts_json"]) == {"provider_disabled": 2}
    assert json.loads(payload["stage_traces_json"]) == [{"stage": "filters"}]
