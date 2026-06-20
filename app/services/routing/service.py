from __future__ import annotations

from threading import Lock
from typing import Any

from starlette.concurrency import run_in_threadpool

from app.database import SessionLocal
from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.services.cache_service import CacheService
from app.services.log_service import LogService
from app.services.model_catalog_service import ModelCatalogService
from app.services.provider_capacity_service import ProviderCapacityService, ProviderCapacityUnavailableError
from app.services.provider_health_state_service import ProviderHealthStateService
from app.services.provider_service import ProviderService
from app.services.setting_service import SettingService
from app.services.routing import helpers as route_helpers
from app.services.routing.context import (
    RouteCandidate,
    RouteCandidateKey,
    RouteCapabilitySet,
    RoutePolicyContext,
    RouteRequest,
    normalize_route_strategy,
)
from app.services.routing.decision import (
    RouteCandidateTraceEvent,
    RouteDecision,
    RouteDiagnostics,
    RouteRetryPlan,
    RouteStageTrace,
)
from app.services.routing.enums import RouteCandidateAction, RouteStage, RouteStageResult
from app.services.routing.filters import CapacityFilter
from app.services.routing.filters import (
    CapabilityFilter,
    CapabilityProbeFilter,
    HealthGateFilter,
    ModelCircuitFilter,
    ProviderAuthorizationFilter,
    ProviderAvailabilityFilter,
    ProviderContentTrustFilter,
    ProviderEndpointProtocolFilter,
    ProviderModelBasicFilter,
    ProviderModelEndpointProtocolFilter,
    RouteFilterChain,
    RouteFilterDecision,
    RouteFilterEvaluationContext,
)
from app.services.routing.orderers import (
    AvailabilityFirstOrderer,
    CandidateWindowOrderer,
    FailedCandidateExclusionOrderer,
)
from app.services.routing.pipeline import RoutePipeline
from app.services.routing.registry import RouteStrategyRegistry, build_default_registry
from app.services.routing.outage import RouteOutageService
from app.utils.json_utils import dumps_json
from app.utils.timezone import now_beijing


