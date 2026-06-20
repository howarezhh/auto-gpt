from __future__ import annotations

import time
from types import SimpleNamespace

from app.services.routing import RouteOutageService, RoutePolicyContext, RoutingService


def test_route_outage_wait_plan_uses_retry_after_detail() -> None:
    setting = SimpleNamespace(route_exhausted_retry_infinite_enabled=False, route_exhausted_retry_max_wait_seconds=600)
    plan = RouteOutageService.wait_plan(
        setting,
        started_at=time.perf_counter(),
        retry_round=0,
        upstream_error={"detail": {"retry_after_seconds": 2}},
    )

    assert plan["wait_source"] == "retry_after"
    assert plan["retry_after_seconds"] == 2.0
    assert plan["sleep_seconds"] >= 2.0


def test_route_outage_wait_plan_respects_zero_wait_window() -> None:
    setting = SimpleNamespace(route_exhausted_retry_infinite_enabled=False, route_exhausted_retry_max_wait_seconds=0)
    plan = RouteOutageService.wait_plan(setting, started_at=time.perf_counter(), retry_round=0)

    assert plan["sleep_seconds"] == 0.0
    assert plan["remaining_wait_seconds"] == 0.0


def test_route_outage_cache_key_includes_policy_context() -> None:
    first = RouteOutageService.cache_key(
        endpoint_path="/v1/chat/completions",
        model_name="测试模型",
        route_context=RoutePolicyContext(allowed_provider_ids=[1]),
        require_vision=False,
        require_stream=False,
        require_tools=False,
        require_image_generation=False,
        require_chat_completions=True,
        require_responses=False,
    )
    second = RouteOutageService.cache_key(
        endpoint_path="/v1/chat/completions",
        model_name="测试模型",
        route_context=RoutePolicyContext(allowed_provider_ids=[2]),
        require_vision=False,
        require_stream=False,
        require_tools=False,
        require_image_generation=False,
        require_chat_completions=True,
        require_responses=False,
    )

    assert first != second
    assert first.startswith("route-outage:")


def test_route_outage_retry_diagnostics_distinguishes_recoverable_capacity() -> None:
    diagnostics = {
        "matching_model_mount_count": 2,
        "pre_capacity_candidate_count": 2,
        "final_candidate_count": 0,
        "reason_counts": {"provider_capacity_exceeded": 2},
    }

    assert RouteOutageService.should_retry_diagnostics(diagnostics)


def test_route_outage_retry_diagnostics_rejects_nonrecoverable_model_mismatch() -> None:
    diagnostics = {
        "matching_model_mount_count": 1,
        "pre_capacity_candidate_count": 0,
        "final_candidate_count": 0,
        "reason_counts": {"chat_not_supported": 1},
    }

    assert not RouteOutageService.should_retry_diagnostics(diagnostics)


def test_route_outage_builds_retry_exhausted_error_payload() -> None:
    setting = SimpleNamespace(route_exhausted_retry_infinite_enabled=False, route_exhausted_retry_max_wait_seconds=3)

    payload = RouteOutageService.build_retry_exhausted_upstream_error(
        setting,
        started_at=time.perf_counter(),
        attempt_count=4,
        trace_id="trace-1",
        last_upstream_error={"status_code": 429, "detail": {"code": "rate_limit"}},
    )

    assert payload["status_code"] == 503
    assert payload["detail"]["attempt_count"] == 4
    assert payload["detail"]["max_wait_seconds"] == 3
    assert payload["detail"]["last_status_code"] == 429


def test_routing_service_next_retry_plan_returns_typed_plan() -> None:
    setting = SimpleNamespace(route_exhausted_retry_infinite_enabled=False, route_exhausted_retry_max_wait_seconds=600)

    plan = RoutingService.next_retry_plan(
        setting,
        started_at=time.perf_counter(),
        retry_round=0,
        diagnostics={
            "matching_model_mount_count": 1,
            "pre_capacity_candidate_count": 1,
            "final_candidate_count": 0,
            "reason_counts": {"provider_capacity_exceeded": 1},
        },
    )

    assert plan.should_retry
    assert plan.reason == "route_diagnostics_recoverable"
    assert plan.sleep_seconds > 0
