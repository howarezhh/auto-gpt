from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.services.provider_capacity_service import ProviderCapacitySnapshot
from app.services.routing import helpers as route_helpers
from app.services.routing.context import RoutePolicyContext


@dataclass(slots=True)
class RouteScoreItem:
    scorer_id: str
    label: str
    value: float
    direction: Literal["bonus", "penalty"]
    reason_code: str
    details: dict[str, Any] = field(default_factory=dict)

    def signed_value(self) -> float:
        return self.value if self.direction == "bonus" else -self.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "scorer_id": self.scorer_id,
            "label": self.label,
            "value": round(float(self.value), 4),
            "direction": self.direction,
            "reason_code": self.reason_code,
            "details": self.details,
        }


class AvailabilityFirstScorer:
    id = "availability_first_score"

    @classmethod
    def score_breakdown(
        cls,
        *,
        provider: Provider,
        provider_model: ProviderModel,
        recent_success_rate: float,
        recent_avg_latency_ms: float | None,
        is_stream: bool = False,
        model_health_state: dict[str, Any] | None = None,
        provider_health_state: dict[str, Any] | None = None,
        capacity_snapshot: ProviderCapacitySnapshot | None = None,
        route_context: RoutePolicyContext | None = None,
    ) -> dict[str, Any]:
        items = cls.score_items(
            provider=provider,
            provider_model=provider_model,
            recent_success_rate=recent_success_rate,
            recent_avg_latency_ms=recent_avg_latency_ms,
            is_stream=is_stream,
            model_health_state=model_health_state,
            provider_health_state=provider_health_state,
            capacity_snapshot=capacity_snapshot,
            route_context=route_context,
        )
        values = {item.scorer_id: item.value for item in items}
        final_score = sum(item.signed_value() for item in items)
        model_health_state = model_health_state or {}
        provider_health_state = provider_health_state or {}
        effective_health_status = str(model_health_state.get("health_status") or provider_model.health_status or "unknown")
        effective_circuit_state = str(model_health_state.get("circuit_state") or provider_model.circuit_state or "closed")
        success_rate = float(model_health_state.get("success_rate_5m") if model_health_state.get("success_rate_5m") is not None else recent_success_rate)
        failure_rate = float(model_health_state.get("failure_rate_5m") or max(0.0, 1.0 - success_rate))
        ewma_latency = model_health_state.get("ewma_latency_ms")
        ewma_ttfb = model_health_state.get("ewma_ttfb_ms")
        latency_source = ewma_latency if ewma_latency is not None else (recent_avg_latency_ms or provider_model.last_latency_ms or provider_health_state.get("ewma_latency_ms") or 0)
        return {
            "health_score": round(values["health_score"], 4),
            "model_priority_score": round(values["model_priority_score"], 4),
            "provider_priority_score": round(values["provider_priority_score"], 4),
            "preferred_provider_bonus": round(values["preferred_provider_bonus"], 4),
            "success_score": round(values["success_score"], 4),
            "region_bonus": round(values["region_bonus"], 4),
            "latency_penalty": round(values["latency_penalty"], 4),
            "ttfb_penalty": round(values["ttfb_penalty"], 4),
            "saturation_penalty": round(values["saturation_penalty"], 4),
            "recent_error_penalty": round(values["recent_error_penalty"], 4),
            "success_rate": round(success_rate, 6),
            "failure_rate": round(failure_rate, 6),
            "latency_source_ms": float(latency_source or 0),
            "ewma_ttfb_ms": float(ewma_ttfb or 0),
            "health_status": effective_health_status,
            "circuit_state": effective_circuit_state,
            "final_score": round(final_score, 4),
            "score_items": [item.to_dict() for item in items],
        }

    @staticmethod
    def score_items(
        *,
        provider: Provider,
        provider_model: ProviderModel,
        recent_success_rate: float,
        recent_avg_latency_ms: float | None,
        is_stream: bool = False,
        model_health_state: dict[str, Any] | None = None,
        provider_health_state: dict[str, Any] | None = None,
        capacity_snapshot: ProviderCapacitySnapshot | None = None,
        route_context: RoutePolicyContext | None = None,
    ) -> list[RouteScoreItem]:
        model_health_state = model_health_state or {}
        provider_health_state = provider_health_state or {}
        effective_health_status = str(model_health_state.get("health_status") or provider_model.health_status or "unknown")
        effective_circuit_state = str(model_health_state.get("circuit_state") or provider_model.circuit_state or "closed")
        health_score = {
            "healthy": 100.0,
            "degraded": 60.0,
            "unknown": 55.0,
            "unhealthy": 0.0,
        }.get(effective_health_status, 50.0)
        if effective_circuit_state == "half_open":
            health_score = min(health_score, 35.0)
        elif effective_circuit_state == "open":
            health_score = 0.0

        priority_score = max(0.0, 30.0 - float(provider_model.priority))
        provider_priority_score = max(0.0, 20.0 - float(provider.priority))
        preferred_provider_bonus = 0.0
        if route_context and provider.id in set(route_context.preferred_provider_ids or []):
            preferred_provider_bonus = 35.0
        latency_bias = route_context.latency_bias if route_context is not None else 1
        success_rate_bias = route_context.success_rate_bias if route_context is not None else 1
        success_rate = float(model_health_state.get("success_rate_5m") if model_health_state.get("success_rate_5m") is not None else recent_success_rate)
        failure_rate = float(model_health_state.get("failure_rate_5m") or max(0.0, 1.0 - success_rate))
        ewma_latency = model_health_state.get("ewma_latency_ms")
        ewma_ttfb = model_health_state.get("ewma_ttfb_ms")
        latency_source = ewma_latency if ewma_latency is not None else (recent_avg_latency_ms or provider_model.last_latency_ms or provider_health_state.get("ewma_latency_ms") or 0)
        latency_penalty = min(30.0, float(latency_source or 0) / 100.0) * max(latency_bias, 0)
        ttfb_penalty = min(25.0, float(ewma_ttfb or 0) / 100.0) if is_stream else 0.0
        success_score = (success_rate * 40.0) * max(success_rate_bias, 0)
        recent_error_penalty = min(45.0, failure_rate * 100.0)
        saturation_penalty = route_helpers.saturation_penalty(provider, capacity_snapshot, is_stream=is_stream)
        region_bonus = 0.0
        if route_context and route_context.preferred_region_tags and provider.region_tag in set(route_context.preferred_region_tags):
            region_bonus = 20.0

        return [
            RouteScoreItem("health_score", "可用性分", health_score, "bonus", "health_status"),
            RouteScoreItem("model_priority_score", "模型优先级分", priority_score, "bonus", "model_priority"),
            RouteScoreItem("provider_priority_score", "提供商优先级分", provider_priority_score, "bonus", "provider_priority"),
            RouteScoreItem("preferred_provider_bonus", "偏好提供商加分", preferred_provider_bonus, "bonus", "preferred_provider"),
            RouteScoreItem("success_score", "成功率分", success_score, "bonus", "success_rate"),
            RouteScoreItem("region_bonus", "地区加分", region_bonus, "bonus", "preferred_region"),
            RouteScoreItem("latency_penalty", "延迟惩罚", latency_penalty, "penalty", "latency"),
            RouteScoreItem("ttfb_penalty", "首 token 惩罚", ttfb_penalty, "penalty", "stream_ttfb"),
            RouteScoreItem("saturation_penalty", "容量饱和惩罚", saturation_penalty, "penalty", "capacity_saturation"),
            RouteScoreItem("recent_error_penalty", "近期错误惩罚", recent_error_penalty, "penalty", "recent_error"),
        ]