class RoutingService:
    _registry: RouteStrategyRegistry = build_default_registry()
    _pipeline = RoutePipeline(_registry)
    RECENT_WINDOW_MINUTES = 5
    CANDIDATE_CACHE_LOCK_LIMIT = 2048
    _candidate_cache_locks: dict[str, Lock] = {}
    _candidate_cache_locks_guard = Lock()

    @classmethod
    def registry(cls) -> RouteStrategyRegistry:
        return cls._registry

    @staticmethod
    def build_policy_context(auth_context: Any, setting: Any) -> RoutePolicyContext:
        route_context = getattr(auth_context, "route_context", None)
        if isinstance(route_context, RoutePolicyContext):
            return route_context
        return RoutePolicyContext(
            allowed_provider_ids=getattr(auth_context, "allowed_provider_ids", None),
            health_gate_mode=str(getattr(setting, "route_health_gate_mode", "permissive") or "permissive"),
            require_trusted_provider=bool(getattr(setting, "trusted_providers_only", False)),
            route_strategy=normalize_route_strategy(getattr(setting, "route_strategy", None)),
        )

    @classmethod
    async def select_candidates(cls, request: RouteRequest) -> RouteDecision:
        if request.db is None:
            raise ValueError("RouteRequest.db is required for route candidate selection")
        candidates = await run_in_threadpool(cls._select_ordered_candidates_with_scoped_session, request)
        diagnostics = await cls.diagnose(request)
        decision = RouteDecision(
            request=request,
            candidates=candidates,
            selected=candidates[0] if candidates else None,
            diagnostics=diagnostics,
            cache_key=request.cache_fingerprint(),
        )
        decision.stage_traces.extend(cls._build_stage_traces(decision))
        cls._pipeline.create_runtime_context(decision)
        return decision

    @classmethod
    async def preview_candidates(cls, request: RouteRequest) -> RouteDecision:
        return await cls.select_candidates(request)

    @classmethod
    def select_candidates_sync(cls, request: RouteRequest) -> RouteDecision:
        if request.db is None:
            raise ValueError("RouteRequest.db is required for route candidate selection")
        candidates = cls._select_ordered_candidates_sync(request.db, request)
        diagnostics = cls.diagnose_sync(request)
        decision = RouteDecision(
            request=request,
            candidates=candidates,
            selected=candidates[0] if candidates else None,
            diagnostics=diagnostics,
            cache_key=request.cache_fingerprint(),
        )
        decision.stage_traces.extend(cls._build_stage_traces(decision))
        cls._pipeline.create_runtime_context(decision)
        return decision

    @classmethod
    def preview_candidates_sync(cls, request: RouteRequest) -> RouteDecision:
        return cls.select_candidates_sync(request)

    @classmethod
    def _select_ordered_candidates_with_scoped_session(cls, request: RouteRequest) -> list[Any]:
        db = SessionLocal()
        try:
            return cls._select_ordered_candidates_sync(db, request)
        finally:
            db.close()

    @classmethod
    def _select_ordered_candidates_sync(cls, db: Any, request: RouteRequest) -> list[Any]:
        candidates = cls._available_candidates(db, request)
        route_context = request.policy_context
        effective_forced_provider_id = (
            route_context.forced_provider_id
            if route_context and route_context.forced_provider_id is not None
            else request.forced_provider_id
        )
        if effective_forced_provider_id is not None:
            candidates = [item for item in candidates if item.provider.id == effective_forced_provider_id]
        candidates = cls._apply_capacity_filter(candidates, is_stream=request.capabilities.require_stream)
        if route_context and route_context.preferred_provider_ids:
            preferred_set = set(route_context.preferred_provider_ids)
            candidates = [
                *[item for item in candidates if item.provider.id in preferred_set],
                *[item for item in candidates if item.provider.id not in preferred_set],
            ]
        recent_route = route_helpers.load_recent_session_route(db, request.sticky_key)
        orderer = cls.registry().get(request.strategy).orderers[0]
        ordered = orderer.order(
            candidates,
            recent_route=recent_route,
            route_context=route_context,
        ).candidates
        before_exclusion_count = len(ordered)
        ordered = FailedCandidateExclusionOrderer.order(
            ordered,
            excluded_candidate_keys=request.excluded_candidate_keys,
        ).candidates
        excluded_count = before_exclusion_count - len(ordered)
        if excluded_count:
            for candidate in ordered[: cls.effective_candidate_attempt_count(route_context)]:
                candidate.score_breakdown["failed_candidate_exclusion_count"] = excluded_count
                candidate.score_breakdown["candidate_window_after_exclusion"] = True
        return CandidateWindowOrderer.order(
            ordered,
            max_count=cls.effective_candidate_attempt_count(route_context),
        ).candidates

    @classmethod
    def _candidate_cache_lock(cls, cache_key: str) -> Lock:
        with cls._candidate_cache_locks_guard:
            lock = cls._candidate_cache_locks.get(cache_key)
            if lock is None:
                if len(cls._candidate_cache_locks) >= cls.CANDIDATE_CACHE_LOCK_LIMIT:
                    cls._candidate_cache_locks.clear()
                lock = Lock()
                cls._candidate_cache_locks[cache_key] = lock
            return lock

    @classmethod
    def _available_candidates(cls, db: Any, request: RouteRequest) -> list[RouteCandidate]:
        cache_key = f"route-candidates:{request.cache_fingerprint()}"
        cached_candidates = CacheService.get(cache_key)
        if isinstance(cached_candidates, list) and all(isinstance(item, dict) for item in cached_candidates):
            return cls._hydrate_candidate_cache_entries(db, cached_candidates, request)
        if isinstance(cached_candidates, list):
            CacheService.invalidate(cache_key)
        with cls._candidate_cache_lock(cache_key):
            cached_candidates = CacheService.get(cache_key)
            if isinstance(cached_candidates, list) and all(isinstance(item, dict) for item in cached_candidates):
                return cls._hydrate_candidate_cache_entries(db, cached_candidates, request)
            if isinstance(cached_candidates, list):
                CacheService.invalidate(cache_key)
            candidates = cls._load_available_candidates_uncached(db, request)
            cls._cache_candidate_entries(cache_key, candidates)
            return candidates

    @classmethod
    def _hydrate_candidate_cache_entries(
        cls,
        db: Any,
        entries: list[dict[str, Any]],
        request: RouteRequest,
    ) -> list[RouteCandidate]:
        if not entries:
            return []
        providers = ProviderService.list_runtime_providers(db)
        provider_map = {provider.id: provider for provider in providers}
        model_map: dict[tuple[int, int], ProviderModel] = {}
        for provider in providers:
            for provider_model in provider.provider_models:
                model_map[(provider.id, provider_model.id)] = provider_model
        metrics = LogService.route_metric_summary(db, window_minutes=cls.RECENT_WINDOW_MINUTES, requested_model=request.model_name)
        enabled_model_names = ModelCatalogService.enabled_model_name_set(db)
        required_endpoint_path = route_helpers.required_endpoint_path(
            require_chat_completions=request.capabilities.require_chat_completions,
            require_responses=request.capabilities.require_responses,
        )
        capacity_provider_ids = {
            provider_id
            for entry in entries
            for provider_id in (route_helpers.parse_int(entry.get("provider_id")),)
            if provider_id is not None
        }
        try:
            capacity_snapshots = ProviderCapacityService.snapshots(capacity_provider_ids)
        except ProviderCapacityUnavailableError:
            raise
        except Exception as exc:
            raise ProviderCapacityUnavailableError(str(exc)) from exc
        hydrated: list[RouteCandidate] = []
        now = now_beijing()
        allowed_provider_ids = (
            set(request.policy_context.allowed_provider_ids)
            if request.policy_context and request.policy_context.allowed_provider_ids is not None
            else None
        )
        for entry in entries:
            provider_id = route_helpers.parse_int(entry.get("provider_id"))
            provider_model_id = route_helpers.parse_int(entry.get("provider_model_id"))
            if provider_id is None or provider_model_id is None:
                continue
            provider = provider_map.get(provider_id)
            provider_model = model_map.get((provider_id, provider_model_id))
            if provider is None or provider_model is None:
                continue
            if not cls._candidate_passes_runtime_filters(
                db,
                provider=provider,
                provider_model=provider_model,
                request=request,
                allowed_provider_ids=allowed_provider_ids,
                enabled_model_names=enabled_model_names,
                required_endpoint_path=required_endpoint_path,
                now=now,
            ):
                continue
            hydrated.append(
                cls._build_route_candidate(
                    provider=provider,
                    provider_model=provider_model,
                    metrics=metrics,
                    capacity_snapshot=capacity_snapshots.get(provider.id),
                    request=request,
                )
            )
        return hydrated

    @classmethod
    def _load_available_candidates_uncached(cls, db: Any, request: RouteRequest) -> list[RouteCandidate]:
        providers = ProviderService.list_runtime_providers(db)
        now = now_beijing()
        metrics = LogService.route_metric_summary(db, window_minutes=cls.RECENT_WINDOW_MINUTES, requested_model=request.model_name)
        route_context = request.policy_context
        allowed_provider_ids = set(route_context.allowed_provider_ids) if route_context and route_context.allowed_provider_ids is not None else None
        enabled_model_names = ModelCatalogService.enabled_model_name_set(db)
        required_endpoint_path = route_helpers.required_endpoint_path(
            require_chat_completions=request.capabilities.require_chat_completions,
            require_responses=request.capabilities.require_responses,
        )
        capacity_provider_ids = {
            provider.id
            for provider in providers
            if provider.enabled
            and provider.circuit_state != "open"
            and not provider.maintenance_mode_enabled
            and (allowed_provider_ids is None or provider.id in allowed_provider_ids)
            and route_helpers.provider_endpoint_protocol_allowed(
                provider,
                require_chat_completions=request.capabilities.require_chat_completions,
                require_responses=request.capabilities.require_responses,
                required_upstream_protocol_type=request.capabilities.required_upstream_protocol_type,
            )
        }
        try:
            capacity_snapshots = ProviderCapacityService.snapshots(capacity_provider_ids)
        except ProviderCapacityUnavailableError:
            raise
        except Exception as exc:
            raise ProviderCapacityUnavailableError(str(exc)) from exc

        candidates: list[RouteCandidate] = []
        for provider in providers:
            for provider_model in provider.provider_models:
                if not cls._candidate_passes_runtime_filters(
                    db,
                    provider=provider,
                    provider_model=provider_model,
                    request=request,
                    allowed_provider_ids=allowed_provider_ids,
                    enabled_model_names=enabled_model_names,
                    required_endpoint_path=required_endpoint_path,
                    now=now,
                ):
                    continue
                candidates.append(
                    cls._build_route_candidate(
                        provider=provider,
                        provider_model=provider_model,
                        metrics=metrics,
                        capacity_snapshot=capacity_snapshots.get(provider.id),
                        request=request,
                    )
                )
                if len(candidates) >= route_helpers.RAW_CANDIDATE_LIMIT:
                    return candidates
        return candidates

    @staticmethod
    def _cache_candidate_entries(cache_key: str, candidates: list[RouteCandidate]) -> None:
        entries = [
            {"provider_id": candidate.provider.id, "provider_model_id": candidate.provider_model.id}
            for candidate in candidates
        ]
        CacheService.set(cache_key, entries, ttl_seconds=RoutingService._route_candidate_cache_ttl_seconds())

    @staticmethod
    def _route_candidate_cache_ttl_seconds() -> int:
        try:
            raw_value = getattr(SettingService.get_cached(), "route_candidate_cache_ttl_sec", 10)
            if raw_value is None:
                return 10
            value = int(raw_value)
            if value <= 0:
                return 0
            return min(value, 60)
        except Exception:
            return 10

    @staticmethod
    def _candidate_passes_runtime_filters(
        db: Any,
        *,
        provider: Provider,
        provider_model: ProviderModel,
        request: RouteRequest,
        allowed_provider_ids: set[int] | None,
        enabled_model_names: set[str],
        required_endpoint_path: str | None,
        now: Any,
    ) -> bool:
        decision = RouteFilterChain().evaluate_candidate(
            provider,
            provider_model,
            RouteFilterEvaluationContext(
                db=db,
                model_name=request.model_name,
                route_context=request.policy_context,
                allowed_provider_ids=allowed_provider_ids,
                enabled_model_names=enabled_model_names,
                required_endpoint_path=required_endpoint_path,
                now=now,
                **request.capabilities.as_kwargs(),
            ),
        )
        return decision.allowed

    @classmethod
    def _build_route_candidate(
        cls,
        *,
        provider: Provider,
        provider_model: ProviderModel,
        metrics: dict[tuple[int, str], dict[str, Any]],
        capacity_snapshot: Any | None,
        request: RouteRequest,
    ) -> RouteCandidate:
        metric = metrics.get((provider.id, provider_model.model_name), {})
        recent_failure_rate = float(metric.get("failure_rate", 0.0))
        recent_success_rate = float(metric.get("success_rate", 1.0))
        recent_avg_latency_ms = metric.get("avg_latency_ms")
        model_health_state = ProviderHealthStateService.get_model_state(provider.id, provider_model.id)
        provider_health_state = ProviderHealthStateService.get_provider_state(provider.id)
        scorer = cls.registry().get(request.strategy).scorers[0]
        score_breakdown = scorer.score_breakdown(
            provider=provider,
            provider_model=provider_model,
            recent_success_rate=recent_success_rate,
            recent_avg_latency_ms=recent_avg_latency_ms,
            is_stream=request.capabilities.require_stream,
            model_health_state=model_health_state,
            provider_health_state=provider_health_state,
            capacity_snapshot=capacity_snapshot,
            route_context=request.policy_context,
        )
        return RouteCandidate(
            provider=provider,
            provider_model=provider_model,
            recent_failure_rate=recent_failure_rate,
            recent_success_rate=recent_success_rate,
            recent_avg_latency_ms=recent_avg_latency_ms,
            route_score=float(score_breakdown.get("final_score") or 0.0),
            health_tier=route_helpers.health_tier(
                provider=provider,
                provider_model=provider_model,
                model_health_state=model_health_state,
                provider_health_state=provider_health_state,
            ),
            score_breakdown=score_breakdown,
        )

    @staticmethod
    def _apply_capacity_filter(candidates: list[Any], *, is_stream: bool) -> list[Any]:
        if not candidates:
            return []
        snapshots = ProviderCapacityService.snapshots({item.provider.id for item in candidates})
        return CapacityFilter().apply_with_snapshots(candidates, snapshots=snapshots, is_stream=is_stream).kept_candidates

    @staticmethod
    async def diagnose(request: RouteRequest) -> RouteDiagnostics:
        return await run_in_threadpool(RoutingService.diagnose_sync, request)

    @staticmethod
    def diagnose_sync(request: RouteRequest) -> RouteDiagnostics:
        if request.db is None:
            raise ValueError("RouteRequest.db is required for route diagnostics")
        return RouteDiagnostics(payload=RoutingService._diagnose_candidate_unavailability(request.db, request))

    @classmethod
    def _diagnose_candidate_unavailability(cls, db: Any, request: RouteRequest) -> dict[str, Any]:
        providers = ProviderService.list_providers(db)
        enabled_model_names = ModelCatalogService.enabled_model_name_set(db)
        metrics = LogService.route_metric_summary(db, window_minutes=cls.RECENT_WINDOW_MINUTES, requested_model=request.model_name)
        now = now_beijing()
        route_context = request.policy_context
        effective_forced_provider_id = (
            route_context.forced_provider_id
            if route_context and route_context.forced_provider_id is not None
            else request.forced_provider_id
        )
        allowed_provider_ids = set(route_context.allowed_provider_ids) if route_context and route_context.allowed_provider_ids is not None else None
        diagnostics: dict[str, Any] = {
            "requested_model": request.model_name,
            "require_stream": request.capabilities.require_stream,
            "require_vision": request.capabilities.require_vision,
            "require_tools": request.capabilities.require_tools,
            "require_image_generation": request.capabilities.require_image_generation,
            "require_chat_completions": request.capabilities.require_chat_completions,
            "require_responses": request.capabilities.require_responses,
            "required_upstream_protocol_type": route_helpers.normalize_required_upstream_protocol_type(
                request.capabilities.required_upstream_protocol_type
            ),
            "health_gate_mode": route_helpers.effective_health_gate_mode(route_context),
            "route_strategy": request.strategy.value,
            "is_stream_request": request.capabilities.require_stream,
            "forced_provider_id": effective_forced_provider_id,
            "allowed_provider_ids": sorted(allowed_provider_ids) if allowed_provider_ids is not None else None,
            "provider_total": len(providers),
            "provider_scanned": 0,
            "mounted_model_total": 0,
            "matching_model_mount_count": 0,
            "pre_capacity_candidate_count": 0,
            "final_candidate_count": 0,
            "reason_counts": {},
            "samples": [],
        }
        provider_id_set = {provider.id for provider in providers}
        if effective_forced_provider_id is not None and effective_forced_provider_id not in provider_id_set:
            route_helpers.record_diagnostic_reason(
                diagnostics,
                "forced_provider_not_found",
                extra={"provider_id": effective_forced_provider_id},
            )

        def record_filter_decision(
            decision: RouteFilterDecision,
            *,
            provider: Provider | None = None,
            provider_model: ProviderModel | None = None,
        ) -> None:
            route_helpers.record_diagnostic_reason(
                diagnostics,
                decision.reason_code,
                provider=provider,
                provider_model=provider_model,
                extra=decision.details or None,
            )

        provider_filter_chain = RouteFilterChain(
            (
                ProviderAvailabilityFilter(),
                ProviderContentTrustFilter(),
                ProviderAuthorizationFilter(),
                ProviderEndpointProtocolFilter(),
            )
        )
        model_filter_chain = RouteFilterChain(
            (
                ProviderModelBasicFilter(),
                ProviderModelEndpointProtocolFilter(),
                HealthGateFilter(),
                CapabilityFilter(),
                CapabilityProbeFilter(),
                ModelCircuitFilter(),
            )
        )
        pre_capacity_candidates: list[RouteCandidate] = []
        required_endpoint_path = route_helpers.required_endpoint_path(
            require_chat_completions=request.capabilities.require_chat_completions,
            require_responses=request.capabilities.require_responses,
        )
        filter_context = RouteFilterEvaluationContext(
            db=db,
            model_name=request.model_name,
            route_context=route_context,
            allowed_provider_ids=allowed_provider_ids,
            enabled_model_names=enabled_model_names,
            required_endpoint_path=required_endpoint_path,
            now=now,
            claim_half_open_probe=False,
            **request.capabilities.as_kwargs(),
        )
        for provider in providers:
            if effective_forced_provider_id is not None and provider.id != effective_forced_provider_id:
                continue
            diagnostics["provider_scanned"] += 1
            if not provider.provider_models:
                route_helpers.record_diagnostic_reason(diagnostics, "provider_without_models", provider=provider)
                continue
            provider_decision = provider_filter_chain.evaluate_candidate(provider, provider.provider_models[0], filter_context)
            if not provider_decision.allowed:
                record_filter_decision(provider_decision, provider=provider)
                continue
            for provider_model in provider.provider_models:
                diagnostics["mounted_model_total"] += 1
                if request.model_name and provider_model.model_name == request.model_name:
                    diagnostics["matching_model_mount_count"] += 1
                model_decision = model_filter_chain.evaluate_candidate(provider, provider_model, filter_context)
                if not model_decision.allowed:
                    record_filter_decision(model_decision, provider=provider, provider_model=provider_model)
                    continue
                pre_capacity_candidates.append(
                    RouteCandidate(
                        provider=provider,
                        provider_model=provider_model,
                        recent_failure_rate=float(metrics.get((provider.id, provider_model.model_name), {}).get("failure_rate", 0.0)),
                        health_tier=route_helpers.health_tier(provider=provider, provider_model=provider_model),
                    )
                )
        diagnostics["pre_capacity_candidate_count"] = len(pre_capacity_candidates)
        if not pre_capacity_candidates:
            diagnostics["summary"] = route_helpers.build_diagnostic_summary(diagnostics)
            return diagnostics

        snapshots = ProviderCapacityService.snapshots({item.provider.id for item in pre_capacity_candidates})
        for candidate in pre_capacity_candidates:
            snapshot = snapshots.get(candidate.provider.id)
            if snapshot is None:
                route_helpers.record_diagnostic_reason(
                    diagnostics,
                    "capacity_snapshot_unavailable",
                    provider=candidate.provider,
                    provider_model=candidate.provider_model,
                )
                continue
            if not ProviderCapacityService._has_capacity(candidate.provider, snapshot=snapshot, is_stream=request.capabilities.require_stream):
                route_helpers.record_diagnostic_reason(
                    diagnostics,
                    "provider_capacity_exceeded",
                    provider=candidate.provider,
                    provider_model=candidate.provider_model,
                )
                continue
            diagnostics["final_candidate_count"] += 1
        diagnostics["summary"] = route_helpers.build_diagnostic_summary(diagnostics)
        return diagnostics

    @staticmethod
    def load_recent_session_route(db: Any, sticky_key: str | None) -> Any:
        return route_helpers.load_recent_session_route(db, sticky_key)

    @staticmethod
    def normalize_required_upstream_protocol_type(protocol_type: str | None) -> str | None:
        return route_helpers.normalize_required_upstream_protocol_type(protocol_type)

    @staticmethod
    def effective_max_candidate_count(route_context: RoutePolicyContext | None = None) -> int:
        return route_helpers.effective_max_candidate_count(route_context)

    @staticmethod
    def effective_candidate_expand_count(route_context: RoutePolicyContext | None = None) -> int:
        return route_helpers.effective_candidate_expand_count(route_context)

    @staticmethod
    def effective_candidate_attempt_count(route_context: RoutePolicyContext | None = None) -> int:
        return route_helpers.effective_candidate_attempt_count(route_context)

    @staticmethod
    def diagnostic_reason_label(reason_code: str) -> str:
        return route_helpers.diagnostic_reason_label(reason_code)

    @staticmethod
    def route_retry_infinite_enabled(setting: Any, route_context: RoutePolicyContext | None = None) -> bool:
        return RouteOutageService.retry_infinite_enabled(setting, route_context)

    @staticmethod
    def route_retry_max_wait_seconds(setting: Any) -> int:
        return RouteOutageService.max_wait_seconds(setting)

    @staticmethod
    def route_retry_elapsed_seconds(*, started_at: float) -> float:
        return RouteOutageService.elapsed_seconds(started_at=started_at)

    @staticmethod
    def route_retry_sleep_seconds(
        setting: Any,
        *,
        started_at: float,
        retry_round: int,
        route_context: RoutePolicyContext | None = None,
        upstream_error: dict[str, Any] | None = None,
    ) -> float:
        return RouteOutageService.sleep_seconds(
            setting,
            started_at=started_at,
            retry_round=retry_round,
            route_context=route_context,
            upstream_error=upstream_error,
        )

    @staticmethod
    def route_retry_wait_plan(
        setting: Any,
        *,
        started_at: float,
        retry_round: int,
        route_context: RoutePolicyContext | None = None,
        upstream_error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return RouteOutageService.wait_plan(
            setting,
            started_at=started_at,
            retry_round=retry_round,
            route_context=route_context,
            upstream_error=upstream_error,
        )

    @staticmethod
    def next_retry_plan(
        setting: Any,
        *,
        started_at: float,
        retry_round: int,
        route_context: RoutePolicyContext | None = None,
        diagnostics: dict[str, Any] | None = None,
        upstream_error: dict[str, Any] | None = None,
    ) -> RouteRetryPlan:
        should_retry = False
        reason = None
        if diagnostics is not None and RouteOutageService.should_retry_diagnostics(diagnostics):
            should_retry = True
            reason = "route_diagnostics_recoverable"
        if upstream_error is not None and RouteOutageService.retry_after_seconds_from_upstream_error(upstream_error) is not None:
            should_retry = True
            reason = reason or "upstream_retry_after"
        payload = RouteOutageService.wait_plan(
            setting,
            started_at=started_at,
            retry_round=retry_round,
            route_context=route_context,
            upstream_error=upstream_error,
        )
        sleep_seconds = float(payload.get("sleep_seconds") or 0.0)
        return RouteRetryPlan(
            sleep_seconds=sleep_seconds,
            should_retry=bool(should_retry and (sleep_seconds > 0 or RouteOutageService.retry_infinite_enabled(setting, route_context))),
            reason=reason,
            payload=payload,
        )

    @staticmethod
    def should_retry_route_diagnostics(diagnostics: dict[str, Any] | None) -> bool:
        return RouteOutageService.should_retry_diagnostics(diagnostics)

    @staticmethod
    def build_route_exhausted_retry_upstream_error(
        setting: Any,
        *,
        started_at: float,
        attempt_count: int,
        trace_id: str | None,
        last_upstream_error: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return RouteOutageService.build_retry_exhausted_upstream_error(
            setting,
            started_at=started_at,
            attempt_count=attempt_count,
            trace_id=trace_id,
            last_upstream_error=last_upstream_error,
        )

    @staticmethod
    def route_outage_cache_key(**kwargs: Any) -> str:
        return RouteOutageService.cache_key(**kwargs)

    @staticmethod
    def remember_route_outage(**kwargs: Any) -> None:
        RouteOutageService.remember(**kwargs)

    @staticmethod
    async def maybe_wait_for_route_outage(*args: Any, **kwargs: Any) -> bool:
        return await RouteOutageService.maybe_wait(*args, **kwargs)

    @staticmethod
    def retry_after_seconds_from_detail(detail: Any) -> float | None:
        return RouteOutageService.retry_after_seconds_from_detail(detail)

    @staticmethod
    def retry_after_seconds_from_upstream_error(upstream_error: dict[str, Any] | None) -> float | None:
        return RouteOutageService.retry_after_seconds_from_upstream_error(upstream_error)

    @staticmethod
    def parse_retry_after_seconds(value: Any, *, milliseconds: bool = False) -> float | None:
        return RouteOutageService.parse_retry_after_seconds(value, milliseconds=milliseconds)

    @staticmethod
    def mark_candidate_failed(
        decision: RouteDecision,
        candidate_key: RouteCandidateKey,
        reason: str,
    ) -> RouteDecision:
        decision.request.excluded_candidate_keys.add(candidate_key)
        failed = decision.diagnostics.payload.setdefault("failed_candidates", [])
        failed.append({"provider_id": candidate_key[0], "provider_model_id": candidate_key[1], "reason": reason})
        return decision

    @staticmethod
    def candidate_key(candidate: Any) -> RouteCandidateKey:
        return (int(candidate.provider.id), int(candidate.provider_model.id))

    @staticmethod
    def serialize_failed_candidate_keys(keys: set[RouteCandidateKey]) -> list[dict[str, int]]:
        return [
            {"provider_id": provider_id, "provider_model_id": provider_model_id}
            for provider_id, provider_model_id in sorted(keys)
        ]

    @staticmethod
    def filter_failed_candidates(candidates: list[Any], failed_candidate_keys: set[RouteCandidateKey]) -> list[Any]:
        if not failed_candidate_keys:
            return candidates
        return [
            candidate
            for candidate in candidates
            if RoutingService.candidate_key(candidate) not in failed_candidate_keys
        ]

    @staticmethod
    def mark_candidate_key_failed(failed_candidate_keys: set[RouteCandidateKey], candidate: Any) -> None:
        failed_candidate_keys.add(RoutingService.candidate_key(candidate))

    @classmethod
    def route_candidates_exhausted_trace_item(
        cls,
        diagnostics: dict[str, Any],
        *,
        failed_candidate_keys: set[RouteCandidateKey] | None = None,
    ) -> dict[str, Any]:
        serialized_failed = cls.serialize_failed_candidate_keys(failed_candidate_keys or set())
        return {
            "result": "route_candidates_exhausted",
            "diagnostic": diagnostics,
            "hard_filter_final_candidate_count": diagnostics.get("final_candidate_count"),
            "failed_candidate_count": len(serialized_failed),
            "failed_candidate_keys": serialized_failed,
            "candidate_count_after_failed_exclusion": 0 if serialized_failed else diagnostics.get("final_candidate_count"),
        }

    @staticmethod
    def route_candidate_trace_summary(candidates: list[Any], *, limit: int = 10) -> list[dict[str, Any]]:
        return RouteDecision(
            request=RouteRequest(
                requested_model=None,
                selected_model=None,
                endpoint_path="",
                public_endpoint_path="",
                capabilities=RouteCapabilitySet(),
            ),
            candidates=candidates,
        ).top_candidates_summary(limit=limit)

    @classmethod
    def route_decision_event_payload(
        cls,
        *,
        route_round: int,
        candidate_count: int | None,
        selected_provider_id: int | None = None,
        selected_provider_model_id: int | None = None,
        selected_reason: str | None = None,
        top_candidates: list[dict[str, Any]] | None = None,
        failed_candidate_keys: set[RouteCandidateKey] | None = None,
        sticky_hit: bool | None = None,
        diagnostics: dict[str, Any] | None = None,
        stage_traces: list[dict[str, Any]] | None = None,
        retry_wait_ms: int | None = None,
        retry_wait_plan: dict[str, Any] | None = None,
        route_policy_label: str = "可用性优先",
    ) -> dict[str, Any]:
        serialized_failed_candidates = cls.serialize_failed_candidate_keys(failed_candidate_keys or set())
        hard_filter_final_candidate_count = (
            diagnostics.get("final_candidate_count") if isinstance(diagnostics, dict) else None
        )
        return {
            "route_round": route_round,
            "route_policy": route_policy_label,
            "candidate_count": candidate_count,
            "hard_filter_final_candidate_count": hard_filter_final_candidate_count,
            "candidate_count_after_failed_exclusion": candidate_count,
            "base_candidate_count": cls.effective_max_candidate_count(None),
            "candidate_expand_count": cls.effective_candidate_expand_count(None),
            "candidate_window_count": cls.effective_candidate_attempt_count(None),
            "selected_provider_id": selected_provider_id,
            "selected_provider_model_id": selected_provider_model_id,
            "selected_reason": selected_reason,
            "sticky_hit": sticky_hit,
            "top_candidates_json": dumps_json(top_candidates or []),
            "failed_candidate_keys_json": dumps_json(serialized_failed_candidates),
            "failed_candidate_count": len(serialized_failed_candidates),
            "excluded_summary_json": dumps_json((diagnostics or {}).get("reason_counts")) if diagnostics else None,
            "hard_filter_reason_counts_json": dumps_json((diagnostics or {}).get("reason_counts") or {}),
            "diagnostics_json": dumps_json(diagnostics) if diagnostics is not None else None,
            "stage_traces_json": dumps_json(stage_traces or []),
            "retry_wait_ms": retry_wait_ms,
            "retry_wait_plan_json": dumps_json(retry_wait_plan or {}),
        }

    @classmethod
    def _build_stage_traces(cls, decision: RouteDecision) -> list[RouteStageTrace]:
        candidates = decision.candidates
        diagnostics_payload = decision.diagnostics.to_dict()
        filter_events = cls._filter_events_from_diagnostics(diagnostics_payload)
        top_events = [
            RouteCandidateTraceEvent(
                provider_id=candidate.provider.id,
                provider_model_id=candidate.provider_model.id,
                action=RouteCandidateAction.SELECTED if rank == 1 else RouteCandidateAction.ORDERED,
                reason_code=str(candidate.selection_reason or "ordered_candidate"),
                reason_label=str(candidate.selection_reason or "按路由策略排序"),
                details={
                    "rank": rank,
                    "route_score": round(float(candidate.route_score or 0.0), 4),
                    "score_breakdown": candidate.score_breakdown or {},
                },
            )
            for rank, candidate in enumerate(candidates[:10], start=1)
        ]
        return [
            RouteStageTrace(
                stage=RouteStage.CONTEXT,
                strategy_id=decision.request.strategy.value,
                result=RouteStageResult.SUCCESS,
                input_count=0,
                output_count=0,
                summary={
                    "requested_model": decision.request.requested_model,
                    "selected_model": decision.request.selected_model,
                    "endpoint_path": decision.request.endpoint_path,
                    "public_endpoint_path": decision.request.public_endpoint_path,
                    "capabilities": decision.request.capabilities.as_kwargs(),
                    "policy_context": cls._serialize_policy_context(decision.request.policy_context),
                },
            ),
            RouteStageTrace(
                stage=RouteStage.FILTERS,
                strategy_id=decision.request.strategy.value,
                result=RouteStageResult.SUCCESS if candidates else RouteStageResult.FAILED,
                input_count=int(diagnostics_payload.get("mounted_model_total") or 0),
                output_count=len(candidates),
                events=filter_events,
                summary={
                    "reason_counts": diagnostics_payload.get("reason_counts") or {},
                    "samples": diagnostics_payload.get("samples") or [],
                    "pre_capacity_candidate_count": diagnostics_payload.get("pre_capacity_candidate_count"),
                    "final_candidate_count": diagnostics_payload.get("final_candidate_count"),
                },
            ),
            RouteStageTrace(
                stage=RouteStage.SCORER,
                strategy_id=decision.request.strategy.value,
                result=RouteStageResult.SUCCESS,
                input_count=len(candidates),
                output_count=len(candidates),
                events=top_events,
                summary={"top_candidate_count": min(len(candidates), 10)},
            ),
            RouteStageTrace(
                stage=RouteStage.ORDERER,
                strategy_id=decision.request.strategy.value,
                result=RouteStageResult.SUCCESS if candidates else RouteStageResult.FAILED,
                input_count=len(candidates),
                output_count=len(candidates),
                events=top_events,
                summary={"selected_reason": getattr(decision.selected, "selection_reason", None) if decision.selected else None},
            ),
            RouteStageTrace(
                stage=RouteStage.DIAGNOSER,
                strategy_id=decision.request.strategy.value,
                result=RouteStageResult.SUCCESS,
                input_count=len(candidates),
                output_count=len(candidates),
                summary=diagnostics_payload,
            ),
        ]

    @staticmethod
    def _serialize_policy_context(route_context: RoutePolicyContext | None) -> dict[str, Any] | None:
        if route_context is None:
            return None
        return {
            "allowed_provider_ids": sorted(route_context.allowed_provider_ids or []),
            "forced_provider_id": route_context.forced_provider_id,
            "preferred_provider_ids": list(route_context.preferred_provider_ids or []),
            "preferred_region_tags": list(route_context.preferred_region_tags or []),
            "latency_bias": route_context.latency_bias,
            "success_rate_bias": route_context.success_rate_bias,
            "require_trusted_provider": route_context.require_trusted_provider,
            "content_guard_required": route_context.content_guard_required,
            "health_gate_mode": route_context.health_gate_mode,
        }

    @staticmethod
    def _filter_events_from_diagnostics(diagnostics_payload: dict[str, Any]) -> list[RouteCandidateTraceEvent]:
        events: list[RouteCandidateTraceEvent] = []
        samples = diagnostics_payload.get("samples")
        if not isinstance(samples, list):
            return events
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            reason_code = str(sample.get("reason") or "unknown")
            details = {
                key: value
                for key, value in sample.items()
                if key
                not in {
                    "provider_id",
                    "provider_model_id",
                    "reason",
                    "reason_label",
                }
            }
            events.append(
                RouteCandidateTraceEvent(
                    provider_id=sample.get("provider_id"),
                    provider_model_id=sample.get("provider_model_id"),
                    action=RouteCandidateAction.REJECTED,
                    reason_code=reason_code,
                    reason_label=str(sample.get("reason_label") or route_helpers.diagnostic_reason_label(reason_code)),
                    details=details,
                )
            )
        return events
