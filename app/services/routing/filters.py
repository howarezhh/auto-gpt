from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy.orm import Session

from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.services.provider_service import ProviderService
from app.services.provider_capacity_service import ProviderCapacityService, ProviderCapacitySnapshot
from app.services.routing import helpers as route_helpers
from app.services.routing.context import RouteCandidate, RoutePolicyContext, RouteRuntimeContext
from app.services.routing.decision import RouteCandidateTraceEvent
from app.services.routing.enums import RouteCandidateAction, RouteFilterId


@dataclass(slots=True)
class RouteFilterResult:
    kept_candidates: list[RouteCandidate]
    rejected_events: list[RouteCandidateTraceEvent] = field(default_factory=list)
    reason_counts: dict[str, int] = field(default_factory=dict)
    stage_summary: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RouteFilterDecision:
    allowed: bool
    reason_code: str = "candidate_kept"
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RouteFilterEvaluationContext:
    db: Session
    model_name: str | None
    route_context: RoutePolicyContext | None
    allowed_provider_ids: set[int] | None
    enabled_model_names: set[str]
    required_endpoint_path: str | None
    now: datetime
    require_vision: bool = False
    require_stream: bool = False
    require_tools: bool = False
    require_image_generation: bool = False
    require_chat_completions: bool = False
    require_responses: bool = False
    required_upstream_protocol_type: str | None = None
    claim_half_open_probe: bool = True

    @property
    def normalized_required_upstream_protocol_type(self) -> str | None:
        return route_helpers.normalize_required_upstream_protocol_type(self.required_upstream_protocol_type)


class RouteFilter(Protocol):
    id: RouteFilterId

    def apply(self, candidates: list[RouteCandidate], context: RouteRuntimeContext) -> RouteFilterResult:
        ...


class CandidateRuntimeFilter(Protocol):
    id: RouteFilterId

    def evaluate(
        self,
        provider: Provider,
        provider_model: ProviderModel,
        context: RouteFilterEvaluationContext,
    ) -> RouteFilterDecision:
        ...


class ProviderAvailabilityFilter:
    id = RouteFilterId.PROVIDER_ENABLED

    def evaluate(
        self,
        provider: Provider,
        provider_model: ProviderModel,
        context: RouteFilterEvaluationContext,
    ) -> RouteFilterDecision:
        if not provider.enabled:
            return RouteFilterDecision(False, "provider_disabled")
        if provider.circuit_state == "open":
            return RouteFilterDecision(False, "provider_circuit_open")
        if provider.maintenance_mode_enabled:
            return RouteFilterDecision(False, "provider_maintenance_mode")
        return RouteFilterDecision(True)


class ProviderContentTrustFilter:
    id = RouteFilterId.CONTENT_TRUST

    def evaluate(
        self,
        provider: Provider,
        provider_model: ProviderModel,
        context: RouteFilterEvaluationContext,
    ) -> RouteFilterDecision:
        reason = route_helpers.content_policy_diagnostic_reason(provider, route_context=context.route_context)
        if reason:
            return RouteFilterDecision(False, reason)
        return RouteFilterDecision(True)


class ProviderAuthorizationFilter:
    id = RouteFilterId.PROVIDER_AUTHORIZED

    def evaluate(
        self,
        provider: Provider,
        provider_model: ProviderModel,
        context: RouteFilterEvaluationContext,
    ) -> RouteFilterDecision:
        if context.allowed_provider_ids is not None and provider.id not in context.allowed_provider_ids:
            return RouteFilterDecision(False, "provider_not_authorized")
        return RouteFilterDecision(True)


class ProviderEndpointProtocolFilter:
    id = RouteFilterId.ENDPOINT_PROTOCOL

    def evaluate(
        self,
        provider: Provider,
        provider_model: ProviderModel,
        context: RouteFilterEvaluationContext,
    ) -> RouteFilterDecision:
        if route_helpers.provider_endpoint_protocol_allowed(
            provider,
            require_chat_completions=context.require_chat_completions,
            require_responses=context.require_responses,
            required_upstream_protocol_type=context.required_upstream_protocol_type,
        ):
            return RouteFilterDecision(True)
        required_native = context.normalized_required_upstream_protocol_type
        if required_native:
            return RouteFilterDecision(
                False,
                "protocol_mismatch",
                {
                    "required_protocol": required_native,
                    "actual_protocol": ProviderService.provider_protocol_type(provider),
                    "protocol_mismatch": True,
                },
            )
        if context.require_chat_completions:
            return RouteFilterDecision(False, "provider_chat_protocol_not_supported")
        return RouteFilterDecision(False, "provider_responses_protocol_not_supported")


