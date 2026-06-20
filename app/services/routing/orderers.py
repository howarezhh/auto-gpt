from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.routing.context import RecentSessionRoute, RouteCandidate, RoutePolicyContext


@dataclass(slots=True)
class RouteOrderResult:
    candidates: list[RouteCandidate]
    events: list[dict[str, Any]] = field(default_factory=list)


class BalancedScoreOrderer:
    id = "balanced_score_orderer"
    route_score_tie_bucket_size = 10.0

    @classmethod
    def order(cls, candidates: list[RouteCandidate]) -> RouteOrderResult:
        ordered = sorted(
            candidates,
            key=lambda item: (
                -cls.route_score_tie_bucket(item.route_score),
                item.load_factor,
                -float(item.score_breakdown.get("preferred_provider_bonus") or 0.0),
                -item.route_score,
                item.provider_model.priority,
                item.provider.priority,
                item.provider.id,
                item.provider_model.id,
            ),
        )
        return RouteOrderResult(
            candidates=ordered,
            events=[
                {
                    "provider_id": item.provider.id,
                    "provider_model_id": item.provider_model.id,
                    "new_rank": rank,
                    "reason_code": "balanced_score_order",
                    "route_score": round(float(item.route_score or 0.0), 4),
                    "load_factor": round(float(item.load_factor or 0.0), 6),
                }
                for rank, item in enumerate(ordered, start=1)
            ],
        )

    @classmethod
    def route_score_tie_bucket(cls, route_score: float | None) -> int:
        score = max(0.0, float(route_score or 0.0))
        bucket = max(1.0, cls.route_score_tie_bucket_size)
        return int((score + bucket / 2.0) // bucket)


class RecentSessionOrderer:
    id = "recent_session_orderer"

    @staticmethod
    def order(
        candidates: list[RouteCandidate],
        *,
        recent_route: RecentSessionRoute | None,
    ) -> RouteOrderResult:
        if recent_route is None:
            return RouteOrderResult(candidates=candidates)
        recent_candidate = None
        if recent_route.provider_model_id is not None:
            recent_candidate = next((item for item in candidates if item.provider_model.id == recent_route.provider_model_id), None)
        if recent_candidate is None and recent_route.provider_id is not None:
            recent_candidate = next((item for item in candidates if item.provider.id == recent_route.provider_id), None)
        if recent_candidate is None:
            return RouteOrderResult(candidates=candidates)
        ordered = [recent_candidate, *[item for item in candidates if item is not recent_candidate]]
        recent_candidate.selection_reason = "recent_session"
        return RouteOrderResult(
            candidates=ordered,
            events=[
                {
                    "provider_id": recent_candidate.provider.id,
                    "provider_model_id": recent_candidate.provider_model.id,
                    "new_rank": 1,
                    "reason_code": "recent_session",
                }
            ],
        )


class FailedCandidateExclusionOrderer:
    id = "failed_candidate_exclusion_orderer"

    @staticmethod
    def order(
        candidates: list[RouteCandidate],
        *,
        excluded_candidate_keys: set[tuple[int, int]] | None,
    ) -> RouteOrderResult:
        if not excluded_candidate_keys:
            return RouteOrderResult(candidates=candidates)
        excluded = set(excluded_candidate_keys)
        ordered = [
            candidate
            for candidate in candidates
            if (candidate.provider.id, candidate.provider_model.id) not in excluded
        ]
        return RouteOrderResult(
            candidates=ordered,
            events=[
                {"provider_id": provider_id, "provider_model_id": model_id, "reason_code": "failed_candidate_excluded"}
                for provider_id, model_id in sorted(excluded)
            ],
        )


class CandidateWindowOrderer:
    id = "candidate_window_orderer"

    @staticmethod
    def order(
        candidates: list[RouteCandidate],
        *,
        max_count: int,
    ) -> RouteOrderResult:
        trimmed = candidates[: max(0, max_count)]
        return RouteOrderResult(
            candidates=trimmed,
            events=[{"candidate_window_count": len(trimmed), "reason_code": "candidate_window_trim"}],
        )


class AvailabilityFirstOrderer:
    id = "availability_first_orderer"

    @classmethod
    def order(
        cls,
        candidates: list[RouteCandidate],
        *,
        recent_route: RecentSessionRoute | None = None,
        route_context: RoutePolicyContext | None = None,
    ) -> RouteOrderResult:
        ordered: list[RouteCandidate] = []
        events: list[dict[str, Any]] = []
        seen: set[tuple[int, int]] = set()
        recent_candidate = recent_route_candidate(candidates, recent_route)
        if recent_candidate is not None:
            key = (recent_candidate.provider.id, recent_candidate.provider_model.id)
            recent_candidate.selection_reason = "recent_session"
            ordered.append(recent_candidate)
            seen.add(key)
            events.append(
                {
                    "provider_id": recent_candidate.provider.id,
                    "provider_model_id": recent_candidate.provider_model.id,
                    "new_rank": 1,
                    "reason_code": "recent_session",
                }
            )
        for health_tier in sorted({candidate.health_tier for candidate in candidates}):
            tier_candidates = [
                candidate
                for candidate in candidates
                if candidate.health_tier == health_tier
                and (candidate.provider.id, candidate.provider_model.id) not in seen
            ]
            balanced = BalancedScoreOrderer.order(tier_candidates)
            for candidate in balanced.candidates:
                key = (candidate.provider.id, candidate.provider_model.id)
                if key in seen:
                    continue
                candidate.selection_reason = selection_reason_for_balanced_candidate(
                    candidate,
                    tier_candidates,
                    route_context=route_context,
                )
                ordered.append(candidate)
                seen.add(key)
            events.extend(balanced.events)
        return RouteOrderResult(candidates=ordered, events=events)


def selection_reason_for_balanced_candidate(
    candidate: RouteCandidate,
    tier_candidates: list[RouteCandidate],
    *,
    route_context: RoutePolicyContext | None,
) -> str:
    preferred_ids = set(route_context.preferred_provider_ids or []) if route_context is not None else set()
    if candidate.provider.id in preferred_ids:
        return "preferred_bonus"
    same_score_bucket = [
        item
        for item in tier_candidates
        if BalancedScoreOrderer.route_score_tie_bucket(item.route_score) == BalancedScoreOrderer.route_score_tie_bucket(candidate.route_score)
    ]
    if len(same_score_bucket) > 1:
        min_load = min(float(item.load_factor or 0.0) for item in same_score_bucket)
        if float(candidate.load_factor or 0.0) <= min_load:
            return "low_load_tiebreak"
    return "highest_score"


def recent_route_candidate(
    candidates: list[RouteCandidate],
    recent_route: RecentSessionRoute | None,
) -> RouteCandidate | None:
    if recent_route is None:
        return None
    if recent_route.provider_model_id is not None:
        exact = next(
            (item for item in candidates if item.provider_model.id == recent_route.provider_model_id),
            None,
        )
        if exact is not None:
            exact.sticky_affinity = max(exact.sticky_affinity, 1_000_000.0)
            return exact
    if recent_route.provider_id is not None:
        same_provider = next(
            (item for item in candidates if item.provider.id == recent_route.provider_id),
            None,
        )
        if same_provider is not None:
            same_provider.sticky_affinity = max(same_provider.sticky_affinity, 500_000.0)
            return same_provider
    return None
