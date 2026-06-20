from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.routing.enums import RoutePipelineStrategy


@dataclass(frozen=True)
class RoutePipelineDefinition:
    strategy: RoutePipelineStrategy
    context_enrichers: tuple[Any, ...] = ()
    filters: tuple[Any, ...] = ()
    scorers: tuple[Any, ...] = ()
    orderers: tuple[Any, ...] = ()
    diagnosers: tuple[Any, ...] = ()


class RouteStrategyRegistry:
    def __init__(self) -> None:
        self._definitions: dict[RoutePipelineStrategy, RoutePipelineDefinition] = {}

    def register(self, definition: RoutePipelineDefinition) -> None:
        self._definitions[definition.strategy] = definition

    def get(self, strategy: RoutePipelineStrategy) -> RoutePipelineDefinition:
        try:
            return self._definitions[strategy]
        except KeyError as exc:
            raise ValueError(f"未注册路由策略: {strategy.value}") from exc

    def registered_strategies(self) -> list[RoutePipelineStrategy]:
        return sorted(self._definitions, key=lambda item: item.value)


def build_default_registry() -> RouteStrategyRegistry:
    from app.services.routing.diagnosers import StageTraceDiagnoser
    from app.services.routing.filters import RouteFilterChain
    from app.services.routing.orderers import AvailabilityFirstOrderer
    from app.services.routing.scorers import AvailabilityFirstScorer, CapacityAvoidanceScorer, LatencyFirstScorer

    registry = RouteStrategyRegistry()
    registry.register(
        RoutePipelineDefinition(
            strategy=RoutePipelineStrategy.AVAILABILITY_FIRST,
            filters=(RouteFilterChain(),),
            scorers=(AvailabilityFirstScorer(),),
            orderers=(AvailabilityFirstOrderer(),),
            diagnosers=(StageTraceDiagnoser(),),
        )
    )
    registry.register(
        RoutePipelineDefinition(
            strategy=RoutePipelineStrategy.LATENCY_FIRST,
            filters=(RouteFilterChain(),),
            scorers=(LatencyFirstScorer(),),
            orderers=(AvailabilityFirstOrderer(),),
            diagnosers=(StageTraceDiagnoser(),),
        )
    )
    registry.register(
        RoutePipelineDefinition(
            strategy=RoutePipelineStrategy.CAPACITY_AVOIDANCE,
            filters=(RouteFilterChain(),),
            scorers=(CapacityAvoidanceScorer(),),
            orderers=(AvailabilityFirstOrderer(),),
            diagnosers=(StageTraceDiagnoser(),),
        )
    )
    return registry