class ProviderModelBasicFilter:
    id = RouteFilterId.MODEL_ENABLED

    def evaluate(
        self,
        provider: Provider,
        provider_model: ProviderModel,
        context: RouteFilterEvaluationContext,
    ) -> RouteFilterDecision:
        if not provider_model.enabled:
            return RouteFilterDecision(False, "model_disabled")
        if route_helpers.provider_model_blocked_by_content_policy(
            provider_model,
            provider=provider,
            route_context=context.route_context,
        ):
            return RouteFilterDecision(False, "model_content_integrity_blocked")
        if provider_model.model_name not in context.enabled_model_names:
            return RouteFilterDecision(False, "model_globally_disabled")
        if context.model_name and provider_model.model_name != context.model_name:
            return RouteFilterDecision(False, "model_name_mismatch")
        return RouteFilterDecision(True)


class ProviderModelEndpointProtocolFilter:
    id = RouteFilterId.ENDPOINT_PROTOCOL

    def evaluate(
        self,
        provider: Provider,
        provider_model: ProviderModel,
        context: RouteFilterEvaluationContext,
    ) -> RouteFilterDecision:
        if route_helpers.provider_model_endpoint_protocol_allowed(
            provider,
            provider_model,
            require_chat_completions=context.require_chat_completions,
            require_responses=context.require_responses,
            required_upstream_protocol_type=context.required_upstream_protocol_type,
        ):
            return RouteFilterDecision(True)
        required_native = context.normalized_required_upstream_protocol_type
        if required_native:
            return RouteFilterDecision(
                False,
                "protocol_mismatch",
                {
                    "required_protocol": required_native,
                    "actual_protocol": ProviderService.provider_or_model_native_protocol(provider, provider_model)
                    or ProviderService.provider_model_protocol_type(provider_model),
                    "protocol_mismatch": True,
                },
            )
        if context.require_chat_completions:
            return RouteFilterDecision(False, "chat_not_supported")
        return RouteFilterDecision(False, "responses_not_supported")


class HealthGateFilter:
    id = RouteFilterId.HEALTH_GATE

    def evaluate(
        self,
        provider: Provider,
        provider_model: ProviderModel,
        context: RouteFilterEvaluationContext,
    ) -> RouteFilterDecision:
        reason = route_helpers.health_gate_diagnostic_reason(
            provider,
            provider_model,
            route_context=context.route_context,
        )
        if reason:
            return RouteFilterDecision(False, reason)
        return RouteFilterDecision(True)


class CapabilityFilter:
    id = RouteFilterId.CAPABILITY

    def evaluate(
        self,
        provider: Provider,
        provider_model: ProviderModel,
        context: RouteFilterEvaluationContext,
    ) -> RouteFilterDecision:
        if context.require_stream and not provider_model.supports_stream:
            return RouteFilterDecision(False, "stream_not_supported")
        if (
            context.require_chat_completions
            and not provider_model.supports_chat_completions
            and not ProviderService.provider_or_model_native_protocol(provider, provider_model)
        ):
            return RouteFilterDecision(False, "chat_not_supported")
        if (
            context.require_responses
            and not provider_model.supports_responses
            and not ProviderService.provider_or_model_native_protocol(provider, provider_model)
        ):
            return RouteFilterDecision(False, "responses_not_supported")
        if context.require_vision and not provider_model.supports_vision:
            return RouteFilterDecision(False, "vision_not_supported")
        if context.require_tools and not provider_model.supports_tools:
            return RouteFilterDecision(False, "tools_not_supported")
        if context.require_image_generation and not ProviderService.provider_model_supports_image_generation(provider_model):
            return RouteFilterDecision(False, "image_generation_not_supported")
        return RouteFilterDecision(True)


class CapabilityProbeFilter:
    id = RouteFilterId.CAPABILITY

    def evaluate(
        self,
        provider: Provider,
        provider_model: ProviderModel,
        context: RouteFilterEvaluationContext,
    ) -> RouteFilterDecision:
        if context.require_vision and route_helpers.capability_probe_failed(
            provider,
            provider_model,
            "vision",
            endpoint_path=context.required_endpoint_path,
        ):
            return RouteFilterDecision(False, "vision_probe_unhealthy")
        if context.require_tools and route_helpers.capability_probe_failed(
            provider,
            provider_model,
            "tools",
            endpoint_path=context.required_endpoint_path,
        ):
            return RouteFilterDecision(False, "tools_probe_unhealthy")
        if context.require_image_generation and route_helpers.capability_probe_failed(
            provider,
            provider_model,
            "image_generation",
        ):
            return RouteFilterDecision(False, "image_generation_probe_unhealthy")
        return RouteFilterDecision(True)


class ModelCircuitFilter:
    id = RouteFilterId.CIRCUIT_BREAKER

    def evaluate(
        self,
        provider: Provider,
        provider_model: ProviderModel,
        context: RouteFilterEvaluationContext,
    ) -> RouteFilterDecision:
        if provider_model.circuit_state != "open":
            return RouteFilterDecision(True)
        if not route_helpers.should_probe_open_model(
            provider=provider,
            provider_model=provider_model,
            recovery_interval_sec=ProviderService.get_effective_recovery_probe_interval_sec(context.db, provider),
            now=context.now,
        ):
            return RouteFilterDecision(False, "model_circuit_open")
        if not context.claim_half_open_probe:
            return RouteFilterDecision(True, details={"half_open_probe_available": True})
        if not route_helpers.claim_half_open_probe(context.db, provider_model, context.now):
            return RouteFilterDecision(False, "model_circuit_probe_claim_failed")
        return RouteFilterDecision(True, details={"half_open_probe_claimed": True})


