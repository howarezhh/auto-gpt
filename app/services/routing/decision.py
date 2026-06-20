from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.routing.context import RouteCandidateKey, RouteRequest
from app.services.routing.enums import RouteCandidateAction, RouteStage, RouteStageResult


@dataclass(slots=True)
class RouteCandidateTraceEvent:
    provider_id: int | None
    provider_model_id: int | None
    action: RouteCandidateAction
    reason_code: str
    reason_label: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "provider_model_id": self.provider_model_id,
            "action": self.action.value,
            "reason_code": self.reason_code,
            "reason_label": self.reason_label,
            "details": self.details,
        }


@dataclass(slots=True)
class RouteStageTrace:
    stage: RouteStage
    strategy_id: str
    result: RouteStageResult
    input_count: int
    output_count: int
    events: list[RouteCandidateTraceEvent] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "strategy_id": self.strategy_id,
            "result": self.result.value,
            "input_count": self.input_count,
            "output_count": self.output_count,
            "events": [event.to_dict() for event in self.events],
            "summary": self.summary,
        }


@dataclass(slots=True)
class RouteDiagnostics:
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def reason_counts(self) -> dict[str, Any]:
        value = self.payload.get("reason_counts")
        return value if isinstance(value, dict) else {}

    @property
    def summary(self) -> str | None:
        value = self.payload.get("summary")
        return value if isinstance(value, str) else None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


@dataclass(slots=True)
class RouteRetryPlan:
    sleep_seconds: float = 0.0
    should_retry: bool = False
    reason: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sleep_seconds": self.sleep_seconds,
            "should_retry": self.should_retry,
            "reason": self.reason,
            **self.payload,
        }


@dataclass(slots=True)
class RouteDecision:
    request: RouteRequest
    candidates: list[Any] = field(default_factory=list)
    selected: Any | None = None
    diagnostics: RouteDiagnostics = field(default_factory=RouteDiagnostics)
    stage_traces: list[RouteStageTrace] = field(default_factory=list)
    retry_plan: RouteRetryPlan | None = None
    cache_key: str | None = None
    policy_version: str = "route-pipeline-v1"

    def candidate_keys(self) -> list[RouteCandidateKey]:
        return [
            (int(candidate.provider.id), int(candidate.provider_model.id))
            for candidate in self.candidates
        ]

    def top_candidates_summary(self, limit: int = 10) -> list[dict[str, Any]]:
        summary: list[dict[str, Any]] = []
        for rank, candidate in enumerate(self.candidates[: max(0, limit)], start=1):
            summary.append(
                {
                    "rank": rank,
                    "provider_id": candidate.provider.id,
                    "provider_name": candidate.provider.name,
                    "provider_model_id": candidate.provider_model.id,
                    "provider_model_name": candidate.provider_model.model_name,
                    "health_tier": candidate.health_tier,
                    "load_factor": round(float(candidate.load_factor or 0.0), 6),
                    "route_score": round(float(candidate.route_score or 0.0), 4),
                    "selection_reason": candidate.selection_reason,
                    "score_breakdown": candidate.score_breakdown or {},
                }
            )
        return summary

    def stage_traces_payload(self) -> list[dict[str, Any]]:
        return [stage_trace.to_dict() for stage_trace in self.stage_traces]

    def to_trace_payload(self) -> dict[str, Any]:
        selected_provider_id = self.selected.provider.id if self.selected is not None else None
        selected_provider_model_id = self.selected.provider_model.id if self.selected is not None else None
        return {
            "route_policy": self.request.strategy.value,
            "policy_version": self.policy_version,
            "candidate_count": len(self.candidates),
            "selected_provider_id": selected_provider_id,
            "selected_provider_model_id": selected_provider_model_id,
            "selected_reason": getattr(self.selected, "selection_reason", None) if self.selected is not None else None,
            "top_candidates": self.top_candidates_summary(),
            "diagnostics": self.diagnostics.to_dict(),
            "stage_traces": self.stage_traces_payload(),
        }

