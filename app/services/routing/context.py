from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from app.services.routing.enums import RoutePipelineStrategy, RouteRequestKind


RouteCandidateKey = tuple[int, int]


@dataclass(slots=True)
class RouteCandidate:
    """描述单个可参与路由的 provider/model 候选项。"""

    provider: Any
    provider_model: Any
    recent_failure_rate: float = 0.0
    recent_success_rate: float = 1.0
    recent_avg_latency_ms: float | None = None
    route_score: float = 0.0
    health_tier: int = 1
    sticky_affinity: float = 0.0
    load_factor: float = 0.0
    score_breakdown: dict[str, Any] = field(default_factory=dict)
    selection_reason: str | None = None


@dataclass(slots=True)
class RecentSessionRoute:
    """记录同一会话最近一次成功使用的模型与提供商。"""

    provider_id: int | None
    provider_model_id: int | None
    model_name: str | None


@dataclass(slots=True)
class RoutePolicyContext:
    """描述一次路由决策的策略上下文。"""

    allowed_provider_ids: list[int] | None = None
    forced_provider_id: int | None = None
    preferred_provider_ids: list[int] | None = None
    preferred_region_tags: list[str] | None = None
    latency_bias: int = 1
    success_rate_bias: int = 1
    require_trusted_provider: bool = False
    content_guard_required: bool = True
    health_gate_mode: str | None = None
    route_strategy: str | RoutePipelineStrategy | None = RoutePipelineStrategy.AVAILABILITY_FIRST

    def with_forced_provider_id(self, forced_provider_id: int | None) -> "RoutePolicyContext":
        """返回一个仅修改强制 provider 配置的新上下文对象。"""
        return RoutePolicyContext(
            allowed_provider_ids=list(self.allowed_provider_ids) if self.allowed_provider_ids is not None else None,
            forced_provider_id=forced_provider_id,
            preferred_provider_ids=list(self.preferred_provider_ids) if self.preferred_provider_ids is not None else None,
            preferred_region_tags=list(self.preferred_region_tags) if self.preferred_region_tags is not None else None,
            latency_bias=self.latency_bias,
            success_rate_bias=self.success_rate_bias,
            require_trusted_provider=self.require_trusted_provider,
            content_guard_required=self.content_guard_required,
            health_gate_mode=self.health_gate_mode,
            route_strategy=self.route_strategy,
        )


@dataclass(slots=True)
class RouteCapabilitySet:
    require_vision: bool = False
    require_stream: bool = False
    require_tools: bool = False
    require_image_generation: bool = False
    require_chat_completions: bool = False
    require_responses: bool = False
    required_upstream_protocol_type: str | None = None

    def as_kwargs(self) -> dict[str, Any]:
        return {
            "require_vision": self.require_vision,
            "require_stream": self.require_stream,
            "require_tools": self.require_tools,
            "require_image_generation": self.require_image_generation,
            "require_chat_completions": self.require_chat_completions,
            "require_responses": self.require_responses,
            "required_upstream_protocol_type": self.required_upstream_protocol_type,
        }

    def fingerprint_parts(self) -> list[str]:
        return [
            "vision" if self.require_vision else "text",
            "stream" if self.require_stream else "json",
            "tools" if self.require_tools else "no-tools",
            "imagegen" if self.require_image_generation else "no-imagegen",
            "chat" if self.require_chat_completions else "any-chat",
            "responses" if self.require_responses else "any-responses",
            f"protocol:{self.required_upstream_protocol_type or 'openai-compatible'}",
        ]


@dataclass(slots=True)
class RouteRequest:
    requested_model: str | None
    selected_model: str | None
    endpoint_path: str
    public_endpoint_path: str
    request_kind: RouteRequestKind = RouteRequestKind.GENERIC
    capabilities: RouteCapabilitySet = field(default_factory=RouteCapabilitySet)
    policy_context: RoutePolicyContext | None = None
    db: Any | None = None
    sticky_key: str | None = None
    forced_provider_id: int | None = None
    excluded_candidate_keys: set[RouteCandidateKey] = field(default_factory=set)
    mapping_trace: dict[str, Any] | None = None
    debug_enabled: bool = True
    strategy: RoutePipelineStrategy = RoutePipelineStrategy.AVAILABILITY_FIRST

    def __post_init__(self) -> None:
        if self.policy_context is not None:
            self.strategy = normalize_route_strategy(getattr(self.policy_context, "route_strategy", None), self.strategy)
        else:
            self.strategy = normalize_route_strategy(self.strategy, RoutePipelineStrategy.AVAILABILITY_FIRST)

    @property
    def model_name(self) -> str | None:
        return self.selected_model or self.requested_model

    def cache_fingerprint(self) -> str:
        context = self.policy_context
        parts: list[str] = [
            str(self.strategy.value),
            self.requested_model or "*",
            self.selected_model or "*",
            self.endpoint_path or "*",
            self.public_endpoint_path or "*",
            self.request_kind.value,
            *self.capabilities.fingerprint_parts(),
        ]
        if context is not None:
            parts.extend(
                [
                    _digest_list(context.allowed_provider_ids),
                    _digest_list(context.preferred_provider_ids),
                    _digest_list(context.preferred_region_tags),
                    str(context.latency_bias),
                    str(context.success_rate_bias),
                    "trusted" if context.require_trusted_provider else "any-trust",
                    "guard" if context.content_guard_required else "guard-off",
                    str(context.health_gate_mode or ""),
                    normalize_route_strategy(context.route_strategy).value,
                ]
            )
        if self.forced_provider_id is not None:
            parts.append(f"forced:{self.forced_provider_id}")
        if self.excluded_candidate_keys:
            parts.append(
                "excluded:"
                + ",".join(f"{provider_id}:{model_id}" for provider_id, model_id in sorted(self.excluded_candidate_keys))
            )
        raw = "|".join(parts)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


@dataclass(slots=True)
class RouteRuntimeContext:
    request: RouteRequest
    raw_candidate_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


def _digest_list(values: list[Any] | None) -> str:
    if values is None:
        return "*"
    if not values:
        return "-"
    return hashlib.sha256(",".join(str(item) for item in values).encode("utf-8")).hexdigest()[:16]


def normalize_route_strategy(
    value: str | RoutePipelineStrategy | None,
    default: RoutePipelineStrategy = RoutePipelineStrategy.AVAILABILITY_FIRST,
) -> RoutePipelineStrategy:
    if isinstance(value, RoutePipelineStrategy):
        return value
    normalized = str(value or "").strip().lower()
    aliases = {
        "health_first": RoutePipelineStrategy.AVAILABILITY_FIRST,
        "healthy_first": RoutePipelineStrategy.AVAILABILITY_FIRST,
        "availability": RoutePipelineStrategy.AVAILABILITY_FIRST,
        "availability_first": RoutePipelineStrategy.AVAILABILITY_FIRST,
        "latency": RoutePipelineStrategy.LATENCY_FIRST,
        "latency_first": RoutePipelineStrategy.LATENCY_FIRST,
        "capacity": RoutePipelineStrategy.CAPACITY_AVOIDANCE,
        "capacity_avoidance": RoutePipelineStrategy.CAPACITY_AVOIDANCE,
        "capacity_aware": RoutePipelineStrategy.CAPACITY_AVOIDANCE,
    }
    return aliases.get(normalized, default)
