from __future__ import annotations

from unittest.mock import patch

from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.services.provider_capacity_service import ProviderCapacityUnavailableError
from app.services.proxy_service import ProxyService
from app.services.router_service import RouteCandidate, RoutePolicyContext, RouterService


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _candidate(provider_id: int, model_id: int, score: float) -> RouteCandidate:
    provider = Provider(
        id=provider_id,
        name=f"候选扩展测试提供商-{provider_id}",
        provider_type="openai_compatible",
        base_url="https://example.com/v1",
        api_key="upstream-secret",
        enabled=True,
        priority=10,
        health_status="healthy",
        circuit_state="closed",
    )
    provider_model = ProviderModel(
        id=model_id,
        provider_id=provider_id,
        model_name="候选扩展测试模型",
        enabled=True,
        priority=10,
        health_status="healthy",
        circuit_state="closed",
        supports_stream=True,
        supports_tools=True,
        supports_vision=True,
        supports_chat_completions=True,
        supports_responses=True,
    )
    return RouteCandidate(
        provider=provider,
        provider_model=provider_model,
        route_score=score,
        health_tier=0,
        load_factor=0.0,
    )


def main() -> None:
    candidates = [_candidate(index, index + 100, 1000 - index) for index in range(1, 6)]
    failed = {
        (candidates[0].provider.id, candidates[0].provider_model.id),
        (candidates[1].provider.id, candidates[1].provider_model.id),
    }
    with patch.object(RouterService, "_effective_candidate_attempt_count", return_value=2):
        ordered = RouterService._order_filtered_candidates(
            None,
            candidates,
            sticky_key=None,
            route_context=RoutePolicyContext(),
            excluded_candidate_keys=failed,
        )
    _assert([item.provider.id for item in ordered] == [3, 4], "failed candidates must be excluded before truncation")
    _assert(
        ordered[0].score_breakdown.get("failed_candidate_exclusion_count") == 2,
        "candidate trace should expose failed exclusion count",
    )

    _assert(
        ProxyService._should_retry_same_provider_status(
            429,
            {"code": "rate_limit_exceeded", "retry_after_seconds": 1},
        ),
        "429 with recoverable retry-after should use same-provider retry budget",
    )
    wait_plan = ProxyService._same_provider_retry_wait_plan(
        retry_index=0,
        upstream_error={"status_code": 429, "retry_after_seconds": 1},
    )
    _assert(wait_plan["wait_source"] == "retry_after", "same-provider retry should prefer Retry-After")
    _assert(wait_plan["retry_after_jitter_seconds"] is not None, "Retry-After wait should include short jitter")

    with patch(
        "app.services.router_service.ProviderCapacityService.snapshots",
        side_effect=ProviderCapacityUnavailableError("Redis ping failed"),
    ):
        try:
            RouterService._filter_capacity_candidates(candidates, is_stream=False)
        except ProviderCapacityUnavailableError as exc:
            _assert("Redis ping failed" in str(exc), "capacity error should preserve concrete reason")
        else:
            raise AssertionError("Redis capacity unavailable must not be swallowed")

    print("stage37 route outage capacity regression check passed")


if __name__ == "__main__":
    main()
