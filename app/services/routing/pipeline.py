from __future__ import annotations

from app.services.routing.context import RouteRuntimeContext
from app.services.routing.decision import RouteDecision
from app.services.routing.registry import RouteStrategyRegistry, build_default_registry


class RoutePipeline:
    def __init__(self, registry: RouteStrategyRegistry | None = None) -> None:
        self.registry = registry or build_default_registry()

    def create_runtime_context(self, decision: RouteDecision) -> RouteRuntimeContext:
        self.registry.get(decision.request.strategy)
        return RouteRuntimeContext(
            request=decision.request,
            raw_candidate_count=len(decision.candidates),
            metadata={"policy_version": decision.policy_version},
        )