class CapacityFilter:
    id = RouteFilterId.CAPACITY

    def apply_with_snapshots(
        self,
        candidates: list[RouteCandidate],
        *,
        snapshots: dict[int, ProviderCapacitySnapshot],
        is_stream: bool,
    ) -> RouteFilterResult:
        kept: list[RouteCandidate] = []
        events: list[RouteCandidateTraceEvent] = []
        reason_counts: dict[str, int] = {}
        for candidate in candidates:
            snapshot = snapshots.get(candidate.provider.id)
            if snapshot is None:
                decision = RouteFilterDecision(False, "capacity_snapshot_unavailable")
            elif not ProviderCapacityService._has_capacity(candidate.provider, snapshot=snapshot, is_stream=is_stream):
                decision = RouteFilterDecision(False, "provider_capacity_exceeded")
            else:
                candidate.load_factor = route_helpers.capacity_load_factor(candidate.provider, snapshot, is_stream=is_stream)
                kept.append(candidate)
                continue
            reason_counts[decision.reason_code] = reason_counts.get(decision.reason_code, 0) + 1
            events.append(_event_for_rejection(candidate.provider, candidate.provider_model, decision))
        return RouteFilterResult(
            kept_candidates=kept,
            rejected_events=events,
            reason_counts=reason_counts,
            stage_summary={
                "filter_ids": [self.id.value],
                "reason_counts": reason_counts,
            },
        )


class RouteFilterChain:
    id = "route_filter_chain"

    def __init__(self, filters: tuple[CandidateRuntimeFilter, ...] | None = None) -> None:
        self.filters = filters or default_runtime_filters()

    def evaluate_candidate(
        self,
        provider: Provider,
        provider_model: ProviderModel,
        context: RouteFilterEvaluationContext,
    ) -> RouteFilterDecision:
        for route_filter in self.filters:
            decision = route_filter.evaluate(provider, provider_model, context)
            if not decision.allowed:
                return decision
        return RouteFilterDecision(True)

    def apply(self, candidates: list[RouteCandidate], context: RouteRuntimeContext) -> RouteFilterResult:
        evaluation_context = _evaluation_context_from_runtime(context)
        kept: list[RouteCandidate] = []
        rejected_events: list[RouteCandidateTraceEvent] = []
        reason_counts: dict[str, int] = {}
        for candidate in candidates:
            decision = self.evaluate_candidate(candidate.provider, candidate.provider_model, evaluation_context)
            if decision.allowed:
                kept.append(candidate)
                continue
            reason_counts[decision.reason_code] = reason_counts.get(decision.reason_code, 0) + 1
            rejected_events.append(_event_for_rejection(candidate.provider, candidate.provider_model, decision))
        return RouteFilterResult(
            kept_candidates=kept,
            rejected_events=rejected_events,
            reason_counts=reason_counts,
            stage_summary={
                "filter_ids": [route_filter.id.value for route_filter in self.filters],
                "reason_counts": reason_counts,
            },
        )


def default_runtime_filters() -> tuple[CandidateRuntimeFilter, ...]:
    return (
        ProviderAvailabilityFilter(),
        ProviderContentTrustFilter(),
        ProviderAuthorizationFilter(),
        ProviderEndpointProtocolFilter(),
        ProviderModelBasicFilter(),
        ProviderModelEndpointProtocolFilter(),
        HealthGateFilter(),
        CapabilityFilter(),
        CapabilityProbeFilter(),
        ModelCircuitFilter(),
    )


def evaluate_runtime_candidate(
    *,
    provider: Provider,
    provider_model: ProviderModel,
    context: RouteFilterEvaluationContext,
) -> RouteFilterDecision:
    return RouteFilterChain().evaluate_candidate(provider, provider_model, context)


def _evaluation_context_from_runtime(context: RouteRuntimeContext) -> RouteFilterEvaluationContext:
    metadata = context.metadata
    return RouteFilterEvaluationContext(
        db=metadata["db"],
        model_name=context.request.model_name,
        route_context=context.request.policy_context,
        allowed_provider_ids=metadata.get("allowed_provider_ids"),
        enabled_model_names=metadata["enabled_model_names"],
        required_endpoint_path=metadata.get("required_endpoint_path"),
        now=metadata["now"],
        **context.request.capabilities.as_kwargs(),
    )


def _event_for_rejection(
    provider: Provider,
    provider_model: ProviderModel,
    decision: RouteFilterDecision,
) -> RouteCandidateTraceEvent:
    return RouteCandidateTraceEvent(
        provider_id=provider.id,
        provider_model_id=provider_model.id,
        action=RouteCandidateAction.REJECTED,
        reason_code=decision.reason_code,
        reason_label=route_helpers.diagnostic_reason_label(decision.reason_code),
        details=decision.details,
    )