class LatencyFirstScorer(AvailabilityFirstScorer):
    id = "latency_first_score"

    @staticmethod
    def score_items(**kwargs: Any) -> list[RouteScoreItem]:
        items = AvailabilityFirstScorer.score_items(**kwargs)
        adjusted: list[RouteScoreItem] = []
        for item in items:
            if item.scorer_id == "latency_penalty":
                adjusted.append(
                    RouteScoreItem(
                        "latency_penalty",
                        "延迟惩罚",
                        item.value * 2.5,
                        "penalty",
                        "latency_first",
                        {**item.details, "strategy_weight": 2.5},
                    )
                )
            elif item.scorer_id == "ttfb_penalty":
                adjusted.append(
                    RouteScoreItem(
                        "ttfb_penalty",
                        "首 token 惩罚",
                        item.value * 2.5,
                        "penalty",
                        "latency_first_stream_ttfb",
                        {**item.details, "strategy_weight": 2.5},
                    )
                )
            else:
                adjusted.append(item)
        return adjusted


class CapacityAvoidanceScorer(AvailabilityFirstScorer):
    id = "capacity_avoidance_score"

    @staticmethod
    def score_items(**kwargs: Any) -> list[RouteScoreItem]:
        items = AvailabilityFirstScorer.score_items(**kwargs)
        adjusted: list[RouteScoreItem] = []
        for item in items:
            if item.scorer_id == "saturation_penalty":
                adjusted.append(
                    RouteScoreItem(
                        "saturation_penalty",
                        "容量饱和惩罚",
                        item.value * 2.5,
                        "penalty",
                        "capacity_avoidance",
                        {**item.details, "strategy_weight": 2.5},
                    )
                )
            else:
                adjusted.append(item)
        return adjusted
